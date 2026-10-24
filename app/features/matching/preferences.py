"""
Lectura de las preferencias estructuradas del pasajero.

Solo LEE user_preferences.structured (lo escribe el slice preferences).
Nunca invoca al LLM. Preferencias ausentes o inválidas → None: una búsqueda
nunca falla por culpa de las preferencias.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.features.preferences.schema import StructuredPreferences

logger = logging.getLogger(__name__)


class PreferencesReader:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def get(self, passenger_id: UUID) -> StructuredPreferences | None:
        async with self._pool.acquire() as conn:
            return await get_passenger_preferences(conn, passenger_id)


async def get_passenger_preferences(conn: Any, passenger_id: UUID) -> StructuredPreferences | None:
    row = await conn.fetchrow("SELECT structured FROM user_preferences WHERE user_id = $1", passenger_id)
    if row is None or row["structured"] is None:
        return None

    raw = row["structured"]
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return StructuredPreferences.model_validate(data)
    except (ValidationError, ValueError) as exc:
        logger.warning("prefs_invalid | passengerId=%s err=%s", passenger_id, exc)
        return None
