"""
Handler del evento preferences.updated.

Orquesta:
1. Verificación de idempotencia por revisión
2. Extracción con LLM (con reintento y DLQ ante fallo de validación)
3. Escritura condicional en user_preferences
4. Generación y persistencia del embedding
5. ACK al stream (lo hace el consumer genérico tras que este handler retorne)

El handler NO importa boto3 ni sabe qué proveedor LLM está activo.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from app.features.preferences import repository as repo
from app.features.preferences.prompts import build_retry_prompt
from app.features.preferences.schema import StructuredPreferences
from app.shared.config import settings
from app.shared.events.envelope import PreferencesUpdatedEnvelope, parse_envelope
from app.shared.llm.port import LLMProvider

logger = logging.getLogger(__name__)


async def handle_preferences_updated(
    raw_envelope_json: str,
    llm: LLMProvider,
    db_conn: Any,
    redis_client: Any,
) -> None:
    """
    Procesa un evento preferences.updated del stream rides.events.

    Args:
        raw_envelope_json: JSON crudo del campo "envelope" del mensaje Redis.
        llm: Proveedor LLM inyectado (BedrockProvider o FakeLLMProvider).
        db_conn: Conexión asyncpg activa.
        redis_client: Cliente Redis (para publicar en DLQ si es necesario).

    Raises:
        Exception: Solo errores inesperados de infraestructura. El consumer
            los captura y NO hace ACK para permitir reprocesamiento.
    """
    # ── 1. Parsear envelope ────────────────────────────────────────────────────
    envelope: PreferencesUpdatedEnvelope | None = parse_envelope(raw_envelope_json)
    if envelope is None:
        # Tipo desconocido o JSON inválido → el consumer ya hizo ACK
        return

    user_id = envelope.data.userId
    revision = envelope.data.revision
    raw_text = envelope.data.rawText

    logger.info(
        "procesando preferences.updated | user_id=%s revision=%d",
        user_id,
        revision,
    )

    # ── 2. Idempotencia: verificar revisión actual ─────────────────────────────
    current_revision = await repo.get_structured_revision(db_conn, user_id)
    if current_revision is not None and revision <= current_revision:
        logger.info(
            "revisión ignorada (no mayor) | user_id=%s event_revision=%d current=%d",
            user_id,
            revision,
            current_revision,
        )
        return  # El consumer hará ACK normalmente

    # ── 3. Extracción de preferencias con LLM ─────────────────────────────────
    structured = await _extract_with_retry_and_dlq(
        llm=llm,
        raw_text=raw_text,
        original_envelope=raw_envelope_json,
        redis_client=redis_client,
    )
    if structured is None:
        # Error de validación agotado → mensaje enviado a DLQ, hacer ACK
        return

    # ── 4. Escritura condicional en user_preferences ──────────────────────────
    updated = await repo.update_structured(
        conn=db_conn,
        user_id=user_id,
        structured=structured.model_dump(mode="json"),
        revision=revision,
    )
    if not updated:
        # Condición de carrera: otra instancia ya escribió una revisión >= esta
        logger.info(
            "update_structured no aplicado (carrera) | user_id=%s revision=%d",
            user_id,
            revision,
        )
        return

    # ── 5. Generación y persistencia del embedding ────────────────────────────
    embedding = llm.embed(raw_text)
    await repo.upsert_embedding(
        conn=db_conn,
        user_id=user_id,
        revision=revision,
        embedding=embedding,
        model_id=settings.BEDROCK_EMBED_MODEL_ID,
    )

    logger.info(
        "preferences.updated procesado OK | user_id=%s revision=%d",
        user_id,
        revision,
    )
    # El consumer hace ACK tras retorno exitoso


# ──────────────────────────────────────────────────────────────────────────────
# Helpers privados
# ──────────────────────────────────────────────────────────────────────────────


async def _extract_with_retry_and_dlq(
    llm: LLMProvider,
    raw_text: str,
    original_envelope: str,
    redis_client: Any,
) -> StructuredPreferences | None:
    """
    Intenta extraer preferencias con hasta 2 llamadas al LLM.

    - Intento 1: prompt normal.
    - Intento 2 (si falla validación): prompt con el error incluido.
    - Si ambos fallan: publica en DLQ y retorna None.

    Returns:
        StructuredPreferences si al menos un intento tuvo éxito, None si ambos fallaron.
    """
    # Intento 1
    try:
        return llm.extract_preferences(raw_text)
    except ValidationError as first_error:
        logger.warning(
            "validación fallida en intento 1, reintentando | error=%s",
            first_error,
        )

    # Intento 2 con error incluido en el prompt
    # BedrockProvider tiene el método especializado; FakeLLMProvider usa el mismo extract
    retry_text = build_retry_prompt(raw_text, str(first_error))
    try:
        # Si el proveedor expone extract_preferences_with_retry_prompt, úsalo
        if hasattr(llm, "extract_preferences_with_retry_prompt"):
            return llm.extract_preferences_with_retry_prompt(raw_text, str(first_error))
        # Fallback genérico: pasar el prompt de reintento como raw_text
        return llm.extract_preferences(retry_text)
    except ValidationError as second_error:
        logger.error(
            "validación fallida en intento 2, enviando a DLQ | error=%s",
            second_error,
        )
        await _send_to_dlq(
            redis_client=redis_client,
            original_envelope=original_envelope,
            reason=str(second_error),
        )
        return None


async def _send_to_dlq(
    redis_client: Any,
    original_envelope: str,
    reason: str,
) -> None:
    """Publica el mensaje original en rides.events.dlq con el motivo del error."""
    dlq_payload = json.dumps(
        {
            "original_envelope": original_envelope,
            "reason": reason,
            "source": "matching-py:preferences-handler",
        }
    )
    await redis_client.xadd(
        settings.REDIS_DLQ_STREAM,
        {"payload": dlq_payload},
    )
    logger.info("mensaje enviado a DLQ | stream=%s", settings.REDIS_DLQ_STREAM)
