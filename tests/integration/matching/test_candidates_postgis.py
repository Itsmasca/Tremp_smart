"""
Query de candidatos contra PostGIS real.

Opt-in: `uv run pytest -m integration` con TEST_DATABASE_URL apuntando a un
Postgres con PostGIS (p. ej. imagen postgis/postgis:16-3.4). Cada test usa un
schema temporal y lo borra al terminar.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.features.matching.candidates import PostGISCandidateSource
from app.shared.osrm.types import LatLng

pytestmark = pytest.mark.integration

DSN = os.environ.get("TEST_DATABASE_URL")
SCHEMA_SQL = (Path(__file__).parents[2] / "fixtures" / "crud_ts_schema.sql").read_text(encoding="utf-8")

# Altabrisa → Centro (Mérida)
P = LatLng(lat=21.0186, lng=-89.5847)
D = LatLng(lat=20.9671, lng=-89.6237)
DEPARTURE = datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)  # 07:30 en Mérida
PASSENGER = uuid.UUID("0f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a")

# Cholul → sur del Centro, pasando a pocos metros de P y luego de D
ROUTE_OK = [(-89.555, 21.045), (-89.5847, 21.0190), (-89.6237, 20.9675), (-89.630, 20.950)]


def _linestring(points: list[tuple[float, float]], dlng: float = 0.0) -> str:
    return "LINESTRING(" + ", ".join(f"{lng + dlng} {lat}" for lng, lat in points) + ")"


@pytest.fixture()
async def pool():
    if not DSN:
        pytest.skip("TEST_DATABASE_URL no definido")
    import asyncpg

    schema = f"test_matching_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f"CREATE EXTENSION IF NOT EXISTS postgis; CREATE SCHEMA {schema}")
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2, server_settings={"search_path": f"{schema},public"})
    async with p.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    try:
        yield p
    finally:
        await p.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


async def _insert_ride(conn, *, route, driver_id=None, departure_at=DEPARTURE, seats=2, max_detour_s=600, status="scheduled"):
    ride_id = uuid.uuid4()
    first, last = route.split("(")[1].rstrip(")").split(", ")[0], route.split(", ")[-1].rstrip(")")
    await conn.execute(
        """
        INSERT INTO rides (id, driver_id, origin, destination, route, duration_s, distance_m,
                           departure_at, seats_available, max_detour_s, status)
        VALUES ($1, $2, ST_GeomFromText('POINT(' || $3 || ')', 4326), ST_GeomFromText('POINT(' || $4 || ')', 4326),
                ST_GeomFromText($5, 4326), 900, 12000, $6, $7, $8, $9)
        """,
        ride_id, driver_id or uuid.uuid4(), first, last, route, departure_at, seats, max_detour_s, status,
    )
    return str(ride_id)


async def _fetch(pool, **overrides):
    source = PostGISCandidateSource(pool, radius_m=1500, terminal_statuses=["rejected", "completed", "cancelled"])
    kwargs = dict(
        pickup=P, dropoff=D, departure_at=DEPARTURE, time_window_min=30,
        passenger_id=PASSENGER, max_candidates=80,
    )
    kwargs.update(overrides)
    return await source.fetch(**kwargs)


async def test_escenarios_quickstart(pool) -> None:
    ok = _linestring(ROUTE_OK)
    async with pool.acquire() as conn:
        r1 = await _insert_ride(conn, route=ok)
        await _insert_ride(conn, route=_linestring(list(reversed(ROUTE_OK))))          # R2 sentido contrario
        await _insert_ride(conn, route=ok, seats=0)                                    # R3 sin asientos
        await _insert_ride(conn, route=ok, departure_at=DEPARTURE + timedelta(minutes=90))  # R4 fuera de ventana
        r5 = await _insert_ride(conn, route=ok, max_detour_s=30)                       # R5 se filtra en detour, no en SQL
        await _insert_ride(conn, route=ok, driver_id=PASSENGER)                        # R6 propio
        r7 = await _insert_ride(conn, route=ok)                                        # R7 match activo
        await conn.execute("INSERT INTO matches (ride_id, passenger_id, status) VALUES ($1, $2, 'pending')", uuid.UUID(r7), PASSENGER)
        r8 = await _insert_ride(conn, route=ok)                                        # match terminado → sí aparece
        await conn.execute("INSERT INTO matches (ride_id, passenger_id, status) VALUES ($1, $2, 'cancelled')", uuid.UUID(r8), PASSENGER)
        await _insert_ride(conn, route=ok, status="cancelled")                         # ride cancelado

    rows = await _fetch(pool)

    assert {r.id for r in rows} == {r1, r5, r8}
    row = next(r for r in rows if r.id == r1)
    assert row.origin == LatLng(lat=21.045, lng=-89.555)
    assert row.destination == LatLng(lat=20.950, lng=-89.630)
    assert row.duration_s == 900
    assert row.corridor_dist_m < 100


async def test_radio(pool) -> None:
    async with pool.acquire() as conn:
        cerca = await _insert_ride(conn, route=_linestring(ROUTE_OK, dlng=0.010))  # ~1.0 km al este
        await _insert_ride(conn, route=_linestring(ROUTE_OK, dlng=0.017))          # ~1.8 km al este

    rows = await _fetch(pool)

    assert [r.id for r in rows] == [cerca]


async def test_tope_y_orden_por_corredor(pool) -> None:
    async with pool.acquire() as conn:
        ids = [await _insert_ride(conn, route=_linestring(ROUTE_OK, dlng=0.002 * i)) for i in range(5)]

    rows = await _fetch(pool, max_candidates=3)

    assert [r.id for r in rows] == ids[:3]
    assert rows[0].corridor_dist_m <= rows[1].corridor_dist_m <= rows[2].corridor_dist_m
