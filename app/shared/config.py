"""
Configuración central del servicio matching-py.

Cargada una sola vez al importar; el proceso falla al arrancar
si falta cualquier variable requerida.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    # ── Redis ──────────────────────────────────────────────────────────────────
    REDIS_URL: str
    REDIS_STREAM: str = "rides.events"
    REDIS_DLQ_STREAM: str = "rides.events.dlq"
    REDIS_CONSUMER_GROUP: str = "matching-preferences"
    REDIS_CONSUMER_NAME: str = "matching-py-1"

    # ── Postgres ───────────────────────────────────────────────────────────────
    DATABASE_URL: str

    # ── AWS Bedrock ────────────────────────────────────────────────────────────
    BEDROCK_REGION: str = "us-east-1"
    # Cross-region inference profile — prefijo "us." distribuye carga entre regiones US
    BEDROCK_LLM_MODEL_ID: str = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
    # Titan Text Embeddings V2 — soporta dimensiones 256 / 512 / 1024
    BEDROCK_EMBED_MODEL_ID: str = "amazon.titan-embed-text-v2:0"
    EMBED_DIM: int = 256

    # ── Proveedor ──────────────────────────────────────────────────────────────
    LLM_PROVIDER: Literal["bedrock", "fake"] = "bedrock"

    # ── Misc ───────────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    DB_POOL_MAX_SIZE: int = Field(default=10, ge=2)

    # ── HTTP ───────────────────────────────────────────────────────────────────
    HTTP_PORT: int = 8000

    # ── OSRM ───────────────────────────────────────────────────────────────────
    OSRM_URL: str = "http://osrm:5000"
    # Debe coincidir con --max-table-size del contenedor OSRM
    OSRM_MAX_TABLE_SIZE: int = Field(default=100, ge=3)
    OSRM_CONNECT_TIMEOUT_S: float = 1.0
    OSRM_READ_TIMEOUT_S: float = 2.0

    # ── Matching ───────────────────────────────────────────────────────────────
    CANDIDATE_RADIUS_M: float = Field(default=1500, gt=0)
    MAX_CANDIDATES: int = Field(default=80, ge=1)
    ROUTE_GEOMETRY_TOP_K: int = Field(default=3, ge=0)
    # Estados de matches que NO cuentan como match activo (lista separada por comas)
    MATCH_TERMINAL_STATUSES: str = "rejected,completed,cancelled"

    # ── Scoring heuristic-v1 ───────────────────────────────────────────────────
    PICKUP_ETA_NORM_S: float = Field(default=1800, gt=0)
    SCORE_W_DETOUR: float = Field(default=0.5, ge=0)
    SCORE_W_TIME: float = Field(default=0.3, ge=0)
    SCORE_W_ETA: float = Field(default=0.2, ge=0)

    # ── Bbox península de Yucatán ──────────────────────────────────────────────
    BBOX_LAT_MIN: float = 17.8
    BBOX_LAT_MAX: float = 21.7
    BBOX_LNG_MIN: float = -92.5
    BBOX_LNG_MAX: float = -86.7

    @property
    def match_terminal_statuses(self) -> list[str]:
        return [s.strip() for s in self.MATCH_TERMINAL_STATUSES.split(",") if s.strip()]

    @model_validator(mode="after")
    def _validate_matching(self) -> Settings:
        if self.SCORE_W_DETOUR + self.SCORE_W_TIME + self.SCORE_W_ETA <= 0:
            raise ValueError("SCORE_W_* deben sumar más de 0")
        if self.BBOX_LAT_MIN >= self.BBOX_LAT_MAX or self.BBOX_LNG_MIN >= self.BBOX_LNG_MAX:
            raise ValueError("BBOX_*_MIN debe ser menor que BBOX_*_MAX")
        return self


# Instancia global — importar desde aquí en todos los módulos
settings = Settings()
