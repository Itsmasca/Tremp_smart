"""
Tests de POST /match con dobles (sin Postgres, OSRM ni Redis reales).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.features.matching.events import MatchShownPublisher
from app.features.matching.router import router
from app.features.matching.scoring import HeuristicV1
from app.features.preferences.schema import StructuredPreferences
from app.shared.config import settings as base_settings
from app.shared.http.app import create_app
from tests.unit.matching.fakes import D, DEPARTURE, P, FakeOsrm, make_ride, times_for

REQUEST_ID = "7b1c2f0e-3d4a-4c8b-9a51-2f1e0c9d8a77"
PASSENGER_ID = "0f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a"


class FakeSource:
    def __init__(self, rides) -> None:
        self.rides = rides
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, **kwargs):
        self.calls.append(kwargs)
        return self.rides[: kwargs["max_candidates"]]


class FakePreferences:
    def __init__(self, prefs: StructuredPreferences | None = None) -> None:
        self.prefs = prefs

    async def get(self, passenger_id):
        return self.prefs


def _body(**overrides) -> dict:
    body = {
        "requestId": REQUEST_ID,
        "passengerId": PASSENGER_ID,
        "pickup": {"lat": P.lat, "lng": P.lng},
        "dropoff": {"lat": D.lat, "lng": D.lng},
        "departureAt": DEPARTURE.isoformat(),
    }
    body.update(overrides)
    return body


def _redis(fail: bool = False) -> MagicMock:
    redis = MagicMock()
    redis.xadd = AsyncMock(side_effect=ConnectionError("redis caído") if fail else None)
    return redis


async def _post(
    *, rides, osrm=None, prefs=None, body=None, redis=None, **settings_overrides
) -> tuple[httpx.Response, FakeOsrm]:
    osrm = osrm or FakeOsrm(times_for(rides))
    settings = base_settings.model_copy(update={"ROUTE_GEOMETRY_TOP_K": 0, **settings_overrides})
    publisher = MatchShownPublisher(redis or _redis(), stream="rides.events")
    app = create_app(
        routers=[router],
        osrm=osrm,
        candidate_source=FakeSource(rides),
        preferences=FakePreferences(prefs),
        ranker=HeuristicV1(w_detour=0.5, w_time=0.3, w_eta=0.2, eta_norm_s=1800),
        match_shown_publisher=publisher,
        settings=settings,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/match", json=body or _body())
    await publisher.drain()
    return resp, osrm


async def test_happy_path() -> None:
    rides = [make_ride(1, duration_s=1100), make_ride(2, duration_s=1000), make_ride(3, duration_s=1150)]
    resp, _ = await _post(rides=rides, body=_body(limit=2))

    assert resp.status_code == 200
    data = resp.json()
    assert data["requestId"] == REQUEST_ID
    assert data["modelVersion"] == "heuristic-v1"
    cands = data["candidates"]
    assert [c["rank"] for c in cands] == [1, 2]
    # Desvíos: r3 = 50, r1 = 100, r2 = 200 → menor desvío primero
    assert [c["rideId"] for c in cands] == [rides[2].id, rides[0].id]
    assert cands[0]["score"] >= cands[1]["score"]
    assert cands[0]["detourS"] == 50
    assert cands[0]["pickupEtaS"] == 300
    for c in cands:
        assert c["snappedPickup"] == {"lat": P.lat, "lng": P.lng}
        assert c["snappedDropoff"] == {"lat": D.lat, "lng": D.lng}
        assert c["routeGeometry"] is None


async def test_parametros_hacia_la_fuente_de_candidatos() -> None:
    rides = [make_ride(1)]
    source = FakeSource(rides)
    app = create_app(
        routers=[router], osrm=FakeOsrm(times_for(rides)), candidate_source=source,
        preferences=FakePreferences(), ranker=HeuristicV1(0.5, 0.3, 0.2, 1800),
        match_shown_publisher=MatchShownPublisher(_redis(), stream="rides.events"),
        settings=base_settings.model_copy(update={"MAX_CANDIDATES": 7}),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/match", json=_body(timeWindowMin=15))

    [call] = source.calls
    assert call["time_window_min"] == 15
    assert call["max_candidates"] == 7
    assert str(call["passenger_id"]) == PASSENGER_ID
    assert call["pickup"] == P


async def test_sin_rides_lista_vacia_sin_llamar_table() -> None:
    resp, osrm = await _post(rides=[])
    assert resp.status_code == 200
    assert resp.json()["candidates"] == []
    assert osrm.table_calls == []


async def test_todos_descartados_lista_vacia() -> None:
    rides = [make_ride(1, duration_s=100, max_detour_s=60)]
    resp, _ = await _post(rides=rides)
    assert resp.status_code == 200
    assert resp.json()["candidates"] == []


async def test_preferencia_de_desvio_filtra() -> None:
    rides = [make_ride(1, duration_s=1100)]  # desvío 100 s
    prefs = StructuredPreferences(max_detour_minutes=1, confidence=0.9)
    resp, _ = await _post(rides=rides, prefs=prefs)
    assert resp.json()["candidates"] == []


@pytest.mark.parametrize("limit", [1, 5])
async def test_limit(limit: int) -> None:
    rides = [make_ride(i) for i in range(3)]
    resp, _ = await _post(rides=rides, body=_body(limit=limit))
    assert len(resp.json()["candidates"]) == min(limit, 3)


# ── US2: trazado top-K ─────────────────────────────────────────────────────────

async def test_geometria_solo_top_k() -> None:
    rides = [make_ride(i, duration_s=1100 + i) for i in range(5)]
    resp, osrm = await _post(rides=rides, ROUTE_GEOMETRY_TOP_K=3)

    cands = resp.json()["candidates"]
    assert [c["routeGeometry"] is not None for c in cands] == [True, True, True, False, False]
    for c in cands[:3]:
        assert c["routeGeometry"]["type"] == "LineString"
    by_id = {r.id: r for r in rides}
    for call, cand in zip(osrm.route_calls, cands[:3]):
        ride = by_id[cand["rideId"]]
        assert call == [ride.origin, P, D, ride.destination]


async def test_fallo_de_route_deja_null_sin_romper() -> None:
    rides = [make_ride(i, duration_s=1100 + i) for i in range(3)]
    osrm = FakeOsrm(times_for(rides))
    # rank 2 = ride con el segundo menor desvío (duration mayor → desvío menor)
    osrm.fail_route_for = {rides[1].origin}
    resp, _ = await _post(rides=rides, osrm=osrm, ROUTE_GEOMETRY_TOP_K=3)

    assert resp.status_code == 200
    cands = resp.json()["candidates"]
    assert cands[1]["rideId"] == rides[1].id
    assert cands[1]["routeGeometry"] is None
    assert cands[0]["routeGeometry"] is not None
    assert cands[2]["routeGeometry"] is not None


async def test_top_k_cero_no_llama_route() -> None:
    rides = [make_ride(1)]
    resp, osrm = await _post(rides=rides, ROUTE_GEOMETRY_TOP_K=0)
    assert resp.json()["candidates"][0]["routeGeometry"] is None
    assert osrm.route_calls == []


# ── US4: errores claros ────────────────────────────────────────────────────────

@pytest.mark.parametrize("campo", ["pickup", "dropoff"])
async def test_fuera_de_bbox_422(campo: str) -> None:
    monterrey = {"lat": 25.6866, "lng": -100.3161}
    resp, osrm = await _post(rides=[make_ride(1)], body=_body(**{campo: monterrey}))

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail[0]["loc"] == ["body", campo]
    assert detail[0]["type"] == "value_error.out_of_bbox"
    assert osrm.nearest_calls == []  # se valida antes de cualquier I/O


@pytest.mark.parametrize(
    "overrides",
    [
        {"departureAt": "2026-10-26T07:30:00"},  # sin zona horaria
        {"departureAt": "mañana"},
        {"limit": 0},
        {"timeWindowMin": -1},
        {"requestId": "no-es-uuid"},
    ],
)
async def test_request_invalido_422(overrides: dict) -> None:
    resp, _ = await _post(rides=[make_ride(1)], body=_body(**overrides))
    assert resp.status_code == 422


async def test_osrm_caido_en_nearest_503_sin_evento() -> None:
    osrm = FakeOsrm()
    osrm.unavailable = True
    redis = _redis()
    resp, _ = await _post(rides=[make_ride(1)], osrm=osrm, redis=redis)

    assert resp.status_code == 503
    assert resp.json() == {"detail": "routing engine unavailable"}
    redis.xadd.assert_not_called()


async def test_osrm_caido_en_table_503() -> None:
    rides = [make_ride(1)]

    class TableDown(FakeOsrm):
        async def table(self, *args, **kwargs):
            from app.shared.osrm.errors import OsrmUnavailable
            raise OsrmUnavailable("down")

    resp, _ = await _post(rides=rides, osrm=TableDown(times_for(rides)))
    assert resp.status_code == 503


# ── US3: match.shown ───────────────────────────────────────────────────────────

async def test_match_shown_publicado_una_vez() -> None:
    redis = _redis()
    resp, _ = await _post(rides=[make_ride(1), make_ride(2)], redis=redis)

    assert resp.status_code == 200
    redis.xadd.assert_awaited_once()
    stream, fields = redis.xadd.await_args.args
    assert stream == "rides.events"
    import json
    envelope = json.loads(fields["envelope"])
    assert envelope["type"] == "match.shown"
    assert envelope["aggregateId"] == REQUEST_ID
    assert [c["rideId"] for c in envelope["data"]["candidates"]] == [
        c["rideId"] for c in resp.json()["candidates"]
    ]


async def test_sin_candidatos_no_publica() -> None:
    redis = _redis()
    resp, _ = await _post(rides=[], redis=redis)
    assert resp.json()["candidates"] == []
    redis.xadd.assert_not_called()


async def test_fallo_de_redis_no_afecta_respuesta() -> None:
    resp, _ = await _post(rides=[make_ride(1)], redis=_redis(fail=True))
    assert resp.status_code == 200
    assert len(resp.json()["candidates"]) == 1
