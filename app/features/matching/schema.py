"""
Esquemas del slice matching.

- Contrato HTTP con crud-ts (camelCase, no cambiar sin versionar):
  specs/001-osrm-matching/contracts/match-api.openapi.yaml
- Modelos internos por request (dataclasses, en memoria): ver data-model.md §3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.shared.osrm.types import LatLng


# ── Contrato HTTP ──────────────────────────────────────────────────────────────

class LatLngIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)

    def to_latlng(self) -> LatLng:
        return LatLng(lat=self.lat, lng=self.lng)

    @classmethod
    def from_latlng(cls, p: LatLng) -> LatLngIn:
        return cls(lat=p.lat, lng=p.lng)


class MatchRequest(BaseModel):
    requestId: UUID
    passengerId: UUID
    pickup: LatLngIn
    dropoff: LatLngIn
    departureAt: AwareDatetime
    timeWindowMin: int = Field(default=30, ge=0)
    limit: int = Field(default=10, ge=1)


class CandidateOut(BaseModel):
    rideId: UUID
    rank: int
    score: float
    detourS: int
    pickupEtaS: int
    snappedPickup: LatLngIn
    snappedDropoff: LatLngIn
    routeGeometry: dict | None = None


class MatchResponse(BaseModel):
    requestId: UUID
    modelVersion: str
    candidates: list[CandidateOut]


# ── Modelos internos ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RideRow:
    """Fila de `rides` ya filtrada por la query de candidatos (solo lectura)."""

    id: str
    driver_id: str
    origin: LatLng
    destination: LatLng
    duration_s: float
    distance_m: float
    departure_at: datetime
    seats_available: int
    max_detour_s: float
    corridor_dist_m: float


@dataclass(slots=True)
class DetourResult:
    """Candidato viable tras la matriz OSRM."""

    ride: RideRow
    t_ap_s: float
    t_pd_s: float
    t_db_s: float
    d_ap_m: float | None
    d_pd_m: float | None
    d_db_m: float | None
    detour_s_raw: float          # puede ser < 0 por diferencias de grafo
    detour_limit_s: float        # min(ride.max_detour_s, prefs.max_detour_minutes*60)

    @property
    def detour_s(self) -> float:
        """Desvío usado para filtro y score (negativos → 0)."""
        return max(self.detour_s_raw, 0.0)


@dataclass(slots=True)
class ScoredCandidate:
    detour: DetourResult
    score: float
    features: dict = field(default_factory=dict)
    rank: int | None = None
    route_geometry: dict | None = None
