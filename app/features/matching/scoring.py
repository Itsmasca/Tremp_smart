"""
Ranking de candidatos.

`RankingStrategy` permite sustituir heuristic-v1 por un modelo aprendido
(lgbm-vN) sin tocar el router. Las estrategias son puras: sin I/O.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from app.features.matching.schema import DetourResult, ScoredCandidate

logger = logging.getLogger(__name__)


class RankingStrategy(Protocol):
    model_version: str

    def rank(
        self,
        results: list[DetourResult],
        *,
        departure_at: datetime,
        time_window_min: int,
    ) -> list[ScoredCandidate]: ...


def _clip(x: float) -> float:
    return min(max(x, 0.0), 1.0)


class HeuristicV1:
    """
    score = w_detour·(1 − detour_n) + w_time·(1 − time_n) + w_eta·(1 − eta_n)

    detour_n = max(detour,0) / límite efectivo
    time_n   = |salida_ride − departureAt| / ventana
    eta_n    = t(A→P) / eta_norm_s
    """

    model_version = "heuristic-v1"

    def __init__(self, w_detour: float, w_time: float, w_eta: float, eta_norm_s: float) -> None:
        total = w_detour + w_time + w_eta
        if total <= 0:
            raise ValueError("los pesos de heuristic-v1 deben sumar más de 0")
        if abs(total - 1.0) > 1e-9:
            logger.warning("pesos de heuristic-v1 no suman 1 (%.3f); se normalizan", total)
        self.w_detour = w_detour / total
        self.w_time = w_time / total
        self.w_eta = w_eta / total
        self.eta_norm_s = eta_norm_s

    def rank(
        self,
        results: list[DetourResult],
        *,
        departure_at: datetime,
        time_window_min: int,
    ) -> list[ScoredCandidate]:
        scored = [self._score(r, departure_at, time_window_min) for r in results]
        scored.sort(key=lambda c: (-c.score, c.detour.detour_s, c.detour.ride.id))
        for i, c in enumerate(scored, start=1):
            c.rank = i
        return scored

    def _score(self, r: DetourResult, departure_at: datetime, time_window_min: int) -> ScoredCandidate:
        delta_s = (r.ride.departure_at - departure_at).total_seconds()

        if r.detour_limit_s > 0:
            detour_n = _clip(r.detour_s / r.detour_limit_s)
        else:
            detour_n = 0.0 if r.detour_s <= 0 else 1.0
        time_n = _clip(abs(delta_s) / (time_window_min * 60)) if time_window_min > 0 else 0.0
        eta_n = _clip(r.t_ap_s / self.eta_norm_s)

        score = _clip(self.w_detour * (1 - detour_n) + self.w_time * (1 - time_n) + self.w_eta * (1 - eta_n))
        features = {
            "detour_s_raw": r.detour_s_raw,
            "detour_limit_s": r.detour_limit_s,
            "pickup_eta_s": r.t_ap_s,
            "t_pd_s": r.t_pd_s,
            "t_db_s": r.t_db_s,
            "dist_ap_m": r.d_ap_m,
            "dist_pd_m": r.d_pd_m,
            "dist_db_m": r.d_db_m,
            "corridor_dist_m": r.ride.corridor_dist_m,
            "departure_delta_s": delta_s,
            "seats_available": r.ride.seats_available,
        }
        return ScoredCandidate(detour=r, score=score, features=features)
