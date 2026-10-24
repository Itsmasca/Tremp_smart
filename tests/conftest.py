"""
Fixtures compartidos para todos los tests.
"""
from __future__ import annotations

import pytest

from app.features.preferences.schema import StructuredPreferences


@pytest.fixture()
def valid_structured_preferences() -> StructuredPreferences:
    """Objeto StructuredPreferences válido para usar en tests."""
    return StructuredPreferences(
        gender_preference="any",
        allow_pets=True,
        allow_smoking=False,
        allow_music=None,
        max_detour_minutes=10,
        preferred_areas=["Altabrisa"],
        avoided_areas=[],
        time_windows=[],
        avoid_strangers_at_night=None,
        notes=None,
        confidence=0.9,
    )
