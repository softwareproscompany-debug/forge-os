"""Unit tests for GeminiProvider, OpenRouterProvider, OllamaProvider.

No network access: all HTTP is mocked via httpx.MockTransport.
"""

from __future__ import annotations

import httpx
import pytest

from forge_llm import (
    GenerationRequest,
    GeminiProvider,
    OllamaProvider,
    OpenRouterProvider,
    get_provider,
)


def _gemini_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": "Hello from Gemini"}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
            },
        },
    )


def _openai_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": "Hello from model"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 6},
        },
    )


def _mocked_async_client(
    monkeypatch: pytest.MonkeyPatch, handler
) -> list[httpx.Request]:
    """Patch httpx.AsyncClient so constructed clients use a MockTransport.

    Returns the list of requests seen by the transport.
    """
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return seen


# ---------------------------------------------------------------------------
# GeminiProvider
# ---------------------------------------------------------------------------


def test_gemini_success_parses_text_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _mocked_async_client(monkeypatch, _gemini_ok)
    provider = GeminiProvider(api_key="test-key")
    assert provider.name == "gemini"
    assert provider.model == "gemini-3.5-flash-lite"

    import asyncio

    result = asyncio.run(
        provider.generate(
            GenerationRequest(
                prompt="Say hi", system_prompt="Be brief", max_tokens=64
            )
        )
    )
    assert result.text == "Hello from Gemini"
    assert result.provider == "gemini"
    assert result.tokens_in == 10
    assert result.tokens_out == 5
    assert result.latency_ms >= 0

    # request shape: key in query param, system instruction in body
    assert len(seen) == 1
    req = seen[0]
    assert "generativelanguage.googleapis.com" in str(req.url)
    assert "gemini-3.5-flash-lite:generateContent" in str(req.url)
    assert req.url.params["key"] == "test-key"
    import json

    body = json.loads(req.content.decode())
    assert body["contents"][0]["parts"][0]["text"] == "Say hi"
    assert body["systemInstruction"]["parts"][0]["text"] == "Be brief"
    assert body["generationConfig"]["maxOutputTokens"] == 64


def test_gemini_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiProvider()


def test_gemini_model_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-pro")
    provider = GeminiProvider(api_key="test-key")
    assert provider.model == "gemini-pro"


def test_gemini_http_error_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad key"}})

    _mocked_async_client(monkeypatch, _bad)
    provider = GeminiProvider(api_key="bad-key")
    import asyncio

    with pytest.raises(RuntimeError, match=r"Gemini API call failed \(400\)"):
        asyncio.run(provider.generate(GenerationRequest(prompt="hi")))


def test_get_provider_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    provider = get_provider()
    assert isinstance(provider, GeminiProvider)
    assert provider.model == "gemini-3.5-flash-lite"


def test_get_provider_gemini_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        get_provider()


# ---------------------------------------------------------------------------
# OpenRouterProvider
# ---------------------------------------------------------------------------


def _openrouter_client(monkeypatch: pytest.MonkeyPatch):
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _openai_ok(request)

    transport = httpx.MockTransport(_handler)
    return httpx.AsyncClient(transport=transport), seen


def test_openrouter_success_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    client, seen = _openrouter_client(monkeypatch)
    provider = OpenRouterProvider(
        api_key="or-key", model="anthropic/claude-sonnet-4", client=client
    )
    assert provider.name == "openrouter"
    assert provider.base_url == "https://openrouter.ai/api/v1"

    import asyncio

    result = asyncio.run(
        provider.generate(GenerationRequest(prompt="hi"))
    )
    assert result.text == "Hello from model"
    assert result.tokens_in == 12
    assert result.tokens_out == 6

    req = seen[0]
    assert str(req.url) == (
        "https://openrouter.ai/api/v1/chat/completions"
    )
    assert req.headers["HTTP-Referer"] == "https://forgeos.app"
    assert req.headers["X-Title"] == "ForgeOS"
    assert req.headers["Authorization"] == "Bearer or-key"


def test_openrouter_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider(model="x/y")


def test_openrouter_missing_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_MODEL"):
        OpenRouterProvider(api_key="or-key")


def test_openrouter_http_error_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid key")

    transport = httpx.MockTransport(_bad)
    provider = OpenRouterProvider(
        api_key="bad",
        model="x/y",
        client=httpx.AsyncClient(transport=transport),
    )
    import asyncio

    with pytest.raises(RuntimeError, match=r"OpenAI-compatible call failed \(401\)"):
        asyncio.run(provider.generate(GenerationRequest(prompt="hi")))


def test_get_provider_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "x/y")
    provider = get_provider()
    assert isinstance(provider, OpenRouterProvider)
    assert provider.model == "x/y"


# ---------------------------------------------------------------------------
# OllamaProvider
# ---------------------------------------------------------------------------


def test_ollama_success_no_key_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, seen = _openrouter_client(monkeypatch)
    provider = OllamaProvider(model="llama3.1", client=client)
    assert provider.name == "ollama"
    assert provider.base_url == "http://localhost:11434/v1"

    import asyncio

    result = asyncio.run(
        provider.generate(GenerationRequest(prompt="hi"))
    )
    assert result.text == "Hello from model"

    req = seen[0]
    assert str(req.url) == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in req.headers  # no key sent


def test_ollama_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/v1")
    client, _ = _openrouter_client(monkeypatch)
    provider = OllamaProvider(model="llama3.1", client=client)
    assert provider.base_url == "http://gpu-box:11434/v1"


def test_ollama_missing_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    with pytest.raises(ValueError, match="OLLAMA_MODEL"):
        OllamaProvider()


def test_ollama_http_error_surfaces() -> None:
    def _bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model not found")

    transport = httpx.MockTransport(_bad)
    provider = OllamaProvider(
        model="nope", client=httpx.AsyncClient(transport=transport)
    )
    import asyncio

    with pytest.raises(RuntimeError, match=r"OpenAI-compatible call failed \(500\)"):
        asyncio.run(provider.generate(GenerationRequest(prompt="hi")))


def test_get_provider_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    provider = get_provider()
    assert isinstance(provider, OllamaProvider)
    assert provider.base_url == "http://localhost:11434/v1"
