"""
Tests de BedrockProvider usando botocore.stub.Stubber.

No se realizan llamadas reales a AWS.
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch, MagicMock

import pytest
from botocore.stub import Stubber

from app.features.preferences.schema import StructuredPreferences


# ──────────────────────────────────────────────────────────────────────────────
# Helpers para construir respuestas simuladas
# ──────────────────────────────────────────────────────────────────────────────

def _make_converse_response(tool_input: dict) -> dict:
    """Respuesta Converse API válida con tool use."""
    return {
        "ResponseMetadata": {"RequestId": "stub-001", "HTTPStatusCode": 200},
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tool-001",
                            "name": "extract_preferences",
                            "input": tool_input,
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
    }


def _make_embed_response(embedding: list[float]) -> dict:
    """Respuesta invoke_model para Titan Embeddings."""
    body_bytes = json.dumps({"embedding": embedding}).encode()
    return {
        "ResponseMetadata": {"RequestId": "stub-002", "HTTPStatusCode": 200},
        "contentType": "application/json",
        "body": BytesIO(body_bytes),
    }


def _make_throttling_error() -> dict:
    """Error de throttling que debe disparar reintento."""
    return {
        "Error": {
            "Code": "ThrottlingException",
            "Message": "Rate exceeded",
        }
    }


# ──────────────────────────────────────────────────────────────────────────────
# Fixture: settings mockeados para no necesitar .env
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_settings(monkeypatch):
    """Inyecta settings fijos para que BedrockProvider no lea variables de entorno."""
    mock = MagicMock()
    mock.BEDROCK_REGION = "us-east-1"
    mock.BEDROCK_LLM_MODEL_ID = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
    mock.BEDROCK_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
    mock.EMBED_DIM = 256
    monkeypatch.setattr("app.shared.llm.bedrock.settings", mock)
    monkeypatch.setattr("app.features.preferences.schema.StructuredPreferences.model_json_schema",
                        lambda: {})
    return mock


# ──────────────────────────────────────────────────────────────────────────────
# Test: extracción exitosa con Converse API
# ──────────────────────────────────────────────────────────────────────────────

def test_extract_preferences_stubbed():
    """
    BedrockProvider.extract_preferences parsea correctamente la respuesta
    de Converse API con tool use.
    """
    from app.shared.llm.bedrock import BedrockProvider

    tool_input = {
        "schema_version": 1,
        "gender_preference": "same_only",
        "allow_pets": None,
        "allow_smoking": False,
        "allow_music": None,
        "max_detour_minutes": None,
        "preferred_areas": [],
        "avoided_areas": [],
        "time_windows": [],
        "avoid_strangers_at_night": None,
        "notes": None,
        "confidence": 0.95,
    }

    provider = BedrockProvider()

    with Stubber(provider._client) as stubber:
        stubber.add_response(
            method="converse",
            service_response=_make_converse_response(tool_input),
            expected_params={
                "modelId": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
            },
        )
        result = provider.extract_preferences("prefiero ir solo con mujeres")

    assert isinstance(result, StructuredPreferences)
    assert result.gender_preference == "same_only"
    assert result.allow_smoking is False
    assert result.confidence == pytest.approx(0.95)


# ──────────────────────────────────────────────────────────────────────────────
# Test: embeddings exitosos
# ──────────────────────────────────────────────────────────────────────────────

def test_embed_stubbed():
    """
    BedrockProvider.embed retorna lista de floats de longitud EMBED_DIM.
    """
    from app.shared.llm.bedrock import BedrockProvider

    expected_embedding = [0.1] * 256
    provider = BedrockProvider()

    with Stubber(provider._client) as stubber:
        stubber.add_response(
            method="invoke_model",
            service_response=_make_embed_response(expected_embedding),
            expected_params={
                "modelId": "amazon.titan-embed-text-v2:0",
            },
        )
        result = provider.embed("texto de prueba")

    assert len(result) == 256
    assert result[0] == pytest.approx(0.1)


# ──────────────────────────────────────────────────────────────────────────────
# Test: reintento ante throttling — éxito en tercer intento
# ──────────────────────────────────────────────────────────────────────────────

def test_throttling_retry_success():
    """
    BedrockProvider reintenta ante ThrottlingException y tiene éxito en el
    tercer intento (dentro de MAX_RETRIES=3).
    """
    import app.shared.llm.bedrock as bedrock_module
    from app.shared.llm.bedrock import BedrockProvider

    tool_input = {
        "schema_version": 1,
        "gender_preference": "any",
        "allow_pets": True,
        "allow_smoking": None,
        "allow_music": None,
        "max_detour_minutes": None,
        "preferred_areas": [],
        "avoided_areas": [],
        "time_windows": [],
        "avoid_strangers_at_night": None,
        "notes": None,
        "confidence": 0.8,
    }

    provider = BedrockProvider()

    # Patch sleep para que los tests sean rápidos
    with patch.object(bedrock_module, "time") as mock_time, \
         Stubber(provider._client) as stubber:
        mock_time.sleep = MagicMock()

        # Dos throttling → un éxito
        stubber.add_client_error("converse", service_error_code="ThrottlingException")
        stubber.add_client_error("converse", service_error_code="ThrottlingException")
        stubber.add_response(
            method="converse",
            service_response=_make_converse_response(tool_input),
            expected_params={
                "modelId": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
            },
        )

        result = provider.extract_preferences("no fumo y llevo a mi perro")

    assert isinstance(result, StructuredPreferences)
    assert mock_time.sleep.call_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# Test: throttling agotado → excepción
# ──────────────────────────────────────────────────────────────────────────────

def test_throttling_exhausted():
    """
    Tres ThrottlingException consecutivos → excepción propagada al caller.
    """
    import app.shared.llm.bedrock as bedrock_module
    from botocore.exceptions import ClientError
    from app.shared.llm.bedrock import BedrockProvider

    provider = BedrockProvider()

    with patch.object(bedrock_module, "time") as mock_time, \
         Stubber(provider._client) as stubber:
        mock_time.sleep = MagicMock()

        stubber.add_client_error("converse", service_error_code="ThrottlingException")
        stubber.add_client_error("converse", service_error_code="ThrottlingException")
        stubber.add_client_error("converse", service_error_code="ThrottlingException")

        with pytest.raises(ClientError) as exc_info:
            provider.extract_preferences("texto cualquiera")

    assert "ThrottlingException" in str(exc_info.value)
