"""
Modelos Pydantic del envelope publicado por crud-ts en rides.events.

El contrato está definido por crud-ts y NO debe modificarse aquí.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class PreferencesUpdatedData(BaseModel):
    userId: str
    revision: int
    rawText: str


class PreferencesUpdatedEnvelope(BaseModel):
    id: str
    type: Literal["preferences.updated"]
    version: int
    source: str
    aggregateType: str
    aggregateId: str
    occurredAt: datetime
    data: PreferencesUpdatedData


def parse_envelope(raw_json: str | bytes) -> PreferencesUpdatedEnvelope | None:
    """
    Parsea el JSON del envelope.

    Retorna None si el `type` no es "preferences.updated" o si el JSON
    no es válido, para que el consumer pueda hacer ACK sin procesar.
    """
    try:
        raw = json.loads(raw_json)
        if raw.get("type") != "preferences.updated":
            return None
        return PreferencesUpdatedEnvelope.model_validate(raw)
    except (ValidationError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("envelope inválido, ignorando: %s | raw=%r", exc, raw_json)
        return None
