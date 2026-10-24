"""
Tests del evento match.shown.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import jsonschema
import pytest

from app.features.matching.events import MatchShownPublisher, build_match_shown
from app.features.matching.schema import DetourResult, LatLngIn, MatchRequest, ScoredCandidate
from app.shared.osrm.types import SnappedPoint
from tests.unit.matching.fakes import D, DEPARTURE, P, make_ride

SCHEMA = json.loads(
    (Path(__file__).parents[3] / "specs" / "001-osrm-matching" / "contracts" / "match-shown.event.schema.json")
    .read_text(encoding="utf-8")
)
REQUEST_ID = "7b1c2f0e-3d4a-4c8b-9a51-2f1e0c9d8a77"


def _req() -> MatchRequest:
    return MatchRequest(
        requestId=UUID(REQUEST_ID),
        passengerId=UUID("0f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a"),
        pickup=LatLngIn(lat=P.lat, lng=P.lng),
        dropoff=LatLngIn(lat=D.lat, lng=D.lng),
        departureAt=DEPARTURE,
    )


def _scored(i: int, rank: int) -> ScoredCandidate:
    detour = DetourResult(
        ride=make_ride(i), t_ap_s=300.4, t_pd_s=600, t_db_s=300,
        d_ap_m=3000, d_pd_m=None, d_db_m=3000, detour_s_raw=-20.6, detour_limit_s=600,
    )
    return ScoredCandidate(detour=detour, score=0.81234, features={"pickup_eta_s": 300.4, "prefs": None}, rank=rank)


def test_envelope_valido_contra_schema() -> None:
    envelope = build_match_shown(
        _req(), "heuristic-v1", [_scored(1, 1), _scored(2, 2)],
        SnappedPoint(location=P, distance_m=3.0), SnappedPoint(location=D, distance_m=4.0),
    )

    jsonschema.validate(envelope, SCHEMA)
    assert envelope["aggregateId"] == REQUEST_ID
    assert envelope["data"]["requestId"] == REQUEST_ID
    assert envelope["data"]["timeWindowMin"] == 30
    assert envelope["data"]["pickup"] == {"lat": P.lat, "lng": P.lng}
    first = envelope["data"]["candidates"][0]
    assert first["detourS"] == -21
    assert first["pickupEtaS"] == 300
    json.dumps(envelope)  # serializable


async def test_publish_hace_xadd_con_campo_envelope() -> None:
    redis = MagicMock()
    redis.xadd = AsyncMock()
    publisher = MatchShownPublisher(redis, stream="rides.events")

    publisher.publish({"type": "match.shown", "x": 1})
    await publisher.drain()

    redis.xadd.assert_awaited_once()
    stream, fields = redis.xadd.await_args.args
    assert stream == "rides.events"
    assert json.loads(fields["envelope"]) == {"type": "match.shown", "x": 1}


async def test_fallo_de_xadd_no_propaga(caplog: pytest.LogCaptureFixture) -> None:
    redis = MagicMock()
    redis.xadd = AsyncMock(side_effect=ConnectionError("redis caído"))
    publisher = MatchShownPublisher(redis, stream="rides.events")

    with caplog.at_level(logging.ERROR):
        publisher.publish({"type": "match.shown", "aggregateId": REQUEST_ID})
        await publisher.drain()

    assert "match_shown_publish_failed" in caplog.text
    assert publisher.pending == 0
