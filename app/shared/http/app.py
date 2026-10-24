"""
App FastAPI compartida por los slices que exponen HTTP.

Las dependencias (pool, redis, osrm, estrategias) se guardan en app.state;
cada slice las lee desde request.app.state. El ciclo de vida de los recursos
lo maneja main.py (comparten event loop con el consumer de Redis Streams).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from app.shared.osrm.errors import OsrmUnavailable


def create_app(*, routers: Iterable[APIRouter] = (), **state: Any) -> FastAPI:
    app = FastAPI(title="matching-py", docs_url=None, redoc_url=None)

    for key, value in state.items():
        setattr(app.state, key, value)

    @app.get("/health")
    async def health() -> dict[str, str]:
        # Liveness: no toca DB ni OSRM
        return {"status": "ok"}

    @app.exception_handler(OsrmUnavailable)
    async def _osrm_unavailable(_: Request, __: OsrmUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "routing engine unavailable"})

    for router in routers:
        app.include_router(router)

    return app
