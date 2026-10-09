"""forge_llm — LLM abstraction layer for ForgeOS.

Provides a provider-agnostic interface for marketing-copy generation:

* :class:`GenerationRequest` / :class:`GenerationResult` — the request/response contract
* :func:`get_provider` — selects a provider from the ``LLM_PROVIDER`` env var
* :mod:`forge_llm.providers` — ``stub`` / ``anthropic`` / ``openai_compatible`` providers
* :mod:`forge_llm.prompts` — Jinja2 prompt-template registry with built-in templates
* :mod:`forge_llm.brand` — brand-kit system prompts + guardrail checks
* :mod:`forge_llm.costing` — per-model USD cost estimation
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from forge_llm.brand import build_brand_system_prompt, check_guardrails
from forge_llm.costing import PRICES_USD_PER_MTOK, estimate_cost
from forge_llm.prompts import (
    BUILTIN_TEMPLATE_NAMES,
    PromptRegistry,
    PromptRenderError,
    default_registry,
    render,
)
from forge_llm.providers import (
    AnthropicProvider,
    LLMProvider,
    OpenAICompatibleProvider,
    StubProvider,
)


@dataclass
class GenerationRequest:
    """Input to :meth:`LLMProvider.generate`."""

    prompt: str
    system_prompt: str = ""
    max_tokens: int = 800
    temperature: float = 0.7
    variables: dict = field(default_factory=dict)


@dataclass
class GenerationResult:
    """Output of :meth:`LLMProvider.generate`."""

    text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int


def get_provider() -> LLMProvider:
    """Return the configured :class:`LLMProvider`.

    Reads ``LLM_PROVIDER`` (``stub`` | ``anthropic`` | ``openai_compatible``,
    default ``stub``). Raises :class:`ValueError` for unknown values.
    """
    name = os.environ.get("LLM_PROVIDER", "stub").strip().lower()
    if name == "stub":
        return StubProvider()
    if name == "anthropic":
        return AnthropicProvider()
    if name == "openai_compatible":
        return OpenAICompatibleProvider()
    raise ValueError(
        f"Unknown LLM_PROVIDER={name!r}; expected one of: stub, anthropic, openai_compatible"
    )


__all__ = [
    "AnthropicProvider",
    "BUILTIN_TEMPLATE_NAMES",
    "GenerationRequest",
    "GenerationResult",
    "LLMProvider",
    "OpenAICompatibleProvider",
    "PRICES_USD_PER_MTOK",
    "PromptRegistry",
    "PromptRenderError",
    "StubProvider",
    "build_brand_system_prompt",
    "check_guardrails",
    "default_registry",
    "estimate_cost",
    "get_provider",
    "render",
]
