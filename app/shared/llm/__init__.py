"""
Factory del proveedor LLM.

Usar get_llm_provider() para obtener la instancia correcta según configuración.
"""
from __future__ import annotations

from app.shared.config import Settings
from app.shared.llm.port import LLMProvider


def get_llm_provider(settings: Settings) -> LLMProvider:
    """
    Retorna BedrockProvider o FakeLLMProvider según settings.LLM_PROVIDER.

    Los imports están dentro de la función para que el módulo bedrock (que importa
    boto3) solo se cargue cuando se elige el proveedor real.
    """
    if settings.LLM_PROVIDER == "bedrock":
        from app.shared.llm.bedrock import BedrockProvider
        return BedrockProvider()

    from app.shared.llm.fake import FakeLLMProvider
    return FakeLLMProvider()
