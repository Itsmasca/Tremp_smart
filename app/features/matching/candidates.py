"""
Generación de candidatos.

`CandidateSource` permite cambiar a un índice H3 sin tocar el router.
`PostGISCandidateSource` resuelve todos los filtros en UNA query de solo lectura
sobre tablas de crud-ts. La ruta del ride (`rides.route`) la genera crud-ts;
aquí solo se usa para filtrar por corredor y dirección.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from app.features.matching.schema import RideRow
from app.shared.osrm.types import LatLng

_METERS_PER_DEGREE = 111_320
# Margen del pre-filtro en grados: nunca debe excluir de más (el filtro exacto es en geography)
_DEGREE_MARGIN = 1.1

# El pre-filtro sobre geometry usa el índice GIST de rides.route;
# el filtro exacto en metros es sobre geography.
_QUERY = """
WITH pts AS (
    SELECT ST_SetSRID(ST_MakePoint($1, $2), 4326) AS p,
           ST_SetSRID(ST_MakePoint($3, $4), 4326) AS d
)
SELECT r.id::text                  AS id,
       r.driver_id::text           AS driver_id,
       ST_Y(r.origin)              AS origin_lat,
       ST_X(r.origin)              AS origin_lng,
       ST_Y(r.destination)         AS destination_lat,
       ST_X(r.destination)         AS destination_lng,
       r.duration_s,
       r.distance_m,
       r.departure_at,
       r.seats_available,
       r.max_detour_s,
       ST_Distance(r.route::geography, pts.p::geography)
     + ST_Distance(r.route::geography, pts.d::geography) AS corridor_dist_m
  FROM rides r, pts
 WHERE r.status = 'scheduled'
   AND r.seats_available > 0
   AND r.driver_id <> $5::uuid
   AND r.departure_at BETWEEN $6 AND $7
   AND ST_DWithin(r.route, pts.p, $8)
   AND ST_DWithin(r.route, pts.d, $8)
   AND ST_DWithin(r.route::geography, pts.p::geography, $9)
   AND ST_DWithin(r.route::geography, pts.d::geography, $9)
   AND ST_LineLocatePoint(r.route, pts.p) < ST_LineLocatePoint(r.route, pts.d)
   AND NOT EXISTS (
       SELECT 1
         FROM matches m
        WHERE m.ride_id = r.id
          AND m.passenger_id = $5::uuid
          AND m.status <> ALL($10::text[])
   )
 ORDER BY corridor_dist_m
 LIMIT $11
"""


class CandidateSource(Protocol):
    async def fetch(
        self,
        *,
        pickup: LatLng,
        dropoff: LatLng,
        departure_at: datetime,
        time_window_min: int,
        passenger_id: UUID,
        max_candidates: int,
    ) -> list[RideRow]: ...


def radius_degrees(radius_m: float, lat: float) -> float:
    """Radio en grados para el pre-filtro geometry (sobre-estimado a lo largo de la longitud)."""
    return radius_m / (_METERS_PER_DEGREE * math.cos(math.radians(lat))) * _DEGREE_MARGIN


class PostGISCandidateSource:
    def __init__(self, pool: Any, radius_m: float, terminal_statuses: list[str]) -> None:
        self._pool = pool
        self._radius_m = radius_m
        self._terminal_statuses = terminal_statuses

    async def fetch(
        self,
        *,
        pickup: LatLng,
        dropoff: LatLng,
        departure_at: datetime,
        time_window_min: int,
        passenger_id: UUID,
        max_candidates: int,
    ) -> list[RideRow]:
        window = timedelta(minutes=time_window_min)
        radius_deg = radius_degrees(self._radius_m, max(abs(pickup.lat), abs(dropoff.lat)))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                _QUERY,
                pickup.lng, pickup.lat,
                dropoff.lng, dropoff.lat,
                passenger_id,
                departure_at - window, departure_at + window,
                radius_deg,
                self._radius_m,
                self._terminal_statuses,
                max_candidates,
            )
        return [_to_ride(r) for r in rows]


def _to_ride(r: Any) -> RideRow:
    return RideRow(
        id=r["id"],
        driver_id=r["driver_id"],
        origin=LatLng(lat=r["origin_lat"], lng=r["origin_lng"]),
        destination=LatLng(lat=r["destination_lat"], lng=r["destination_lng"]),
        duration_s=float(r["duration_s"]),
        distance_m=float(r["distance_m"]),
        departure_at=r["departure_at"],
        seats_available=int(r["seats_available"]),
        max_detour_s=float(r["max_detour_s"]),
        corridor_dist_m=float(r["corridor_dist_m"]),
    )
