"""
Entry point de matching-py.

Inicializa las dependencias y arranca el consumer de Redis Streams.
"""
from __future__ import annotations

import asyncio
import logging

from app.shared.config import settings
from app.shared.llm import get_llm_provider


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


async def _run() -> None:
    import asyncpg
    import redis.asyncio as aioredis

    from app.features.preferences.handler import handle_preferences_updated
    from app.shared.events.consumer import run_consumer

    # ── Proveedor LLM ──────────────────────────────────────────────────────────
    llm = get_llm_provider(settings)
    logger = logging.getLogger(__name__)
    logger.info("proveedor LLM activo | provider=%s", settings.LLM_PROVIDER)

    # ── Conexiones ─────────────────────────────────────────────────────────────
    db_pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=2, max_size=5)
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=False)

    # ── Dispatcher de eventos ──────────────────────────────────────────────────
    async def on_preferences_updated(raw_json: str) -> None:
        async with db_pool.acquire() as conn:
            await handle_preferences_updated(
                raw_envelope_json=raw_json,
                llm=llm,
                db_conn=conn,
                redis_client=redis_client,
            )

    handlers = {
        "preferences.updated": on_preferences_updated,
    }

    # ── Loop principal ─────────────────────────────────────────────────────────
    await run_consumer(
        redis=redis_client,
        stream=settings.REDIS_STREAM,
        group=settings.REDIS_CONSUMER_GROUP,
        consumer_name=settings.REDIS_CONSUMER_NAME,
        handlers=handlers,
    )


def main() -> None:
    _configure_logging()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
