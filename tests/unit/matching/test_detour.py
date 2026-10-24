"""
Tests del cálculo de desvío exacto (matriz OSRM).
"""
from __future__ import annotations

from app.features.matching.detour import compute_detours, max_batch_candidates
from app.features.preferences.schema import StructuredPreferences
from tests.unit.matching.fakes import D, P, FakeOsrm, make_ride, times_for


def _prefs(max_detour_minutes: int | None) -> StructuredPreferences:
    return StructuredPreferences(max_detour_minutes=max_detour_minutes, confidence=0.9)


async def test_indices_de_la_matriz() -> None:
    rides = [make_ride(i) for i in range(3)]
    osrm = FakeOsrm(times_for(rides))

    await compute_detours(rides, P, D, osrm, prefs=None)

    coords, sources, destinations = osrm.table_calls[0]
    assert coords == [r.origin for r in rides] + [P, D] + [r.destination for r in rides]
    assert sources == [0, 1, 2, 3, 4]
    assert destinations == [3, 4, 5, 6, 7]


async def test_formula_desvio_y_eta() -> None:
    ride = make_ride(1, duration_s=900)
    times = times_for([ride], t_ap=200, t_pd=500, t_db=400)
    osrm = FakeOsrm(times)

    viable, discarded = await compute_detours([ride], P, D, osrm, prefs=None)

    [r] = viable
    assert (r.t_ap_s, r.t_pd_s, r.t_db_s) == (200, 500, 400)
    assert r.detour_s_raw == 200 + 500 + 400 - 900
    assert (r.d_ap_m, r.d_pd_m, r.d_db_m) == (2000, 5000, 4000)
    assert sum(discarded.values()) == 0


async def test_celda_null_descarta_no_route() -> None:
    rides = [make_ride(1), make_ride(2)]
    times = times_for(rides)
    del times[(rides[0].origin, P)]

    viable, discarded = await compute_detours(rides, P, D, FakeOsrm(times), prefs=None)

    assert [r.ride.id for r in viable] == [rides[1].id]
    assert discarded["no_route"] == 1


async def test_sin_ruta_p_a_d_descarta_todos() -> None:
    rides = [make_ride(1), make_ride(2)]
    times = times_for(rides)
    del times[(P, D)]

    viable, discarded = await compute_detours(rides, P, D, FakeOsrm(times), prefs=None)

    assert viable == []
    assert discarded["no_route"] == 2


async def test_desvio_mayor_al_del_conductor() -> None:
    ride = make_ride(1, duration_s=900, max_detour_s=200)
    times = times_for([ride], t_ap=300, t_pd=600, t_db=300)  # desvío 300

    viable, discarded = await compute_detours([ride], P, D, FakeOsrm(times), prefs=None)

    assert viable == []
    assert discarded["detour_driver"] == 1


async def test_preferencia_del_pasajero_mas_restrictiva() -> None:
    ride = make_ride(1, duration_s=900, max_detour_s=600)
    times = times_for([ride], t_ap=300, t_pd=600, t_db=300)  # desvío 300 s = 5 min

    viable, discarded = await compute_detours([ride], P, D, FakeOsrm(times), prefs=_prefs(4))
    assert viable == []
    assert discarded["detour_passenger"] == 1

    viable, _ = await compute_detours([ride], P, D, FakeOsrm(times), prefs=_prefs(5))
    assert viable[0].detour_limit_s == 300


async def test_desvio_negativo_conserva_valor_real() -> None:
    ride = make_ride(1, duration_s=1300, max_detour_s=0)
    times = times_for([ride], t_ap=300, t_pd=600, t_db=300)  # desvío −100

    viable, _ = await compute_detours([ride], P, D, FakeOsrm(times), prefs=None)

    assert viable[0].detour_s_raw == -100
    assert viable[0].detour_s == 0


async def test_80_candidatos_una_sola_llamada() -> None:
    rides = [make_ride(i) for i in range(80)]
    osrm = FakeOsrm(times_for(rides), max_table_size=100)

    viable, _ = await compute_detours(rides, P, D, osrm, prefs=None)

    assert max_batch_candidates(100) == 98
    assert len(osrm.table_calls) == 1
    assert len(viable) == 80


async def test_lotes_incluyen_p_y_d_y_resultado_identico() -> None:
    rides = [make_ride(i, duration_s=800 + i) for i in range(20)]
    times = times_for(rides)
    for i, r in enumerate(rides):  # tiempos distintos por ride
        times[(r.origin, P)] = 100 + i

    single = FakeOsrm(times, max_table_size=100)
    batched = FakeOsrm(times, max_table_size=10)

    expected, _ = await compute_detours(rides, P, D, single, prefs=None)
    got, _ = await compute_detours(rides, P, D, batched, prefs=None)

    assert len(single.table_calls) == 1
    assert len(batched.table_calls) == 3  # lotes de 8 (= 10 − 2): 8 + 8 + 4
    for coords, _, _ in batched.table_calls:
        assert P in coords and D in coords
    assert [(r.ride.id, r.detour_s_raw, r.t_ap_s) for r in got] == [
        (r.ride.id, r.detour_s_raw, r.t_ap_s) for r in expected
    ]


async def test_sin_rides_no_llama_a_osrm() -> None:
    osrm = FakeOsrm()
    viable, _ = await compute_detours([], P, D, osrm, prefs=None)
    assert viable == []
    assert osrm.table_calls == []
