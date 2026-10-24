"""
Configuración central del servicio matching-py.

Cargada una sola vez al importar; el proceso falla al arrancar
si falta cualquier variable requerida.
"""
from __future__ import annotations

from typing import Literal

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


# Instancia global — importar desde aquí en todos los módulos
settings = Settings()
