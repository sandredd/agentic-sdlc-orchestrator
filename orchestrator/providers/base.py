"""The provider seam: how an agent talks to a language model, or doesn't.

Every agent depends on this interface, never on a concrete SDK. That is what
makes "pluggable providers" real rather than aspirational: swapping
:class:`DeterministicProvider` for an Anthropic-backed one is a config change,
not a code change, and the same agent code runs in CI (no network, fully
reproducible) and in a live demo (a real model, genuinely variable output).

The interface is deliberately narrow -- one method, free-text in, free-text
out. Agents that need structured data ask for JSON in the prompt and parse it
defensively (see :meth:`orchestrator.agents.base.Agent.think`); the provider
itself does not know or care about the domain schema. That keeps the provider
boundary stable even as agents' output contracts evolve.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(Exception):
    """A provider could not produce a completion. Agents are expected to
    catch this and fall back to a deterministic path rather than let a stage
    fail outright on a transient model/network problem -- that classification
    is exactly what `resilience.classify` already does for the retry loop."""


@dataclass(frozen=True)
class GenerationRequest:
    system: str
    prompt: str
    max_tokens: int = 4096
    temperature: float = 0.2  # low by default: engineering output, not prose


class Provider(ABC):
    """A source of model completions."""

    name: str = "provider"

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> str: ...

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int = 4096, temperature: float = 0.2
    ) -> str:
        """Convenience wrapper so call sites don't construct the dataclass by hand."""
        return await self.generate(
            GenerationRequest(
                system=system, prompt=prompt, max_tokens=max_tokens, temperature=temperature
            )
        )
