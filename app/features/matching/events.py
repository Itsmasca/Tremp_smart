"""
Evento match.shown — lo que se mostró a quién, en qué orden y con qué modelo.

Contrato: specs/001-osrm-matching/contracts/match-shown.event.schema.json.
Se publica en rides.events (campo `envelope`, mismo formato que los eventos
de crud-ts) sin bloquear la respuesta: un fallo se registra y se descarta.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.features.matching.schema import MatchRequest, ScoredCandidate
from app.shared.osrm.types import SnappedPoint

logger = logging.getLogger(__name__)


def build_match_shown(
    req: MatchRequest,
    model_version: str,
    ranked: list[ScoredCandidate],
    snapped_p: SnappedPoint,
    snapped_d: SnappedPoint,
) -> dict[str, Any]:
    request_id = str(req.requestId)
    return {
        "id": str(uuid.uuid4()),
        "type": "match.shown",
        "version": 1,
        "source": "matching-py",
        "aggregateType": "match_request",
        "aggregateId": request_id,
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "data": {
            "requestId": request_id,
            "passengerId": str(req.passengerId),
            "modelVersion": model_version,
            "departureAt": req.departureAt.isoformat(),
            "timeWindowMin": req.timeWindowMin,
            "pickup": {"lat": snapped_p.location.lat, "lng": snapped_p.location.lng},
            "dropoff": {"lat": snapped_d.location.lat, "lng": snapped_d.location.lng},
            "candidates": [
                {
                    "rideId": c.detour.ride.id,
                    "rank": c.rank,
                    "score": c.score,
                    "detourS": round(c.detour.detour_s_raw),
                    "pickupEtaS": round(c.detour.t_ap_s),
                    "features": c.features,
                }
                for c in ranked
            ],
        },
    }


class MatchShownPublisher:
    """
    Publica fire-and-forget. Guarda referencia a cada task para que el GC
    no la cancele, y registra su excepción al terminar.
    """

    def __init__(self, redis: Any, stream: str) -> None:
        self._redis = redis
        self._stream = stream
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def publish(self, envelope: dict[str, Any]) -> None:
        task = asyncio.create_task(self._xadd(envelope))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout_s: float | None = None) -> None:
        """Espera las publicaciones pendientes (tests y apagado ordenado)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout_s)

    async def _xadd(self, envelope: dict[str, Any]) -> None:
        try:
            await self._redis.xadd(self._stream, {"envelope": json.dumps(envelope)})
        except Exception:
            logger.exception(
                "match_shown_publish_failed | requestId=%s stream=%s",
                envelope.get("aggregateId"),
                self._stream,
            )
