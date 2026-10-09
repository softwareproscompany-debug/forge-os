"""LLM provider implementations for ForgeOS.

Providers:

* :class:`StubProvider` — deterministic, zero-credential provider used in dev/test.
  The output copy is derived from the SHA-256 hash of the prompt, so the same
  prompt always yields the same output. Reports zero cost.
* :class:`AnthropicProvider` — calls the Anthropic Messages API via the official
  ``anthropic`` SDK (async client). Requires ``ANTHROPIC_API_KEY``.
* :class:`OpenAICompatibleProvider` — POSTs OpenAI-style ``/chat/completions``
  payloads to any compatible endpoint (``OPENAI_COMPAT_BASE_URL``).
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from forge_llm.costing import estimate_cost


class LLMProvider(Protocol):
    """Contract every LLM provider must satisfy."""

    name: str

    async def generate(self, req: "GenerationRequest") -> "GenerationResult": ...


if TYPE_CHECKING:  # pragma: no cover - avoids a runtime circular import
    from forge_llm import GenerationRequest, GenerationResult


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token); used only by the stub provider."""
    return max(1, len(text) // 4)


class StubProvider:
    """Deterministic stub provider: no network, no keys, zero cost.

    The generated copy is picked from a small set of marketing templates using
    the SHA-256 hash of ``system_prompt + prompt`` as the seed, so identical
    prompts always produce identical output. This lets the whole
    generate -> guardrail -> approve -> send pipeline run end-to-end in dev.
    """

    name = "stub"
    model = "stub"

    _TEMPLATES: tuple[str, ...] = (
        "{headline}\n\n{lede}\n\n{value}\n\n{cta}",
        "Introducing: {headline}\n\n{lede}\n{value}\n\n{cta}",
        "{headline} — {lede}\n\n{value}\n\n{cta}",
    )

    _CTAS: tuple[str, ...] = (
        "Learn more: {cta_url}",
        "Get started today: {cta_url}",
        "Claim your spot: {cta_url}",
        "See how it works: {cta_url}",
    )

    def _seed(self, req: "GenerationRequest") -> int:
        digest = hashlib.sha256(
            f"{req.system_prompt}\n{req.prompt}".encode("utf-8")
        ).hexdigest()
        return int(digest[:16], 16)

    def _derive_copy(self, req: "GenerationRequest") -> str:
        rng = random.Random(self._seed(req))
        words = req.prompt.split()
        topic = " ".join(words[:10]) + ("…" if len(words) > 10 else "")
        headline = str(
            req.variables.get("headline") or (words[0].title() + " " + " ".join(words[1:4]) if words else "New from us")
        )
        lede = str(
            req.variables.get("lede")
            or f"Here's what you need to know: {topic}."
        )
        value = str(
            req.variables.get("value_prop")
            or "Built for busy teams that want results without the busywork."
        )
        cta_url = str(req.variables.get("cta_url") or "https://example.com")
        cta = rng.choice(self._CTAS).format(cta_url=cta_url)
        template = self._TEMPLATES[rng.randrange(len(self._TEMPLATES))]
        return template.format(headline=headline, lede=lede, value=value, cta=cta)

    async def generate(self, req: "GenerationRequest") -> "GenerationResult":
        from forge_llm import GenerationResult  # deferred: keeps module import light

        started = time.perf_counter()
        text = self._derive_copy(req)
        latency_ms = int((time.perf_counter() - started) * 1000)
        tokens_in = _estimate_tokens(req.system_prompt + "\n" + req.prompt)
        tokens_out = _estimate_tokens(text)
        return GenerationResult(
            text=text,
            provider=self.name,
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=0.0,  # stub generations are always free
            latency_ms=latency_ms,
        )


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class AnthropicProvider:
    """Anthropic Messages API provider (async, via the official SDK)."""

    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        key = (api_key if api_key is not None else _get_env("ANTHROPIC_API_KEY"))
        if not key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. Set it to your Anthropic API key "
                "to use LLM_PROVIDER=anthropic, or use LLM_PROVIDER=stub for local dev."
            )
        self._api_key = key
        self.model = (
            model
            if model is not None
            else _get_env("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
        )

    def _client(self) -> Any:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise RuntimeError(
                "The 'anthropic' package is not installed. "
                "Install it with: pip install forge-llm"
            ) from exc
        return AsyncAnthropic(api_key=self._api_key)

    async def generate(self, req: "GenerationRequest") -> "GenerationResult":
        from forge_llm import GenerationResult

        started = time.perf_counter()
        try:
            response = await self._client().messages.create(
                model=self.model,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                system=req.system_prompt or None,
                messages=[{"role": "user", "content": req.prompt}],
            )
        except Exception as exc:
            raise RuntimeError(f"Anthropic API call failed (model={self.model}): {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        tokens_in = int(response.usage.input_tokens or 0)
        tokens_out = int(response.usage.output_tokens or 0)
        return GenerationResult(
            text=text,
            provider=self.name,
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=estimate_cost(self.model, tokens_in, tokens_out),
            latency_ms=latency_ms,
        )


class OpenAICompatibleProvider:
    """Any OpenAI-compatible ``/chat/completions`` endpoint (e.g. local servers)."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_base = (
            base_url if base_url is not None else _get_env("OPENAI_COMPAT_BASE_URL")
        ).rstrip("/")
        if not resolved_base:
            raise ValueError(
                "OPENAI_COMPAT_BASE_URL is not set. Set it to the base URL of your "
                "OpenAI-compatible server (e.g. http://localhost:8000/v1)."
            )
        resolved_model = model if model is not None else _get_env("OPENAI_COMPAT_MODEL")
        if not resolved_model:
            raise ValueError(
                "OPENAI_COMPAT_MODEL is not set. Set it to the model name served by "
                "your OpenAI-compatible endpoint."
            )
        self.base_url = resolved_base
        self.model = resolved_model
        self._api_key = api_key if api_key is not None else _get_env("OPENAI_COMPAT_API_KEY")
        self._client = client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def generate(self, req: "GenerationRequest") -> "GenerationResult":
        from forge_llm import GenerationResult

        messages: list[dict[str, str]] = []
        if req.system_prompt:
            messages.append({"role": "system", "content": req.system_prompt})
        messages.append({"role": "user", "content": req.prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        started = time.perf_counter()
        client = self._client or httpx.AsyncClient(timeout=120.0)
        close_client = self._client is None
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"OpenAI-compatible call failed ({response.status_code}) "
                    f"at {self.base_url}: {response.text[:500]}"
                ) from exc
            data = response.json()
        finally:
            if close_client:
                await client.aclose()
        latency_ms = int((time.perf_counter() - started) * 1000)
        choices = data.get("choices") or []
        text = ""
        if choices:
            text = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        return GenerationResult(
            text=text,
            provider=self.name,
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=estimate_cost(self.model, tokens_in, tokens_out),
            latency_ms=latency_ms,
        )
