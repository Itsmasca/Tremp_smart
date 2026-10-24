"""
Proveedor LLM falso para desarrollo local y tests.

No requiere credenciales AWS. Activar con LLM_PROVIDER=fake.
"""
from __future__ import annotations

from pydantic import ValidationError

from app.features.preferences.schema import StructuredPreferences
from app.shared.config import settings


# Respuesta fija que sirve como default determinista
_DEFAULT_RESPONSE = StructuredPreferences(
    gender_preference="any",
    allow_pets=None,
    allow_smoking=None,
    allow_music=None,
    max_detour_minutes=None,
    preferred_areas=[],
    avoided_areas=[],
    time_windows=[],
    avoid_strangers_at_night=None,
    notes=None,
    confidence=1.0,
)


class FakeLLMProvider:
    """
    Proveedor determinista para tests y desarrollo local.

    Args:
        response_override: Si se proporciona, extract_preferences siempre
            retorna este objeto en lugar del default.
        embed_dim: Dimensión del vector de embeddings (default: settings.EMBED_DIM).
    """

    def __init__(
        self,
        response_override: StructuredPreferences | None = None,
        embed_dim: int | None = None,
    ) -> None:
        self._response = response_override or _DEFAULT_RESPONSE
        self._embed_dim = embed_dim or settings.EMBED_DIM

    def extract_preferences(self, raw_text: str) -> StructuredPreferences:  # noqa: ARG002
        return self._response

    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.0] * self._embed_dim


class FailOnceFakeLLMProvider:
    """
    Proveedor que falla con ValidationError en el primer llamado y
    retorna una respuesta válida en el segundo.

    Útil para testear la lógica de reintento del handler.
    """

    def __init__(
        self,
        success_response: StructuredPreferences | None = None,
        embed_dim: int | None = None,
    ) -> None:
        self._success_response = success_response or _DEFAULT_RESPONSE
        self._embed_dim = embed_dim or settings.EMBED_DIM
        self._call_count = 0

    def extract_preferences(self, raw_text: str) -> StructuredPreferences:  # noqa: ARG002
        self._call_count += 1
        if self._call_count == 1:
            # Simula una ValidationError de Pydantic
            try:
                StructuredPreferences.model_validate({"confidence": 99.0})  # valor inválido
            except ValidationError as exc:
                raise exc
        return self._success_response

    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.0] * self._embed_dim


class AlwaysFailFakeLLMProvider:
    """
    Proveedor que siempre lanza ValidationError.
    Útil para testear el flujo de DLQ.
    """

    def __init__(self, embed_dim: int | None = None) -> None:
        self._embed_dim = embed_dim or settings.EMBED_DIM

    def extract_preferences(self, raw_text: str) -> StructuredPreferences:  # noqa: ARG002
        try:
            StructuredPreferences.model_validate({"confidence": 99.0})
        except ValidationError as exc:
            raise exc
        # Nunca llega aquí, pero satisface el type checker
        raise RuntimeError("unreachable")

    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.0] * self._embed_dim
