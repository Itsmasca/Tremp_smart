"""
Desvío exacto con OSRM.

Para n candidatos se pide una matriz con
    coords       = [A₁…Aₙ, P, D, B₁…Bₙ]
    sources      = [A₁…Aₙ, P, D]
    destinations = [P, D, B₁…Bₙ]
y detour_i = t(Aᵢ→P) + t(P→D) + t(D→Bᵢ) − ride.duration_s.

OSRM rechaza una tabla cuando len(sources) × len(destinations) > max_table_size²,
así que caben n ≤ max_table_size − 2 candidatos por llamada (research R-03).
Solo si se excede se parte en lotes, con P y D en cada uno.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Protocol

from app.features.matching.schema import DetourResult, RideRow
from app.features.preferences.schema import StructuredPreferences
from app.shared.osrm.types import LatLng, Matrix

NO_ROUTE = "no_route"
DETOUR_DRIVER = "detour_driver"
DETOUR_PASSENGER = "detour_passenger"


class TableClient(Protocol):
    max_table_size: int

    async def table(self, coords: list[LatLng], sources: list[int], destinations: list[int]) -> Matrix: ...


def max_batch_candidates(max_table_size: int) -> int:
    """Candidatos por llamada a /table: (n+2)² ≤ N² ⇔ n ≤ N − 2."""
    return max_table_size - 2


async def compute_detours(
    rides: list[RideRow],
    pickup: LatLng,
    dropoff: LatLng,
    osrm: TableClient,
    prefs: StructuredPreferences | None,
) -> tuple[list[DetourResult], Counter[str]]:
    """
    Devuelve los candidatos viables (en el mismo orden de `rides`) y el conteo
    de descartes por motivo.
    """
    discarded: Counter[str] = Counter()
    if not rides:
        return [], discarded

    size = max_batch_candidates(osrm.max_table_size)
    batches = [rides[i : i + size] for i in range(0, len(rides), size)]
    matrices = await asyncio.gather(*(_table(batch, pickup, dropoff, osrm) for batch in batches))

    passenger_limit_s = (
        prefs.max_detour_minutes * 60 if prefs is not None and prefs.max_detour_minutes is not None else None
    )

    viable: list[DetourResult] = []
    for batch, matrix in zip(batches, matrices):
        n = len(batch)
        t_pd = matrix.durations[n][1]
        d_pd = matrix.distances[n][1]
        for i, ride in enumerate(batch):
            t_ap = matrix.durations[i][0]
            t_db = matrix.durations[n + 1][2 + i]
            if t_ap is None or t_pd is None or t_db is None:
                discarded[NO_ROUTE] += 1
                continue

            detour_raw = t_ap + t_pd + t_db - ride.duration_s
            detour = max(detour_raw, 0.0)
            if detour > ride.max_detour_s:
                discarded[DETOUR_DRIVER] += 1
                continue
            if passenger_limit_s is not None and detour > passenger_limit_s:
                discarded[DETOUR_PASSENGER] += 1
                continue

            limit = ride.max_detour_s if passenger_limit_s is None else min(ride.max_detour_s, passenger_limit_s)
            viable.append(
                DetourResult(
                    ride=ride,
                    t_ap_s=t_ap,
                    t_pd_s=t_pd,
                    t_db_s=t_db,
                    d_ap_m=matrix.distances[i][0],
                    d_pd_m=d_pd,
                    d_db_m=matrix.distances[n + 1][2 + i],
                    detour_s_raw=detour_raw,
                    detour_limit_s=limit,
                )
            )
    return viable, discarded


async def _table(batch: list[RideRow], pickup: LatLng, dropoff: LatLng, osrm: TableClient) -> Matrix:
    n = len(batch)
    coords = [r.origin for r in batch] + [pickup, dropoff] + [r.destination for r in batch]
    sources = list(range(n + 2))
    destinations = [n, n + 1] + list(range(n + 2, 2 * n + 2))
    return await osrm.table(coords, sources, destinations)
