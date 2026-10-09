# forge-llm

LLM abstraction layer for ForgeOS: provider-agnostic marketing-copy generation,
a Jinja2 prompt registry, brand-kit helpers, and per-model cost estimation.

## Install

```bash
pip install -e packages/forge-llm
pip install -e "packages/forge-llm[dev]"   # tests
```

## Quick start

```python
import asyncio
from forge_llm import GenerationRequest, get_provider, render, check_guardrails

async def main():
    provider = get_provider()  # LLM_PROVIDER env: stub | anthropic | openai_compatible
    result = await provider.generate(GenerationRequest(
        prompt="Write a launch email for our spring sale",
        system_prompt="You write punchy B2B copy.",
    ))
    print(result.text, result.cost_usd)

asyncio.run(main())

# Render a built-in prompt template
subject = render("email_subject", {"offer_headline": "50% off rowers"})
```

## Providers

| `LLM_PROVIDER` | Class | Env vars |
|---|---|---|
| `stub` (default) | `StubProvider` | none — deterministic, zero cost, no network |
| `anthropic` | `AnthropicProvider` | `ANTHROPIC_API_KEY` (required), `ANTHROPIC_MODEL` (default `claude-sonnet-4-5-20250929`) |
| `openai_compatible` | `OpenAICompatibleProvider` | `OPENAI_COMPAT_BASE_URL` (required), `OPENAI_COMPAT_MODEL` (required), `OPENAI_COMPAT_API_KEY` (optional) |

`GenerationRequest`: `prompt`, `system_prompt=""`, `max_tokens=800`,
`temperature=0.7`, `variables={}`.
`GenerationResult`: `text`, `provider`, `model`, `tokens_in`, `tokens_out`,
`cost_usd`, `latency_ms`.

The stub provider derives its copy from the SHA-256 hash of the prompt, so the
same prompt always yields the same output — the full
generate → guardrail → approve → send pipeline runs end-to-end with zero keys.

## Prompt registry

```python
from forge_llm import PromptRegistry

registry = PromptRegistry()
registry.register("welcome", "Hi {{ name }}!", defaults={"name": "there"})
registry.render("welcome", {})            # "Hi there!"
registry.render("nope", {})               # KeyError: lists available templates
registry.render("welcome", {"x": 1})      # works; extra vars ignored
```

Built-in templates on `forge_llm.default_registry` (also via `forge_llm.render`):
`email_subject`, `email_body`, `sms_body`, `social_post`. Rendering with a
missing variable raises `PromptRenderError` (a `KeyError`) naming the template.

## Brand helpers

```python
from forge_llm import build_brand_system_prompt, check_guardrails

brand = {
    "voice_description": "Plainspoken and warm.",
    "tone_tags": ["friendly"],
    "icp_description": "Busy ops managers.",
    "do_list": ["Use short sentences."],
    "dont_list": ["leverage"],
    "channel": "email",  # enables the unsubscribe-hint check
}
system = build_brand_system_prompt(brand)
violations = check_guardrails("We leverage synergy! " * 10, brand)
```

`check_guardrails` flags (case-insensitive): `dont_list` phrases present in the
text, excessive capitalization (>60% uppercase letters), and a missing
unsubscribe/opt-out hint for email copy.

## Costing

```python
from forge_llm import estimate_cost
estimate_cost("claude-sonnet-4-5-20250929", 1_000_000, 1_000_000)  # 18.0 USD
```

Price table (`PRICES_USD_PER_MTOK`, USD per 1M tokens in/out): `claude-opus-4-1`
15/75, `claude-sonnet-4-5` 3/15, `claude-haiku` 1/5, `gpt-4o` 2.5/10,
`gpt-4o-mini` 0.15/0.6. Unknown models → `0.0`.

## Tests

```bash
cd packages/forge-llm && python -m pytest
```
