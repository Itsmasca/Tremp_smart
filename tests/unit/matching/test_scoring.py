"""
Tests de heuristic-v1.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.features.matching.schema import DetourResult
from app.features.matching.scoring import HeuristicV1
from tests.unit.matching.fakes import DEPARTURE, make_ride


def _result(i: int, *, detour: float, eta: float, dep_delta_min: float = 0, limit: float = 600) -> DetourResult:
    ride = make_ride(i, departure_at=DEPARTURE + timedelta(minutes=dep_delta_min))
    return DetourResult(
        ride=ride, t_ap_s=eta, t_pd_s=600, t_db_s=300,
        d_ap_m=None, d_pd_m=None, d_db_m=None,
        detour_s_raw=detour, detour_limit_s=limit,
    )


def _ranker(**kw) -> HeuristicV1:
    return HeuristicV1(**{"w_detour": 0.5, "w_time": 0.3, "w_eta": 0.2, "eta_norm_s": 1800, **kw})


def _rank(ranker, results, window=30):
    return ranker.rank(results, departure_at=DEPARTURE, time_window_min=window)


def test_formula_con_pesos_default() -> None:
    # detour_n = 300/600 = .5 ; time_n = 15/30 = .5 ; eta_n = 900/1800 = .5
    [c] = _rank(_ranker(), [_result(1, detour=300, eta=900, dep_delta_min=15)])
    assert c.score == pytest.approx(0.5 * 0.5 + 0.3 * 0.5 + 0.2 * 0.5)
    assert c.rank == 1


def test_pesos_configurables() -> None:
    r = _result(1, detour=0, eta=1800, dep_delta_min=0)  # detour_n=0, time_n=0, eta_n=1
    assert _rank(_ranker(), [r])[0].score == pytest.approx(0.8)
    assert _rank(_ranker(w_detour=0, w_time=0, w_eta=1), [r])[0].score == pytest.approx(0.0)


def test_pesos_que_no_suman_1_se_normalizan() -> None:
    r = _result(1, detour=0, eta=0)
    assert _rank(_ranker(w_detour=1, w_time=1, w_eta=2), [r])[0].score == pytest.approx(1.0)


def test_ventana_cero() -> None:
    [c] = _rank(_ranker(), [_result(1, detour=0, eta=0)], window=0)
    assert c.score == pytest.approx(1.0)


def test_limite_cero_con_desvio_no_positivo() -> None:
    [c] = _rank(_ranker(), [_result(1, detour=-50, eta=0, limit=0)])
    assert c.score == pytest.approx(1.0)


def test_score_siempre_en_rango() -> None:
    results = [_result(i, detour=d, eta=e, dep_delta_min=m) for i, (d, e, m) in enumerate(
        [(600, 99999, 45), (-100, 0, -30), (0, 3600, 0)]
    )]
    for c in _rank(_ranker(), results):
        assert 0.0 <= c.score <= 1.0


def test_orden_y_desempates() -> None:
    a = _result(3, detour=100, eta=0)
    b = _result(2, detour=100, eta=0)    # mismo score y desvío que a → rideId menor primero
    c = _result(1, detour=0, eta=0)      # mejor score
    ranked = _rank(_ranker(w_detour=0), [a, b, c])  # sin peso de desvío: empate a/b/c en score
    assert [x.detour.ride.id for x in ranked] == [r.ride.id for r in (c, b, a)]
    assert [x.rank for x in ranked] == [1, 2, 3]

    ranked = _rank(_ranker(), [a, c])
    assert ranked[0].detour.ride.id == c.ride.id


def test_model_version_y_features() -> None:
    ranker = _ranker()
    assert ranker.model_version == "heuristic-v1"
    [c] = _rank(ranker, [_result(1, detour=-10, eta=120, dep_delta_min=-5)])
    assert c.features["detour_s_raw"] == -10
    assert c.features["pickup_eta_s"] == 120
    assert c.features["departure_delta_s"] == -300
    assert c.features["corridor_dist_m"] == 10.0
