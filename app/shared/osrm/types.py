"""
Tipos del cliente OSRM.

Todo el código fuera de client.py usa LatLng(lat, lng); el orden lon,lat
de OSRM queda encapsulado en el cliente.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LatLng:
    lat: float
    lng: float


@dataclass(frozen=True, slots=True)
class SnappedPoint:
    location: LatLng
    distance_m: float  # metros entre el punto original y el ajustado


@dataclass(frozen=True, slots=True)
class Matrix:
    # Filas = sources, columnas = destinations. None = sin ruta posible.
    durations: list[list[float | None]]
    distances: list[list[float | None]]


@dataclass(frozen=True, slots=True)
class Route:
    duration_s: float
    distance_m: float
    geometry: dict  # GeoJSON LineString
