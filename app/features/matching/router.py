"""
POST /match — contrato con crud-ts en specs/001-osrm-matching/contracts/match-api.openapi.yaml.

Orquesta: bbox → candidatos + preferencias + snap (en paralelo) → matriz OSRM →
descartes → heuristic-v1 → recorte a `limit` → trazado top-K → match.shown.
Dependencias en request.app.state: candidate_source, preferences, osrm, ranker,
match_shown_publisher, settings.
Este slice no escribe en la DB ni llama al LLM; la ruta original del ride
(rides.route) es de crud-ts y aquí solo se calcula al vuelo A→P→D→B.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError

from app.features.matching.detour import DETOUR_DRIVER, DETOUR_PASSENGER, NO_ROUTE, compute_detours
from app.features.matching.events import build_match_shown
from app.features.matching.schema import CandidateOut, LatLngIn, MatchRequest, MatchResponse, ScoredCandidate
from app.shared.osrm.errors import OsrmNoSegment
from app.shared.osrm.types import SnappedPoint

logger = logging.getLogger(__name__)

router = APIRouter()

_OUT_OF_BBOX_MSG = "coordenada fuera del área de servicio (península de Yucatán)"


@router.post("/match", response_model=MatchResponse)
async def post_match(req: MatchRequest, request: Request) -> MatchResponse:
    state = request.app.state
    settings = state.settings
    _validate_bbox(req, settings)

    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    pickup, dropoff = req.pickup.to_latlng(), req.dropoff.to_latlng()

    # ── 1. Candidatos + preferencias + snap, en paralelo ───────────────────────
    results = await asyncio.gather(
        state.candidate_source.fetch(
            pickup=pickup,
            dropoff=dropoff,
            departure_at=req.departureAt,
            time_window_min=req.timeWindowMin,
            passenger_id=req.passengerId,
            max_candidates=settings.MAX_CANDIDATES,
        ),
        state.preferences.get(req.passengerId),
        state.osrm.nearest(pickup),
        state.osrm.nearest(dropoff),
        return_exceptions=True,
    )
    _raise_first_error(results)
    rides, prefs, snapped_p, snapped_d = results
    timings["ms_fetch"] = _ms(t0)

    if snapped_p is None or snapped_d is None or not rides:
        return _respond(req, state, [], None, None, rides or [], {}, timings, t0)

    # ── 2. Matriz OSRM → desvíos ───────────────────────────────────────────────
    t = time.perf_counter()
    viable, discarded = await compute_detours(rides, snapped_p.location, snapped_d.location, state.osrm, prefs)
    timings["ms_matrix"] = _ms(t)

    # ── 3. Ranking + recorte ───────────────────────────────────────────────────
    t = time.perf_counter()
    ranked = state.ranker.rank(viable, departure_at=req.departureAt, time_window_min=req.timeWindowMin)[: req.limit]
    for c in ranked:
        c.features["snap_dist_pickup_m"] = snapped_p.distance_m
        c.features["snap_dist_dropoff_m"] = snapped_d.distance_m
        c.features["prefs"] = prefs.model_dump(mode="json") if prefs is not None else None
    timings["ms_rank"] = _ms(t)

    # ── 4. Trazado A→P→D→B del top-K ───────────────────────────────────────────
    t = time.perf_counter()
    await _attach_geometry(ranked, snapped_p, snapped_d, state.osrm, settings.ROUTE_GEOMETRY_TOP_K)
    timings["ms_route"] = _ms(t)

    response = _respond(req, state, ranked, snapped_p, snapped_d, rides, discarded, timings, t0)

    # ── 5. match.shown (fire-and-forget, después de construir la respuesta) ────
    if ranked:
        state.match_shown_publisher.publish(
            build_match_shown(req, state.ranker.model_version, ranked, snapped_p, snapped_d)
        )
    return response


def _validate_bbox(req: MatchRequest, settings: Any) -> None:
    """Fuera del bbox → 422 con la misma forma que los errores nativos de FastAPI."""
    errors = [
        {
            "loc": ("body", field),
            "msg": _OUT_OF_BBOX_MSG,
            "type": "value_error.out_of_bbox",
            "input": point.model_dump(),
        }
        for field, point in (("pickup", req.pickup), ("dropoff", req.dropoff))
        if not (
            settings.BBOX_LAT_MIN <= point.lat <= settings.BBOX_LAT_MAX
            and settings.BBOX_LNG_MIN <= point.lng <= settings.BBOX_LNG_MAX
        )
    ]
    if errors:
        raise RequestValidationError(errors)


async def _attach_geometry(
    ranked: list[ScoredCandidate],
    snapped_p: SnappedPoint,
    snapped_d: SnappedPoint,
    osrm: Any,
    top_k: int,
) -> None:
    """
    routeGeometry para los primeros `top_k`. Un fallo deja null en ese
    candidato (los tiempos ya están calculados; no es motivo de 503).
    """
    top = ranked[:top_k]
    if not top:
        return
    routes = await asyncio.gather(
        *(
            osrm.route([c.detour.ride.origin, snapped_p.location, snapped_d.location, c.detour.ride.destination])
            for c in top
        ),
        return_exceptions=True,
    )
    for c, r in zip(top, routes):
        if isinstance(r, BaseException):
            logger.warning("route_geometry_failed | rideId=%s err=%r", c.detour.ride.id, r)
            continue
        c.route_geometry = r.geometry


def _raise_first_error(results: list[Any]) -> None:
    """
    Propaga el primer error relevante. OsrmNoSegment (punto sin calle cerca)
    no es error: se traduce a lista vacía marcando el snap como None.
    """
    for i, r in enumerate(results):
        if isinstance(r, OsrmNoSegment):
            results[i] = None
        elif isinstance(r, BaseException):
            raise r


def _respond(
    req: MatchRequest,
    state: Any,
    ranked: list[ScoredCandidate],
    snapped_p: SnappedPoint | None,
    snapped_d: SnappedPoint | None,
    rides: list,
    discarded: dict[str, int],
    timings: dict[str, float],
    t0: float,
) -> MatchResponse:
    candidates = [
        CandidateOut(
            rideId=c.detour.ride.id,
            rank=c.rank,
            score=round(c.score, 4),
            detourS=round(c.detour.detour_s_raw),
            pickupEtaS=round(c.detour.t_ap_s),
            snappedPickup=LatLngIn.from_latlng(snapped_p.location),
            snappedDropoff=LatLngIn.from_latlng(snapped_d.location),
            routeGeometry=c.route_geometry,
        )
        for c in ranked
    ]
    logger.info(
        "match | requestId=%s generated=%d %s=%d %s=%d %s=%d returned=%d %s ms_total=%.1f",
        req.requestId,
        len(rides),
        NO_ROUTE, discarded.get(NO_ROUTE, 0),
        DETOUR_DRIVER, discarded.get(DETOUR_DRIVER, 0),
        DETOUR_PASSENGER, discarded.get(DETOUR_PASSENGER, 0),
        len(candidates),
        " ".join(f"{k}={v:.1f}" for k, v in timings.items()),
        _ms(t0),
    )
    return MatchResponse(requestId=req.requestId, modelVersion=state.ranker.model_version, candidates=candidates)


def _ms(since: float) -> float:
    return (time.perf_counter() - since) * 1000
