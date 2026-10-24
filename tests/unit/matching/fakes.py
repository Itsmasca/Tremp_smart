"""
Dobles de prueba compartidos por los tests del slice matching.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.features.matching.schema import RideRow
from app.shared.osrm.errors import OsrmUnavailable
from app.shared.osrm.types import LatLng, Matrix, Route, SnappedPoint

P = LatLng(lat=21.0186, lng=-89.5847)
D = LatLng(lat=20.9671, lng=-89.6237)
DEPARTURE = datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)


def make_ride(i: int, *, duration_s: float = 900, max_detour_s: float = 600, departure_at: datetime = DEPARTURE) -> RideRow:
    return RideRow(
        id=f"00000000-0000-0000-0000-{i:012d}",
        driver_id=f"10000000-0000-0000-0000-{i:012d}",
        origin=LatLng(lat=21.0 + i * 0.001, lng=-89.5),
        destination=LatLng(lat=20.9 - i * 0.001, lng=-89.7),
        duration_s=duration_s,
        distance_m=12000,
        departure_at=departure_at,
        seats_available=2,
        max_detour_s=max_detour_s,
        corridor_dist_m=10.0 * i,
    )


class FakeOsrm:
    """
    OSRM falso: tiempos por par de puntos (`times[(a, b)]`); los pares sin
    entrada no tienen ruta (None). Distancia = tiempo × 10.
    """

    def __init__(self, times: dict[tuple[LatLng, LatLng], float] | None = None, max_table_size: int = 100) -> None:
        self.times = times or {}
        self.max_table_size = max_table_size
        self.table_calls: list[tuple[list[LatLng], list[int], list[int]]] = []
        self.route_calls: list[list[LatLng]] = []
        self.nearest_calls: list[LatLng] = []
        self.unavailable = False
        self.fail_route_for: set[LatLng] = set()

    async def nearest(self, point: LatLng) -> SnappedPoint:
        self.nearest_calls.append(point)
        if self.unavailable:
            raise OsrmUnavailable("down")
        return SnappedPoint(location=point, distance_m=0.0)

    async def table(self, coords: list[LatLng], sources: list[int], destinations: list[int]) -> Matrix:
        if self.unavailable:
            raise OsrmUnavailable("down")
        assert len(sources) * len(destinations) <= self.max_table_size**2, "tabla excede max_table_size"
        self.table_calls.append((coords, sources, destinations))
        durations = [[self._t(coords[s], coords[d]) for d in destinations] for s in sources]
        distances = [[None if t is None else t * 10 for t in row] for row in durations]
        return Matrix(durations=durations, distances=distances)

    async def route(self, coords: list[LatLng]) -> Route:
        self.route_calls.append(coords)
        if self.unavailable:
            raise OsrmUnavailable("down")
        if coords[0] in self.fail_route_for:
            raise RuntimeError("route falló")
        return Route(
            duration_s=0.0,
            distance_m=0.0,
            geometry={"type": "LineString", "coordinates": [[c.lng, c.lat] for c in coords]},
        )

    def _t(self, a: LatLng, b: LatLng) -> float | None:
        if a == b:
            return 0.0
        return self.times.get((a, b))


def times_for(rides: list[RideRow], *, t_ap: float = 300, t_pd: float = 600, t_db: float = 300, p: LatLng = P, d: LatLng = D) -> dict:
    times: dict[tuple[LatLng, LatLng], float] = {(p, d): t_pd}
    for r in rides:
        times[(r.origin, p)] = t_ap
        times[(d, r.destination)] = t_db
    return times
