"""
Cliente async de OSRM (perfil driving).

Único módulo que conoce el orden lon,lat de las URLs de OSRM.
Un reintento ante errores de transporte; las respuestas con code != "Ok"
no se reintentan.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.shared.osrm.errors import OsrmError, OsrmNoSegment, OsrmTooBig, OsrmUnavailable
from app.shared.osrm.types import LatLng, Matrix, Route, SnappedPoint

logger = logging.getLogger(__name__)

_PROFILE = "driving"
_MAX_ATTEMPTS = 2  # intento original + 1 reintento


def _fmt(points: list[LatLng]) -> str:
    return ";".join(f"{p.lng:.6f},{p.lat:.6f}" for p in points)


def _to_latlng(location: list[float]) -> LatLng:
    lng, lat = location
    return LatLng(lat=lat, lng=lng)


class OsrmClient:
    def __init__(
        self,
        base_url: str,
        max_table_size: int,
        connect_timeout_s: float = 1.0,
        read_timeout_s: float = 2.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.max_table_size = max_table_size
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── API pública ────────────────────────────────────────────────────────────

    async def nearest(self, point: LatLng) -> SnappedPoint:
        body = await self._get(f"/nearest/v1/{_PROFILE}/{_fmt([point])}", {"number": "1"})
        wp = body["waypoints"][0]
        return SnappedPoint(location=_to_latlng(wp["location"]), distance_m=float(wp["distance"]))

    async def table(
        self,
        coords: list[LatLng],
        sources: list[int],
        destinations: list[int],
        *,
        validate_size: bool = True,
    ) -> Matrix:
        """
        Matriz de tiempos y distancias. Filas = sources, columnas = destinations.

        OSRM rechaza (TooBig) cuando len(sources) × len(destinations) > max_table_size².
        Se valida localmente para no gastar el round-trip; la partición en lotes
        es responsabilidad del caller (features/matching/detour.py).
        """
        if validate_size and len(sources) * len(destinations) > self.max_table_size**2:
            raise OsrmTooBig(
                f"tabla {len(sources)}x{len(destinations)} excede max_table_size={self.max_table_size}",
                code="TooBig",
            )
        params = {
            "sources": ";".join(map(str, sources)),
            "destinations": ";".join(map(str, destinations)),
            "annotations": "duration,distance",
        }
        body = await self._get(f"/table/v1/{_PROFILE}/{_fmt(coords)}", params)
        n_rows, n_cols = len(sources), len(destinations)
        empty = [[None] * n_cols for _ in range(n_rows)]
        return Matrix(
            durations=body.get("durations") or empty,
            distances=body.get("distances") or empty,
        )

    async def route(self, coords: list[LatLng]) -> Route:
        params = {"overview": "full", "geometries": "geojson", "steps": "false"}
        body = await self._get(f"/route/v1/{_PROFILE}/{_fmt(coords)}", params)
        r = body["routes"][0]
        return Route(duration_s=float(r["duration"]), distance_m=float(r["distance"]), geometry=r["geometry"])

    # ── Internos ───────────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.TransportError as exc:  # incluye timeouts
                last_exc = exc
                logger.warning("osrm error de transporte | intento=%d path=%s err=%r", attempt, path.split("/")[1], exc)
                continue

            if resp.status_code >= 500:
                last_exc = OsrmUnavailable(f"OSRM HTTP {resp.status_code}")
                logger.warning("osrm 5xx | intento=%d status=%d", attempt, resp.status_code)
                continue

            return self._parse(resp)

        raise OsrmUnavailable(f"OSRM no disponible tras {_MAX_ATTEMPTS} intentos: {last_exc!r}")

    @staticmethod
    def _parse(resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError as exc:
            raise OsrmError(f"respuesta no JSON (HTTP {resp.status_code})") from exc

        code = body.get("code")
        if code == "Ok":
            return body
        message = body.get("message", "")
        if code == "NoSegment":
            raise OsrmNoSegment(message, code=code)
        if code == "TooBig":
            raise OsrmTooBig(message, code=code)
        raise OsrmError(message or f"OSRM code={code} HTTP {resp.status_code}", code=code)
