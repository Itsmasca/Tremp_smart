"""
Acceso a datos del slice preferences.

Escrituras:
- user_preferences.structured / structured_revision / structured_at  (UPDATE condicional)
- user_preference_embeddings  (UPSERT)

Solo escribe columnas propias de matching-py.
Las columnas user_id, raw_text, revision son propiedad de crud-ts (solo lectura).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def get_structured_revision(conn: Any, user_id: str) -> int | None:
    """
    Retorna structured_revision actual de user_preferences.
    Retorna None si la fila no existe o structured_revision es NULL.
    """
    row = await conn.fetchrow(
        "SELECT structured_revision FROM user_preferences WHERE user_id = $1",
        user_id,
    )
    if row is None:
        return None
    return row["structured_revision"]


async def update_structured(
    conn: Any,
    user_id: str,
    structured: dict,
    revision: int,
) -> bool:
    """
    Escribe structured, structured_revision y structured_at en user_preferences.

    UPDATE condicional: solo actualiza si structured_revision es NULL o menor
    que el revision entrante. Protege contra condiciones de carrera entre
    instancias del consumer.

    Returns:
        True si la fila fue actualizada, False si ya había una revisión igual o mayor.
    """
    now = datetime.now(timezone.utc)
    result = await conn.execute(
        """
        UPDATE user_preferences
           SET structured          = $1::jsonb,
               structured_revision = $2,
               structured_at       = $3
         WHERE user_id             = $4
           AND (structured_revision IS NULL OR structured_revision < $2)
        """,
        json.dumps(structured),
        revision,
        now,
        user_id,
    )
    # asyncpg retorna "UPDATE N" como string
    rows_affected = int(result.split()[-1])
    updated = rows_affected > 0
    if not updated:
        logger.debug(
            "update_structured ignorado (revisión no mayor) | user_id=%s revision=%d",
            user_id,
            revision,
        )
    return updated


async def upsert_embedding(
    conn: Any,
    user_id: str,
    revision: int,
    embedding: list[float],
    model_id: str,
) -> None:
    """
    Inserta o actualiza el embedding en user_preference_embeddings.

    Siempre sobreescribe porque el embedding corresponde a la revisión
    más reciente de structured que acaba de guardarse.
    """
    now = datetime.now(timezone.utc)
    await conn.execute(
        """
        INSERT INTO user_preference_embeddings (user_id, revision, embedding, model_id, updated_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id) DO UPDATE
           SET revision   = EXCLUDED.revision,
               embedding  = EXCLUDED.embedding,
               model_id   = EXCLUDED.model_id,
               updated_at = EXCLUDED.updated_at
        """,
        user_id,
        revision,
        embedding,  # pgvector acepta list[float] con el adaptador instalado
        model_id,
        now,
    )
    logger.debug(
        "upsert_embedding | user_id=%s revision=%d model=%s",
        user_id,
        revision,
        model_id,
    )
