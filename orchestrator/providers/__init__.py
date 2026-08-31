"""Provider factory: turns config into a concrete `Provider` without agents
ever importing a specific SDK."""

from __future__ import annotations

from orchestrator.config import OrchestratorConfig
from orchestrator.providers.base import GenerationRequest, Provider, ProviderError
from orchestrator.providers.deterministic import DeterministicProvider

__all__ = [
    "GenerationRequest",
    "Provider",
    "ProviderError",
    "DeterministicProvider",
    "get_provider",
]


def get_provider(config: OrchestratorConfig) -> Provider:
    if config.provider == "anthropic":
        import os

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ProviderError(
                "provider='anthropic' but ANTHROPIC_API_KEY is not set; "
                "export it or switch config.provider to 'deterministic'"
            )
        from orchestrator.providers.anthropic_client import AnthropicProvider

        return AnthropicProvider(api_key=api_key, model=config.model)

    return DeterministicProvider()
