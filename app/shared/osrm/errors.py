"""
Excepciones del cliente OSRM.

- OsrmUnavailable → el servicio HTTP responde 503.
- OsrmNoSegment   → no hay calle cerca del punto; la búsqueda devuelve lista vacía.
- OsrmTooBig      → la petición excede --max-table-size (bug de partición).
"""
from __future__ import annotations


class OsrmError(Exception):
    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OsrmUnavailable(OsrmError):
    """Error de transporte, timeout o 5xx tras el reintento."""


class OsrmNoSegment(OsrmError):
    """OSRM no encontró un segmento de calle cerca de la coordenada."""


class OsrmTooBig(OsrmError):
    """La tabla excede OSRM_MAX_TABLE_SIZE."""
