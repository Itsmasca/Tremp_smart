"""
Tests unitarios del handler preferences.updated.

Usa FakeLLMProvider — sin llamadas reales a AWS ni a Postgres/Redis.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.features.preferences.handler import handle_preferences_updated
from app.features.preferences.schema import StructuredPreferences
from app.shared.llm.fake import (
    AlwaysFailFakeLLMProvider,
    FailOnceFakeLLMProvider,
    FakeLLMProvider,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_envelope(revision: int = 3, raw_text: str = "prefiero mujeres") -> str:
    return json.dumps(
        {
            "id": "evt-001",
            "type": "preferences.updated",
            "version": 1,
            "source": "crud-ts",
            "aggregateType": "user",
            "aggregateId": "user-uuid-1",
            "occurredAt": datetime.now(timezone.utc).isoformat(),
            "data": {
                "userId": "user-uuid-1",
                "revision": revision,
                "rawText": raw_text,
            },
        }
    )


def _make_db_conn(current_revision: int | None = None) -> MagicMock:
    """Mock de conexión asyncpg."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"structured_revision": current_revision} if current_revision is not None else None
    )
    conn.execute = AsyncMock(return_value=f"UPDATE 1")
    return conn


def _make_redis() -> MagicMock:
    redis = MagicMock()
    redis.xadd = AsyncMock()
    return redis


# ──────────────────────────────────────────────────────────────────────────────
# Tests de idempotencia
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_idempotency_old_revision(valid_structured_preferences):
    """Evento con revisión vieja (2 < 5) → no escribe, no llama al LLM."""
    llm = FakeLLMProvider(response_override=valid_structured_preferences)
    db_conn = _make_db_conn(current_revision=5)
    redis = _make_redis()

    await handle_preferences_updated(
        raw_envelope_json=_make_envelope(revision=2),
        llm=llm,
        db_conn=db_conn,
        redis_client=redis,
    )

    # No debe haber escrito nada
    db_conn.execute.assert_not_called()
    redis.xadd.assert_not_called()


@pytest.mark.asyncio
async def test_idempotency_same_revision(valid_structured_preferences):
    """Evento con misma revisión → no escribe."""
    llm = FakeLLMProvider(response_override=valid_structured_preferences)
    db_conn = _make_db_conn(current_revision=3)
    redis = _make_redis()

    await handle_preferences_updated(
        raw_envelope_json=_make_envelope(revision=3),
        llm=llm,
        db_conn=db_conn,
        redis_client=redis,
    )

    db_conn.execute.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_happy_path(valid_structured_preferences):
    """Revisión nueva → extrae, escribe structured y embedding."""
    llm = FakeLLMProvider(response_override=valid_structured_preferences)
    db_conn = _make_db_conn(current_revision=1)
    redis = _make_redis()

    with patch("app.shared.config.settings") as mock_settings:
        mock_settings.BEDROCK_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
        mock_settings.REDIS_DLQ_STREAM = "rides.events.dlq"
        mock_settings.EMBED_DIM = 256

        await handle_preferences_updated(
            raw_envelope_json=_make_envelope(revision=3),
            llm=llm,
            db_conn=db_conn,
            redis_client=redis,
        )

    # Debe haber llamado a execute dos veces: update_structured + upsert_embedding
    assert db_conn.execute.call_count == 2
    redis.xadd.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# Reintento por validación
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validation_retry_success(valid_structured_preferences):
    """Primer intento falla validación, segundo OK → escribe normalmente."""
    llm = FailOnceFakeLLMProvider(success_response=valid_structured_preferences)
    db_conn = _make_db_conn(current_revision=None)
    redis = _make_redis()

    with patch("app.shared.config.settings") as mock_settings:
        mock_settings.BEDROCK_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
        mock_settings.REDIS_DLQ_STREAM = "rides.events.dlq"
        mock_settings.EMBED_DIM = 256

        await handle_preferences_updated(
            raw_envelope_json=_make_envelope(revision=1),
            llm=llm,
            db_conn=db_conn,
            redis_client=redis,
        )

    assert db_conn.execute.call_count == 2  # update_structured + upsert_embedding
    redis.xadd.assert_not_called()


@pytest.mark.asyncio
async def test_validation_retry_dlq():
    """Ambos intentos fallan validación → mensaje publicado en DLQ, no escribe en DB."""
    llm = AlwaysFailFakeLLMProvider()
    db_conn = _make_db_conn(current_revision=None)
    redis = _make_redis()

    with patch("app.shared.config.settings") as mock_settings:
        mock_settings.BEDROCK_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
        mock_settings.REDIS_DLQ_STREAM = "rides.events.dlq"
        mock_settings.EMBED_DIM = 256

        await handle_preferences_updated(
            raw_envelope_json=_make_envelope(revision=1),
            llm=llm,
            db_conn=db_conn,
            redis_client=redis,
        )

    # No debe escribir en DB
    db_conn.execute.assert_not_called()

    # Debe publicar en DLQ
    redis.xadd.assert_called_once()
    dlq_stream, dlq_fields = redis.xadd.call_args[0]
    assert "dlq" in dlq_stream
    dlq_payload = json.loads(dlq_fields["payload"])
    assert "reason" in dlq_payload
    assert "source" in dlq_payload


# ──────────────────────────────────────────────────────────────────────────────
# Tipo de evento ignorado
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_event_type_ignored():
    """Eventos de tipo distinto a preferences.updated se ignoran silenciosamente."""
    llm = FakeLLMProvider()
    db_conn = _make_db_conn()
    redis = _make_redis()

    unknown_envelope = json.dumps(
        {
            "id": "evt-002",
            "type": "ride.created",
            "version": 1,
            "source": "crud-ts",
            "aggregateType": "ride",
            "aggregateId": "ride-uuid-1",
            "occurredAt": datetime.now(timezone.utc).isoformat(),
            "data": {},
        }
    )

    await handle_preferences_updated(
        raw_envelope_json=unknown_envelope,
        llm=llm,
        db_conn=db_conn,
        redis_client=redis,
    )

    db_conn.execute.assert_not_called()
    redis.xadd.assert_not_called()
