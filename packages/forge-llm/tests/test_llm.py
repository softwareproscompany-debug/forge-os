"""Unit tests for forge_llm. No network access; all providers are stubbed/mocked."""

from __future__ import annotations

import pytest

from forge_llm import (
    AnthropicProvider,
    GenerationRequest,
    OpenAICompatibleProvider,
    PromptRegistry,
    PromptRenderError,
    StubProvider,
    build_brand_system_prompt,
    check_guardrails,
    default_registry,
    ensure_affiliate_disclosure,
    estimate_cost,
    get_provider,
    has_affiliate_disclosure,
    looks_like_affiliate_content,
    render,
)
from forge_llm.brand import AFFILIATE_DISCLOSURE


# ---------------------------------------------------------------------------
# StubProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_provider_is_deterministic() -> None:
    provider = StubProvider()
    req = GenerationRequest(prompt="Write a launch email", variables={"headline": "Big launch"})
    first = await provider.generate(req)
    second = await provider.generate(req)
    assert first.text == second.text
    assert first.provider == "stub"
    assert first.model == "stub"
    assert first.cost_usd == 0.0
    assert first.tokens_in > 0 and first.tokens_out > 0


@pytest.mark.asyncio
async def test_stub_provider_varies_with_prompt() -> None:
    provider = StubProvider()
    a = await provider.generate(GenerationRequest(prompt="email about spring sale"))
    b = await provider.generate(GenerationRequest(prompt="sms about winter clearance"))
    # Same seed space is astronomically unlikely to collide on different prompts;
    # assert on the derived topic echo to keep the test meaningful.
    assert "spring sale" in a.text
    assert "winter clearance" in b.text


@pytest.mark.asyncio
async def test_stub_provider_uses_variables() -> None:
    provider = StubProvider()
    result = await provider.generate(
        GenerationRequest(
            prompt="promo",
            variables={"headline": "Half-price rowers", "cta_url": "https://shop.example/r"},
        )
    )
    assert "Half-price rowers" in result.text
    assert "https://shop.example/r" in result.text


# ---------------------------------------------------------------------------
# PromptRegistry
# ---------------------------------------------------------------------------


def test_registry_render_with_defaults() -> None:
    registry = PromptRegistry()
    registry.register("greet", "Hello {{ name }}!", defaults={"name": "friend"})
    assert registry.render("greet", {}) == "Hello friend!"
    assert registry.render("greet", {"name": "Ada"}) == "Hello Ada!"


def test_registry_unknown_template_keyerror_lists_available() -> None:
    registry = PromptRegistry()
    registry.register("only_one", "x")
    with pytest.raises(KeyError) as excinfo:
        registry.render("nope", {})
    assert "nope" in str(excinfo.value)
    assert "only_one" in str(excinfo.value)


def test_registry_missing_variable_raises_clear_error() -> None:
    registry = PromptRegistry()
    registry.register("needs_two", "{{ first }} and {{ second }}")
    with pytest.raises(PromptRenderError) as excinfo:
        registry.render("needs_two", {"first": "a"})
    # PromptRenderError is a KeyError, and the message names the template.
    assert isinstance(excinfo.value, KeyError)
    assert "needs_two" in str(excinfo.value)


def test_registry_rejects_bad_template_syntax() -> None:
    registry = PromptRegistry()
    with pytest.raises(ValueError):
        registry.register("broken", "{{ unclosed ")


def test_builtin_templates_render() -> None:
    subject = render("email_subject", {"offer_headline": "50% off rowers"})
    assert subject == "50% off rowers — from Forge"

    body = render(
        "email_body",
        {
            "intro": "Spring sale is live.",
            "offer_details": "Save on every rower.",
            "cta_text": "Shop now",
            "cta_url": "https://example.com",
            "unsubscribe_url": "https://example.com/unsub",
        },
    )
    assert "Hi there," in body  # default first_name
    assert "Unsubscribe anytime" in body

    sms = render("sms_body", {"message": "Sale ends tonight"})
    assert sms.startswith("Forge: Sale ends tonight")
    assert "STOP" in sms

    post = render("social_post", {"hook": "Look at this", "body": "Details here"})
    assert "Look at this" in post

    assert set(default_registry.names) >= {"email_subject", "email_body", "sms_body", "social_post"}


def test_builtin_template_missing_variable() -> None:
    with pytest.raises(PromptRenderError):
        render("email_subject", {})  # offer_headline has no default


# ---------------------------------------------------------------------------
# Brand helpers
# ---------------------------------------------------------------------------


def _brand(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "voice_description": "Plainspoken and warm.",
        "tone_tags": ["friendly", "direct"],
        "icp_description": "Busy ops managers.",
        "do_list": ["Use short sentences."],
        "dont_list": ["leverage", "synergy"],
    }
    base.update(overrides)
    return base


def test_build_brand_system_prompt_weaves_sections() -> None:
    prompt = build_brand_system_prompt(_brand())
    for needle in (
        "Plainspoken and warm.",
        "friendly",
        "Busy ops managers.",
        "Use short sentences.",
        "leverage",
    ):
        assert needle in prompt


def test_build_brand_system_prompt_tolerates_sparse_brand() -> None:
    prompt = build_brand_system_prompt({})
    assert "marketing copywriter" in prompt


def test_check_guardrails_flags_dont_list_phrase() -> None:
    violations = check_guardrails("We leverage synergy daily.", _brand())
    assert any("leverage" in v for v in violations)
    assert any("synergy" in v for v in violations)


def test_check_guardrails_case_insensitive() -> None:
    violations = check_guardrails("We LEVERAGE everything.", _brand())
    assert violations, "expected a dont_list violation"


def test_check_guardrails_flags_excessive_caps() -> None:
    violations = check_guardrails(
        "BUY NOW LIMITED TIME OFFER ACT FAST BEFORE IT IS GONE", _brand()
    )
    assert any("capitalization" in v for v in violations)


def test_check_guardrails_missing_unsubscribe_for_email() -> None:
    violations = check_guardrails("Hello, here is our newsletter.", _brand(channel="email"))
    assert any("unsubscribe" in v for v in violations)


def test_check_guardrails_unsubscribe_present_passes() -> None:
    violations = check_guardrails(
        "Hello. Unsubscribe: https://example.com/u", _brand(channel="email")
    )
    assert not any("unsubscribe hint" in v.lower() for v in violations)


def test_check_guardrails_clean_text() -> None:
    violations = check_guardrails(
        "Hi there — our spring catalog is out. Thanks for reading!",
        _brand(channel="social"),
    )
    assert violations == []


# ---------------------------------------------------------------------------
# Costing
# ---------------------------------------------------------------------------


def test_estimate_cost_sonnet() -> None:
    # $3/1M in + $15/1M out
    assert estimate_cost("claude-sonnet-4-5-20250929", 1_000_000, 1_000_000) == pytest.approx(18.0)


def test_estimate_cost_opus_and_haiku() -> None:
    assert estimate_cost("claude-opus-4-1", 1_000_000, 0) == pytest.approx(15.0)
    assert estimate_cost("claude-haiku-4-5", 0, 1_000_000) == pytest.approx(5.0)


def test_estimate_cost_gpt4o_family() -> None:
    assert estimate_cost("gpt-4o", 1_000_000, 1_000_000) == pytest.approx(12.5)
    # mini must not be swallowed by the gpt-4o prefix
    assert estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)


def test_estimate_cost_unknown_model_is_zero() -> None:
    assert estimate_cost("some-future-model", 100, 100) == 0.0
    assert estimate_cost("stub", 100, 100) == 0.0


# ---------------------------------------------------------------------------
# get_provider selection
# ---------------------------------------------------------------------------


def test_get_provider_defaults_to_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert isinstance(get_provider(), StubProvider)


def test_get_provider_stub_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    assert isinstance(get_provider(), StubProvider)


def test_get_provider_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    provider = get_provider()
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-sonnet-4-5-20250929"


def test_get_provider_anthropic_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_provider()


def test_get_provider_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "llama3")
    provider = get_provider()
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "llama3"


def test_get_provider_openai_compatible_missing_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "llama3")
    with pytest.raises(ValueError, match="OPENAI_COMPAT_BASE_URL"):
        get_provider()


def test_get_provider_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "watson")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_provider()


# ---------------------------------------------------------------------------
# Affiliate disclosure guardrail (FTC)
# ---------------------------------------------------------------------------


def test_check_guardrails_flags_missing_affiliate_disclosure() -> None:
    violations = check_guardrails(
        "Check out this espresso maker: https://shop.example/r/maker",
        _brand(is_affiliate_content=True),
    )
    assert any("affiliate disclosure" in v for v in violations)


def test_check_guardrails_affiliate_disclosure_present_passes() -> None:
    text = (
        "Check out this espresso maker: https://shop.example/r/maker\n\n"
        "Disclosure: this post contains affiliate links."
    )
    violations = check_guardrails(text, _brand(is_affiliate_content=True))
    assert not any("affiliate disclosure" in v for v in violations)


def test_check_guardrails_no_affiliate_flag_no_disclosure_needed() -> None:
    violations = check_guardrails("Just a regular newsletter.", _brand())
    assert not any("affiliate" in v for v in violations)


def test_has_affiliate_disclosure_matches_phrases() -> None:
    assert has_affiliate_disclosure("As an Amazon Associate I earn.")
    assert has_affiliate_disclosure("We may earn a commission here.")
    assert not has_affiliate_disclosure("Buy now, limited offer!")


def test_looks_like_affiliate_content_detects_short_links() -> None:
    assert looks_like_affiliate_content("Grab it here: https://api.example/r/deal-1")
    assert not looks_like_affiliate_content("Visit https://example.com/deals today")


def test_ensure_affiliate_disclosure_appends_once() -> None:
    text, appended = ensure_affiliate_disclosure("Great deal on grinders.")
    assert appended is True
    assert text.endswith(AFFILIATE_DISCLOSURE)
    # Idempotent: never appends twice.
    text2, appended2 = ensure_affiliate_disclosure(text)
    assert appended2 is False
    assert text2 == text
    assert text2.count(AFFILIATE_DISCLOSURE) == 1


def test_ensure_affiliate_disclosure_skips_when_present() -> None:
    text, appended = ensure_affiliate_disclosure(
        "Deal! Disclosure: this contains affiliate links."
    )
    assert appended is False
