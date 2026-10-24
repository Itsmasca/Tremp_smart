"""
Entry point de matching-py.

Inicializa las dependencias y arranca, en el mismo event loop:
- el servidor HTTP interno (POST /match, GET /health)
- el consumer de Redis Streams (slice preferences)
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app.shared.config import settings
from app.shared.llm import get_llm_provider


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


async def _run() -> int:
    import asyncpg
    import redis.asyncio as aioredis
    import uvicorn

    from app.features.matching.candidates import PostGISCandidateSource
    from app.features.matching.events import MatchShownPublisher
    from app.features.matching.preferences import PreferencesReader
    from app.features.matching.router import router as matching_router
    from app.features.matching.scoring import HeuristicV1
    from app.features.preferences.handler import handle_preferences_updated
    from app.shared.events.consumer import run_consumer
    from app.shared.http.app import create_app
    from app.shared.osrm import OsrmClient

    # ── Proveedor LLM ──────────────────────────────────────────────────────────
    llm = get_llm_provider(settings)
    logger = logging.getLogger(__name__)
    logger.info("proveedor LLM activo | provider=%s", settings.LLM_PROVIDER)

    # ── Conexiones ─────────────────────────────────────────────────────────────
    db_pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=2, max_size=settings.DB_POOL_MAX_SIZE)
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=False)
    osrm = OsrmClient(
        settings.OSRM_URL,
        max_table_size=settings.OSRM_MAX_TABLE_SIZE,
        connect_timeout_s=settings.OSRM_CONNECT_TIMEOUT_S,
        read_timeout_s=settings.OSRM_READ_TIMEOUT_S,
    )

    # ── HTTP ───────────────────────────────────────────────────────────────────
    match_shown_publisher = MatchShownPublisher(redis_client, stream=settings.REDIS_STREAM)
    app = create_app(
        routers=[matching_router],
        db_pool=db_pool,
        redis=redis_client,
        osrm=osrm,
        candidate_source=PostGISCandidateSource(
            db_pool,
            radius_m=settings.CANDIDATE_RADIUS_M,
            terminal_statuses=settings.match_terminal_statuses,
        ),
        preferences=PreferencesReader(db_pool),
        ranker=HeuristicV1(
            w_detour=settings.SCORE_W_DETOUR,
            w_time=settings.SCORE_W_TIME,
            w_eta=settings.SCORE_W_ETA,
            eta_norm_s=settings.PICKUP_ETA_NORM_S,
        ),
        match_shown_publisher=match_shown_publisher,
        settings=settings,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.HTTP_PORT, log_config=None))

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

    # ── HTTP + consumer en el mismo loop ───────────────────────────────────────
    http_task = asyncio.create_task(server.serve(), name="http")
    consumer_task = asyncio.create_task(
        run_consumer(
            redis=redis_client,
            stream=settings.REDIS_STREAM,
            group=settings.REDIS_CONSUMER_GROUP,
            consumer_name=settings.REDIS_CONSUMER_NAME,
            handlers=handlers,
        ),
        name="consumer",
    )

    exit_code = 1
    try:
        done, pending = await asyncio.wait({http_task, consumer_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exc = task.exception()
            if exc is not None:
                logger.error("tarea terminó con error | task=%s err=%r", task.get_name(), exc)
        # Salida limpia solo si uvicorn se detuvo por señal (SIGTERM/SIGINT) sin error
        if http_task in done and http_task.exception() is None and server.should_exit:
            exit_code = 0
        else:
            logger.error("una tarea terminó inesperadamente; saliendo para que el orquestador reinicie")
    finally:
        # match.shown pendientes: esperar poco, nunca bloquear el apagado
        await match_shown_publisher.drain(timeout_s=2.0)
        await osrm.aclose()
        await db_pool.close()
        await redis_client.aclose()
    return exit_code


def main() -> None:
    _configure_logging()
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
