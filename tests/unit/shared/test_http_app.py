"""
Tests de la app HTTP compartida.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
from fastapi import APIRouter

from app.shared.http.app import create_app
from app.shared.osrm.errors import OsrmUnavailable


async def test_health_ok_sin_tocar_dependencias() -> None:
    db_pool, osrm = MagicMock(), MagicMock()
    app = create_app(db_pool=db_pool, osrm=osrm)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert db_pool.method_calls == []
    assert osrm.method_calls == []


async def test_osrm_unavailable_mapea_a_503() -> None:
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> None:
        raise OsrmUnavailable("down")

    app = create_app(routers=[router])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/boom")

    assert resp.status_code == 503
    assert resp.json() == {"detail": "routing engine unavailable"}
