"""The offline, reproducible default provider.

This is not a toy stand-in for "no provider configured" -- it is the provider
the whole system is designed to run well on. Every agent's real intelligence
lives in its own deterministic heuristics (parsing the requirement text,
templating code against a domain model); this provider's job is just to
participate honestly in the same interface a real model would, so an agent
never has two different code paths for "with LLM" and "without".

Concretely: when a prompt asks for JSON, this returns an empty object rather
than fabricating plausible-looking structured data it has no way to back --
:meth:`orchestrator.agents.base.Agent.think` treats a JSON payload with no
useful fields the same as a parse failure and falls back to the agent's own
heuristics. That is a deliberate choice over synthesizing a fake-plausible
answer: a prototype that quietly hallucinates structure is worse than one
that visibly declines and defers to code the author actually wrote and can
defend.
"""

from __future__ import annotations

import re

from orchestrator.providers.base import GenerationRequest, Provider

_JSON_HINT = re.compile(r"\bjson\b", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")

_STOPWORDS = frozenset(
    [
        "that", "this", "with", "from", "into", "your", "must", "should",
        "would", "could", "will", "shall", "have", "has", "been", "being",
        "were", "where", "when", "what", "which", "system", "service",
    ]
)


def _keywords(text: str, limit: int = 6) -> list[str]:
    seen: list[str] = []
    for word in _WORD.findall(text):
        lw = word.lower()
        if lw in _STOPWORDS or lw in seen:
            continue
        seen.append(lw)
        if len(seen) >= limit:
            break
    return seen


class DeterministicProvider(Provider):
    name = "deterministic"

    async def generate(self, request: GenerationRequest) -> str:
        if _JSON_HINT.search(request.system) or _JSON_HINT.search(request.prompt):
            return "{}"
        keywords = _keywords(request.prompt)
        focus = ", ".join(keywords) if keywords else "the stated requirement"
        return (
            f"[deterministic-provider] no language model is configured; "
            f"proceeding on rule-based analysis of: {focus}."
        )
