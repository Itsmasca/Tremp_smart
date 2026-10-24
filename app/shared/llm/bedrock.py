"""
Implementación de LLMProvider sobre Amazon Bedrock.

Usa:
- Converse API con tool use forzado para extracción estructurada (Claude 3.5 Haiku)
- invoke_model con Titan Text Embeddings V2 para embeddings

Este módulo es el ÚNICO lugar del proyecto donde se importa boto3.
El handler y el repository no deben importar boto3 directamente.
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.features.preferences.schema import PREFERENCES_JSON_SCHEMA, StructuredPreferences
from app.features.preferences.prompts import SYSTEM_PROMPT, build_retry_prompt
from app.shared.config import settings

logger = logging.getLogger(__name__)

# Nombre de la herramienta en la Converse API
_TOOL_NAME = "extract_preferences"

# Códigos de error que ameritan reintento con backoff
_RETRYABLE_ERRORS = {"ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException"}

# Configuración de reintentos
_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.0


def _build_tool_config() -> dict[str, Any]:
    """Construye el toolConfig para la Converse API con tool_choice forzado."""
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": _TOOL_NAME,
                    "description": (
                        "Extrae las preferencias de viaje del usuario en formato estructurado."
                    ),
                    "inputSchema": {"json": PREFERENCES_JSON_SCHEMA},
                }
            }
        ],
        # Fuerza al modelo a llamar esta herramienta — no puede responder con texto libre
        "toolChoice": {"tool": {"name": _TOOL_NAME}},
    }


def _extract_tool_input(response: dict[str, Any]) -> dict[str, Any]:
    """
    Extrae el dict de argumentos del tool use de la respuesta Converse.

    Ruta: response["output"]["message"]["content"][0]["toolUse"]["input"]
    """
    try:
        content = response["output"]["message"]["content"]
        for block in content:
            if "toolUse" in block and block["toolUse"]["name"] == _TOOL_NAME:
                return block["toolUse"]["input"]
        raise ValueError(f"No se encontró toolUse '{_TOOL_NAME}' en la respuesta")
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Respuesta inesperada de Converse API: {exc}") from exc


class BedrockProvider:
    """
    Proveedor LLM sobre Amazon Bedrock.

    Parámetros leídos de settings:
    - BEDROCK_REGION
    - BEDROCK_LLM_MODEL_ID  (cross-region inference profile)
    - BEDROCK_EMBED_MODEL_ID
    - EMBED_DIM
    """

    def __init__(self) -> None:
        boto_config = Config(
            region_name=settings.BEDROCK_REGION,
            connect_timeout=5,
            read_timeout=30,
            # Los reintentos de throttling se manejan manualmente con backoff
            retries={"max_attempts": 1, "mode": "standard"},
        )
        self._client = boto3.client("bedrock-runtime", config=boto_config)
        self._llm_model_id = settings.BEDROCK_LLM_MODEL_ID
        self._embed_model_id = settings.BEDROCK_EMBED_MODEL_ID
        self._embed_dim = settings.EMBED_DIM
        self._tool_config = _build_tool_config()

    # ──────────────────────────────────────────────────────────────────────────
    # Extracción de preferencias
    # ──────────────────────────────────────────────────────────────────────────

    def extract_preferences(self, raw_text: str) -> StructuredPreferences:
        """
        Extrae preferencias estructuradas usando Converse API con tool use forzado.

        Raises:
            pydantic.ValidationError: si la respuesta no pasa validación.
            Exception: errores no recuperables tras agotar reintentos.
        """
        messages = [{"role": "user", "content": [{"text": raw_text}]}]
        response = self._converse_with_retry(messages)
        tool_input = _extract_tool_input(response)
        return StructuredPreferences.model_validate(tool_input)

    def _converse_with_retry(self, messages: list[dict]) -> dict[str, Any]:
        """Llama a Converse API con backoff exponencial ante throttling."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return self._client.converse(
                    modelId=self._llm_model_id,
                    system=[{"text": SYSTEM_PROMPT}],
                    messages=messages,
                    toolConfig=self._tool_config,
                )
            except ClientError as exc:
                error_code = exc.response["Error"]["Code"]
                if error_code in _RETRYABLE_ERRORS and attempt < _MAX_RETRIES - 1:
                    wait = _BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.5)
                    logger.warning(
                        "Bedrock throttling/timeout (intento %d/%d), esperando %.1fs | error=%s",
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                        error_code,
                    )
                    time.sleep(wait)
                    last_exc = exc
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    def extract_preferences_with_retry_prompt(
        self, raw_text: str, validation_error: str
    ) -> StructuredPreferences:
        """
        Segundo intento de extracción incluyendo el error de validación en el prompt.
        Llamado por el handler cuando el primer intento falla validación.
        """
        retry_text = build_retry_prompt(raw_text, validation_error)
        messages = [{"role": "user", "content": [{"text": retry_text}]}]
        response = self._converse_with_retry(messages)
        tool_input = _extract_tool_input(response)
        return StructuredPreferences.model_validate(tool_input)

    # ──────────────────────────────────────────────────────────────────────────
    # Embeddings
    # ──────────────────────────────────────────────────────────────────────────

    def embed(self, text: str) -> list[float]:
        """
        Genera un embedding usando Titan Text Embeddings V2.

        Returns:
            Lista de floats de longitud EMBED_DIM.
        """
        body = json.dumps(
            {
                "inputText": text,
                "dimensions": self._embed_dim,
                "normalize": True,
            }
        )
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.invoke_model(
                    modelId=self._embed_model_id,
                    body=body,
                    contentType="application/json",
                    accept="application/json",
                )
                result = json.loads(response["body"].read())
                return result["embedding"]
            except ClientError as exc:
                error_code = exc.response["Error"]["Code"]
                if error_code in _RETRYABLE_ERRORS and attempt < _MAX_RETRIES - 1:
                    wait = _BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.5)
                    logger.warning(
                        "Bedrock embed throttling (intento %d/%d), esperando %.1fs",
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError("Se agotaron los reintentos de embed")  # pragma: no cover
