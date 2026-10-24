"""
Tests de extracción en español usando BedrockProvider + Stubber.

Verifican que el parseo de la respuesta Converse API produce el
StructuredPreferences esperado para inputs reales de usuarios mexicanos.
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.stub import Stubber


# ──────────────────────────────────────────────────────────────────────────────
# Fixture de settings (idéntica a test_bedrock_provider.py)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_settings(monkeypatch):
    mock = MagicMock()
    mock.BEDROCK_REGION = "us-east-1"
    mock.BEDROCK_LLM_MODEL_ID = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
    mock.BEDROCK_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
    mock.EMBED_DIM = 256
    monkeypatch.setattr("app.shared.llm.bedrock.settings", mock)
    return mock


def _converse_response(tool_input: dict) -> dict:
    return {
        "ResponseMetadata": {"RequestId": "stub-x", "HTTPStatusCode": 200},
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tu-001",
                            "name": "extract_preferences",
                            "input": tool_input,
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 80, "outputTokens": 40, "totalTokens": 120},
    }


# ──────────────────────────────────────────────────────────────────────────────
# Caso 1: preferencia de género
# ──────────────────────────────────────────────────────────────────────────────

def test_extraccion_genero():
    """
    "prefiero ir solo con mujeres" → gender_preference="same_only"
    """
    from app.shared.llm.bedrock import BedrockProvider

    tool_input = {
        "schema_version": 1,
        "gender_preference": "same_only",
        "allow_pets": None,
        "allow_smoking": None,
        "allow_music": None,
        "max_detour_minutes": None,
        "preferred_areas": [],
        "avoided_areas": [],
        "time_windows": [],
        "avoid_strangers_at_night": None,
        "notes": None,
        "confidence": 0.97,
    }
    provider = BedrockProvider()

    with Stubber(provider._client) as stubber:
        stubber.add_response("converse", _converse_response(tool_input),
                             {"modelId": "us.anthropic.claude-3-5-haiku-20241022-v1:0"})
        result = provider.extract_preferences("prefiero ir solo con mujeres")

    assert result.gender_preference == "same_only"
    assert result.allow_pets is None
    assert result.confidence >= 0.9


# ──────────────────────────────────────────────────────────────────────────────
# Caso 2: mascotas y fumar
# ──────────────────────────────────────────────────────────────────────────────

def test_extraccion_mascotas_y_fumar():
    """
    "no fumo y llevo a mi perro" → allow_smoking=False, allow_pets=True
    """
    from app.shared.llm.bedrock import BedrockProvider

    tool_input = {
        "schema_version": 1,
        "gender_preference": "any",
        "allow_pets": True,
        "allow_smoking": False,
        "allow_music": None,
        "max_detour_minutes": None,
        "preferred_areas": [],
        "avoided_areas": [],
        "time_windows": [],
        "avoid_strangers_at_night": None,
        "notes": None,
        "confidence": 0.99,
    }
    provider = BedrockProvider()

    with Stubber(provider._client) as stubber:
        stubber.add_response("converse", _converse_response(tool_input),
                             {"modelId": "us.anthropic.claude-3-5-haiku-20241022-v1:0"})
        result = provider.extract_preferences("no fumo y llevo a mi perro")

    assert result.allow_smoking is False
    assert result.allow_pets is True
    assert result.gender_preference == "any"


# ──────────────────────────────────────────────────────────────────────────────
# Caso 3: desvío y zona preferida
# ──────────────────────────────────────────────────────────────────────────────

def test_extraccion_desvio_y_zona():
    """
    "puedo desviarme 10 min si es por Altabrisa"
    → max_detour_minutes=10, preferred_areas=["Altabrisa"]
    """
    from app.shared.llm.bedrock import BedrockProvider

    tool_input = {
        "schema_version": 1,
        "gender_preference": "any",
        "allow_pets": None,
        "allow_smoking": None,
        "allow_music": None,
        "max_detour_minutes": 10,
        "preferred_areas": ["Altabrisa"],
        "avoided_areas": [],
        "time_windows": [],
        "avoid_strangers_at_night": None,
        "notes": None,
        "confidence": 0.92,
    }
    provider = BedrockProvider()

    with Stubber(provider._client) as stubber:
        stubber.add_response("converse", _converse_response(tool_input),
                             {"modelId": "us.anthropic.claude-3-5-haiku-20241022-v1:0"})
        result = provider.extract_preferences(
            "puedo desviarme 10 min si es por Altabrisa"
        )

    assert result.max_detour_minutes == 10
    assert "Altabrisa" in result.preferred_areas


# ──────────────────────────────────────────────────────────────────────────────
# Caso 4: ventana horaria entre semana
# ──────────────────────────────────────────────────────────────────────────────

def test_extraccion_ventana_horaria():
    """
    "entre semana salgo 7:30"
    → time_windows con days=[mon,tue,wed,thu,fri], start="07:30"
    """
    from app.features.preferences.schema import Weekday
    from app.shared.llm.bedrock import BedrockProvider

    tool_input = {
        "schema_version": 1,
        "gender_preference": "any",
        "allow_pets": None,
        "allow_smoking": None,
        "allow_music": None,
        "max_detour_minutes": None,
        "preferred_areas": [],
        "avoided_areas": [],
        "time_windows": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": "07:30",
                "end": "23:59",
            }
        ],
        "avoid_strangers_at_night": None,
        "notes": None,
        "confidence": 0.75,
    }
    provider = BedrockProvider()

    with Stubber(provider._client) as stubber:
        stubber.add_response("converse", _converse_response(tool_input),
                             {"modelId": "us.anthropic.claude-3-5-haiku-20241022-v1:0"})
        result = provider.extract_preferences("entre semana salgo 7:30")

    assert len(result.time_windows) == 1
    tw = result.time_windows[0]
    assert Weekday.mon in tw.days
    assert Weekday.fri in tw.days
    assert Weekday.sat not in tw.days
    assert tw.start == "07:30"
