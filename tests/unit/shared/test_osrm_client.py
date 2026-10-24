"""
Tests del OsrmClient con respx — sin OSRM real.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.shared.osrm import (
    LatLng,
    OsrmClient,
    OsrmError,
    OsrmNoSegment,
    OsrmTooBig,
    OsrmUnavailable,
)

BASE = "http://osrm.test"
P = LatLng(lat=21.0186, lng=-89.5847)
D = LatLng(lat=20.9671, lng=-89.6237)


@pytest.fixture()
async def client():
    c = OsrmClient(BASE, max_table_size=100)
    yield c
    await c.aclose()


@respx.mock
async def test_nearest_ok(client: OsrmClient) -> None:
    route = respx.get(url__startswith=f"{BASE}/nearest/v1/driving/").mock(
        return_value=httpx.Response(
            200,
            json={"code": "Ok", "waypoints": [{"location": [-89.5850, 21.0190], "distance": 12.5}]},
        )
    )

    snapped = await client.nearest(P)

    req = route.calls.last.request
    # orden lon,lat en la URL
    assert req.url.path == "/nearest/v1/driving/-89.584700,21.018600"
    assert req.url.params["number"] == "1"
    assert snapped.location == LatLng(lat=21.0190, lng=-89.5850)
    assert snapped.distance_m == 12.5


@respx.mock
async def test_nearest_no_segment(client: OsrmClient) -> None:
    respx.get(url__startswith=f"{BASE}/nearest/").mock(
        return_value=httpx.Response(400, json={"code": "NoSegment", "message": "Could not find a matching segment"})
    )
    with pytest.raises(OsrmNoSegment):
        await client.nearest(P)


@respx.mock
async def test_table_ok(client: OsrmClient) -> None:
    route = respx.get(url__startswith=f"{BASE}/table/v1/driving/").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "Ok",
                "durations": [[10.0, 20.0], [0.0, 30.0]],
                "distances": [[100.0, 200.0], [0.0, 300.0]],
            },
        )
    )

    m = await client.table([P, D, P], sources=[0, 1], destinations=[1, 2])

    params = route.calls.last.request.url.params
    assert params["sources"] == "0;1"
    assert params["destinations"] == "1;2"
    assert params["annotations"] == "duration,distance"
    assert m.durations == [[10.0, 20.0], [0.0, 30.0]]
    assert m.distances == [[100.0, 200.0], [0.0, 300.0]]


@respx.mock
async def test_table_null_cells(client: OsrmClient) -> None:
    respx.get(url__startswith=f"{BASE}/table/").mock(
        return_value=httpx.Response(
            200,
            json={"code": "Ok", "durations": [[None, 5.0]], "distances": [[None, 50.0]]},
        )
    )
    m = await client.table([P, D], sources=[0], destinations=[0, 1])
    assert m.durations[0][0] is None
    assert m.distances[0][0] is None


@respx.mock
async def test_table_too_big_local() -> None:
    c = OsrmClient(BASE, max_table_size=3)
    route = respx.get(url__startswith=f"{BASE}/table/")
    with pytest.raises(OsrmTooBig):
        # 4 × 4 = 16 > 3² = 9
        await c.table([P] * 4, sources=[0, 1, 2, 3], destinations=[0, 1, 2, 3])
    assert not route.called
    await c.aclose()


@respx.mock
async def test_route_ok(client: OsrmClient) -> None:
    geometry = {"type": "LineString", "coordinates": [[-89.58, 21.01], [-89.62, 20.96]]}
    route = respx.get(url__startswith=f"{BASE}/route/v1/driving/").mock(
        return_value=httpx.Response(
            200,
            json={"code": "Ok", "routes": [{"duration": 600.0, "distance": 7000.0, "geometry": geometry}]},
        )
    )

    r = await client.route([P, D])

    params = route.calls.last.request.url.params
    assert params["overview"] == "full"
    assert params["geometries"] == "geojson"
    assert r.geometry == geometry
    assert r.duration_s == 600.0


@respx.mock
async def test_retry_on_transport_error(client: OsrmClient) -> None:
    route = respx.get(url__startswith=f"{BASE}/nearest/").mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"code": "Ok", "waypoints": [{"location": [-89.5, 21.0], "distance": 1.0}]}),
        ]
    )
    snapped = await client.nearest(P)
    assert route.call_count == 2
    assert snapped.location == LatLng(lat=21.0, lng=-89.5)


@respx.mock
async def test_unavailable_after_retry(client: OsrmClient) -> None:
    route = respx.get(url__startswith=f"{BASE}/nearest/").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OsrmUnavailable):
        await client.nearest(P)
    assert route.call_count == 2


@respx.mock
async def test_5xx_unavailable(client: OsrmClient) -> None:
    respx.get(url__startswith=f"{BASE}/table/").mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(OsrmUnavailable):
        await client.table([P, D], sources=[0], destinations=[1])


@respx.mock
async def test_no_retry_on_code_error(client: OsrmClient) -> None:
    route = respx.get(url__startswith=f"{BASE}/route/").mock(
        return_value=httpx.Response(400, json={"code": "InvalidQuery", "message": "bad"})
    )
    with pytest.raises(OsrmError) as exc_info:
        await client.route([P, D])
    assert exc_info.value.code == "InvalidQuery"
    assert route.call_count == 1
