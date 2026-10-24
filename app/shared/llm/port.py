"""
Puerto (Protocol) del proveedor LLM.

El handler y el repository solo conocen este protocolo.
boto3 no se importa aquí ni en ningún módulo que solo use el puerto.

Contrato:
- extract_preferences: puede lanzar ValidationError de Pydantic si el LLM
  devuelve datos inválidos incluso tras parsing interno. El caller es quien
  gestiona reintentos y DLQ.
- embed: retorna un vector de floats de longitud settings.EMBED_DIM.
  No lanza ante throttling — el proveedor maneja reintentos internamente.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.features.preferences.schema import StructuredPreferences


@runtime_checkable
class LLMProvider(Protocol):
    def extract_preferences(self, raw_text: str) -> StructuredPreferences:
        """
        Extrae preferencias estructuradas desde texto libre.

        Args:
            raw_text: texto libre escrito por el usuario.

        Returns:
            StructuredPreferences validado.

        Raises:
            pydantic.ValidationError: si la respuesta del LLM no pasa validación.
            Exception: errores de red no recuperables (tras agotar reintentos internos).
        """
        ...

    def embed(self, text: str) -> list[float]:
        """
        Genera un embedding del texto.

        Args:
            text: texto a embeber (normalmente raw_text).

        Returns:
            Lista de floats de longitud EMBED_DIM.
        """
        ...
