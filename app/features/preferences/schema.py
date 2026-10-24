"""
Esquema Pydantic v2 de preferencias estructuradas.

schema_version = 1 — incrementar si hay cambios incompatibles.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator


class Weekday(str, Enum):
    mon = "mon"
    tue = "tue"
    wed = "wed"
    thu = "thu"
    fri = "fri"
    sat = "sat"
    sun = "sun"


_HH_MM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class TimeWindow(BaseModel):
    """Ventana horaria de disponibilidad."""

    days: list[Weekday] = Field(min_length=1)
    start: str = Field(description="Hora de inicio en formato HH:MM (24h)")
    end: str = Field(description="Hora de fin en formato HH:MM (24h)")

    @field_validator("start", "end")
    @classmethod
    def validate_hhmm(cls, v: str) -> str:
        if not _HH_MM.match(v):
            raise ValueError(f"El horario debe estar en formato HH:MM (24h), recibido: {v!r}")
        return v


class StructuredPreferences(BaseModel):
    """
    Preferencias del usuario extraídas de su texto libre.

    Regla de oro: los campos que el usuario NO mencionó quedan en None / lista vacía.
    El LLM no debe inventar valores.
    """

    schema_version: Literal[1] = 1

    # Preferencia de género de los compañeros de viaje
    gender_preference: Literal["any", "same_only"] = "any"

    # Reglas de convivencia en el auto
    allow_pets: bool | None = None
    allow_smoking: bool | None = None
    allow_music: bool | None = None

    # Desvío máximo aceptable en minutos
    max_detour_minutes: int | None = Field(default=None, ge=0)

    # Zonas de preferencia / evitación (tal como las escribió el usuario)
    preferred_areas: list[str] = Field(default_factory=list)
    avoided_areas: list[str] = Field(default_factory=list)

    # Ventanas de disponibilidad horaria
    time_windows: list[TimeWindow] = Field(default_factory=list)

    # Seguridad nocturna
    avoid_strangers_at_night: bool | None = None

    # Texto residual que no encaja en los campos anteriores
    notes: str | None = None

    # Confianza del LLM en la extracción (0 = nula, 1 = máxima)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]


# JSON Schema derivado del modelo — se pasa al tool use de Bedrock
PREFERENCES_JSON_SCHEMA: dict = StructuredPreferences.model_json_schema()
