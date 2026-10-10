"""Draven — voice-first executive assistant API (``/draven``).

``POST /draven/chat`` routes a natural-language message to the typed tool
registry (``app/draven_tools.py``): low-risk tools execute immediately,
medium/high-risk tools return structured approval requests that never
execute. Replies are composed by the configured LLM provider when one is
available, otherwise deterministically from real tool outputs (stub mode).

Provider credentials are per-business, Fernet-encrypted at rest
(``draven_provider_config``), and never returned to clients. Admin-only
endpoints (provider configuration + test) require the ``owner``/``admin``
role.

``POST /draven/stop`` is the emergency stop: an in-process kill switch
that blocks new tool executions for 60s. It is intentionally honest
about its scope — a single API process only; document multi-worker
deployments accordingly.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from forge_db.models import (
    DravenProviderConfig,
    DravenSwarmEvent,
    DravenSwarmRun,
    DravenSwarmRunStatus,
    DravenToolRun,
    User,
)
from forge_db.session import SessionLocal
from forge_llm import GenerationRequest, get_provider

from app import draven_swarm as swarm
from app.core.deps import CurrentSettings, CurrentUser, DbSession, require_role
from app.core.rate_limit import quota_limited
from app.draven_crypto import decrypt_secret, encrypt_secret, get_fernet
from app import draven_conversation as conversation
from app.draven_conversation import PendingIntent
from app.settings_vault import service as vault
from app.draven_tools import (
    DEFAULT_TOOL_TIMEOUT_S,
    TOOLS,
    execute_tool,
    route_intent,
    tool_json_schema,
    write_audit_row,
)

router = APIRouter(prefix="/draven", tags=["draven"])

AdminUser = Annotated[User, Depends(require_role("owner", "admin"))]

_ALLOWED_PROVIDERS = ("gemini", "anthropic", "openrouter", "ollama", "openai_compatible")

# In-process emergency stop: timestamp of the last POST /draven/stop.
# Blocks new tool executions for 60s. Single-process scope — in a
# multi-worker deployment each process holds its own flag.
_kill_switch_at: datetime | None = None

# Last provider test latency per business (business_id -> (latency_ms, at)).
_last_test_latency: dict[uuid.UUID, tuple[float, datetime]] = {}


# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------

# Hard allowlist: every ElevenLabs request is built from this constant.
# No custom URLs are accepted for this provider (SSRF protection) —
# only the voice_id path segment varies, and it is strictly validated.
_ELEVENLABS_BASE = "https://api.elevenlabs.io"
_ELEVENLABS_VOICES_PATH = "/v1/voices"
_ELEVENLABS_TTS_PATH = "/v1/text-to-speech/{voice_id}"

# In-process voice-list cache: business_id -> (voices, fetched_at). 1h TTL.
_voices_cache: dict[uuid.UUID, tuple[list[dict[str, Any]], datetime]] = {}
_VOICES_CACHE_TTL = timedelta(hours=1)

# Per-business TTS rate limit: 20 requests per rolling 60s window, in-process.
# (Multi-worker deployments should move this to Redis; documented limitation.)
_tts_rate: dict[uuid.UUID, list[float]] = {}
_TTS_RATE_LIMIT = 20
_TTS_RATE_WINDOW_S = 60.0

# ElevenLabs API per-character USD rates by model (ESTIMATES from their
# published API rate card, https://elevenlabs.io/pricing — Multilingual v2
# $0.10/1k chars, Flash/Turbo $0.05/1k chars). Actual cost depends on the
# account's plan; treat every computed cost as an estimate, not a bill.
_ELEVENLABS_USD_PER_CHAR: dict[str, float] = {
    "eleven_multilingual_v2": 0.00010,
    "eleven_v3": 0.00010,
    "eleven_flash_v2_5": 0.00005,
    "eleven_turbo_v2_5": 0.00005,
}
_ELEVENLABS_DEFAULT_USD_PER_CHAR = 0.00010

# ElevenLabs voice IDs are opaque alphanumeric strings; this rejects any
# path separator or traversal attempt before the id reaches the URL.
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _elevenlabs_tts_key(
    db: Session, user: CurrentUser, settings
) -> str | None:
    """Decrypted ElevenLabs API key for the business, or None if unconfigured.

    Reads the vault first; falls back to the deprecated
    ``draven_provider_config.tts_api_key_enc`` column during migration.
    """
    key = vault.get_secret(
        db, user.business_id, "elevenlabs.api_key", settings.DRAVEN_CONFIG_KEY
    )
    if key:
        return key
    row = _business_config(db, user.business_id)
    if row is None or row.tts_provider != "elevenlabs" or not row.tts_api_key_enc:
        return None
    return decrypt_secret(get_fernet(settings.DRAVEN_CONFIG_KEY), row.tts_api_key_enc)


async def _elevenlabs_api(
    method: str,
    path: str,
    api_key: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> httpx.Response:
    """One ElevenLabs HTTPS call. ``path`` is always an internal constant
    (plus a validated voice_id); the host is the hard allowlist above."""
    url = _ELEVENLABS_BASE + path
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(
            method, url, headers={"xi-api-key": api_key}, json=json_body
        )


def _map_voice(v: dict[str, Any]) -> dict[str, Any]:
    labels = v.get("labels") or {}
    return {
        "voice_id": v.get("voice_id"),
        "name": v.get("name"),
        "language": labels.get("language"),
        "category": v.get("category"),
    }


async def _fetch_live_voices(api_key: str) -> list[dict[str, Any]]:
    """Fetch the live voice list from ElevenLabs (raises on upstream error)."""
    resp = await _elevenlabs_api(
        "GET", _ELEVENLABS_VOICES_PATH, api_key, timeout=15.0
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs voices request failed ({resp.status_code}): "
            f"{resp.text[:200]}"
        )
    data = resp.json()
    voices = data.get("voices") or []
    return [_map_voice(v) for v in voices if v.get("voice_id")]


async def _cached_voices(
    db: Session, user: CurrentUser, settings, api_key: str
) -> list[dict[str, Any]] | None:
    """Voice list from cache, refreshing from upstream when stale.

    Returns None when the upstream fetch fails (caller turns that into a
    502 rather than serving a fabricated list).
    """
    now = datetime.now(timezone.utc)
    cached = _voices_cache.get(user.business_id)
    if cached is not None:
        voices, fetched_at = cached
        if now - fetched_at < _VOICES_CACHE_TTL:
            return voices
    try:
        voices = await _fetch_live_voices(api_key)
    except Exception:
        return None
    _voices_cache[user.business_id] = (voices, now)
    return voices


def _check_tts_rate_limit(business_id: uuid.UUID) -> bool:
    """Sliding-window rate limit: True if the request is allowed."""
    now = time.monotonic()
    window_start = now - _TTS_RATE_WINDOW_S
    stamps = [s for s in _tts_rate.get(business_id, []) if s > window_start]
    if len(stamps) >= _TTS_RATE_LIMIT:
        _tts_rate[business_id] = stamps
        return False
    stamps.append(now)
    _tts_rate[business_id] = stamps
    return True


def _elevenlabs_cost_estimate(model_id: str, chars: int) -> float:
    rate = _ELEVENLABS_USD_PER_CHAR.get(model_id, _ELEVENLABS_DEFAULT_USD_PER_CHAR)
    return chars * rate


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ToolInfo(BaseModel):
    id: str
    description: str
    risk: str
    input_schema: dict[str, Any]


class ChatHistoryMsg(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class DravenChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatHistoryMsg] = Field(default_factory=list, max_length=20)
    persona: Literal["draven", "calcifer"] = Field(default="draven")
    client_tz: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "IANA timezone of the client (e.g. America/Chicago), used for "
            "time-aware greetings. Unknown values are rejected."
        ),
    )

    @field_validator("client_tz")
    @classmethod
    def _validate_tz(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown timezone: {v}")
        return v


class ToolUseOut(BaseModel):
    tool: str
    risk: str
    status: str
    duration_ms: int


class ApprovalNeedOut(BaseModel):
    tool: str
    description: str
    input: dict[str, Any]


class DravenChatResponse(BaseModel):
    reply: str
    tools_used: list[ToolUseOut]
    approvals_needed: list[ApprovalNeedOut]
    estimated_cost_usd: float
    provider: str


class TtsStatus(BaseModel):
    provider: str = "none"  # "elevenlabs" | "none"
    configured: bool = False
    voice_count: int | None = None  # from cached voice list, None when unknown


class ProviderStatus(BaseModel):
    provider: str
    model: str | None
    configured: bool
    latency_ms: float | None
    tts: TtsStatus = Field(default_factory=TtsStatus)


class ProviderConfigIn(BaseModel):
    """Admin provider configuration.

    Chat LLM provider: ``provider`` in {"gemini", "anthropic", "openrouter",
    "ollama", "openai_compatible"} (stub is not offered — an unconfigured
    provider reports ``configured=false`` instead). Passing
    ``provider="elevenlabs"`` (optionally with ``api_key``) is accepted as a
    shorthand that configures the TTS voice provider instead, leaving the
    chat provider untouched.
    Explicit ``tts_provider``/``tts_api_key`` fields do the same job; pairing
    an explicit ``tts_provider`` with ``provider="stub"`` is a TTS-only
    update that leaves the chat provider columns untouched.
    """

    provider: str
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    tts_provider: str | None = None  # "elevenlabs" | "none" | None (unchanged)
    tts_api_key: str | None = None

    model_config = {"extra": "forbid"}


class ProviderConfigOut(BaseModel):
    provider: str
    model: str | None
    has_api_key: bool
    has_base_url: bool
    tts: TtsStatus = Field(default_factory=TtsStatus)
    updated_at: datetime


class ProviderTestOut(BaseModel):
    ok: bool
    latency_ms: float | None
    error: str | None


class StopOut(BaseModel):
    stopped: bool
    at: datetime


class VoiceInfo(BaseModel):
    voice_id: str
    name: str | None = None
    language: str | None = None
    category: str | None = None


class VoicesOut(BaseModel):
    configured: bool
    voices: list[VoiceInfo]


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice_id: str | None = Field(default=None, max_length=64)
    model_id: str = Field(default="eleven_multilingual_v2", max_length=64)
    output_format: str = Field(default="mp3_44100_192", max_length=32)


class AuditItem(BaseModel):
    tool: str
    risk: str
    status: str
    duration_ms: int
    created_at: datetime
    output_summary: str | None


class AuditOut(BaseModel):
    items: list[AuditItem]
    total: int


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------


def _business_config(
    db: Session, business_id: uuid.UUID
) -> DravenProviderConfig | None:
    return (
        db.query(DravenProviderConfig)
        .filter(DravenProviderConfig.business_id == business_id)
        .first()
    )


def _build_provider_from_config(
    db: Session, business_id: uuid.UUID, row: DravenProviderConfig, settings
) -> tuple[Any, str | None]:
    """Build an LLM provider from a business config row.

    Key material comes from the settings vault first, with fallback to the
    deprecated ``draven_provider_config`` columns during migration.
    Returns (provider, model). Raises ValueError on missing/invalid config.
    """
    from forge_llm.providers import (
        AnthropicProvider,
        GeminiProvider,
        OllamaProvider,
        OpenAICompatibleProvider,
        OpenRouterProvider,
        StubProvider,
    )

    fernet = get_fernet(settings.DRAVEN_CONFIG_KEY)
    ck = settings.DRAVEN_CONFIG_KEY

    def _vault_or_legacy(key_name: str, legacy_enc: str | None) -> str | None:
        key = vault.get_secret(db, business_id, key_name, ck)
        if key:
            return key
        return decrypt_secret(fernet, legacy_enc)

    if row.provider == "stub":
        # Legacy rows: stub is no longer a user-facing option. It is treated
        # as "not configured" everywhere (see _resolve_provider).
        return StubProvider(), "stub"
    if row.provider == "anthropic":
        api_key = _vault_or_legacy("anthropic.api_key", row.api_key_enc)
        if not api_key:
            raise ValueError("anthropic provider is missing its API key")
        return AnthropicProvider(api_key=api_key, model=row.model), row.model
    if row.provider == "gemini":
        api_key = _vault_or_legacy("gemini.api_key", row.api_key_enc)
        if not api_key:
            raise ValueError("gemini provider is missing its API key")
        return GeminiProvider(api_key=api_key, model=row.model), row.model
    if row.provider == "openrouter":
        api_key = _vault_or_legacy("openrouter.api_key", row.api_key_enc)
        if not api_key:
            raise ValueError("openrouter provider is missing its API key")
        if not row.model:
            raise ValueError("openrouter provider needs a model")
        return OpenRouterProvider(api_key=api_key, model=row.model), row.model
    if row.provider == "ollama":
        base_url = _vault_or_legacy("ollama.base_url", row.base_url_enc)
        if not row.model:
            raise ValueError("ollama provider needs a model")
        return OllamaProvider(base_url=base_url or None, model=row.model), row.model
    if row.provider == "openai_compatible":
        base_url = _vault_or_legacy("openai.base_url", row.base_url_enc)
        api_key = _vault_or_legacy("openai.api_key", row.api_key_enc)
        if not base_url or not row.model:
            raise ValueError(
                "openai_compatible provider needs a base URL and a model"
            )
        return (
            OpenAICompatibleProvider(
                base_url=base_url, api_key=api_key, model=row.model
            ),
            row.model,
        )
    raise ValueError(f"unsupported provider: {row.provider}")


def _resolve_provider(
    db: Session, user: CurrentUser, settings
) -> tuple[Any, str, bool]:
    """Business config wins; otherwise fall back to the global env provider.

    Reads the DB per request so multi-worker deployments stay consistent.
    Returns (provider_instance, display_name, configured).

    "Configured" means a real LLM will actually answer. Anything else —
    legacy stub rows, missing keys, unknown env values — resolves to a
    StubProvider instance with configured=False, and callers must NOT
    present it as a working AI (see the chat composition below).
    When nothing is configured at all, the display name defaults to
    "gemini" so the UI can point the user at the right setup step.
    """
    from forge_llm.providers import StubProvider  # local: avoids import cost

    row = _business_config(db, user.business_id)
    if row is not None:
        if row.provider == "stub":
            return StubProvider(), "stub", False
        try:
            provider, _ = _build_provider_from_config(
                db, user.business_id, row, settings
            )
        except ValueError:
            # Explicitly chosen but broken (e.g. key removed): unconfigured,
            # never a silent stub.
            return StubProvider(), row.provider, False
        return provider, provider.name, True
    name = (settings.LLM_PROVIDER or "").strip().lower()
    if name in _ALLOWED_PROVIDERS:
        try:
            provider = get_provider()
        except ValueError:
            return StubProvider(), name, False
        return provider, provider.name, True
    return StubProvider(), "gemini", False


def _tts_status(
    db: Session, user: CurrentUser, settings
) -> TtsStatus:
    """TTS section for provider status/config responses. Never key material."""
    row = _business_config(db, user.business_id)
    tts_provider = (row.tts_provider if row else None) or "none"
    configured = tts_provider == "elevenlabs" and bool(
        vault.get_secret(
            db, user.business_id, "elevenlabs.api_key",
            settings.DRAVEN_CONFIG_KEY,
        )
        or (row and row.tts_api_key_enc)
    )
    voice_count: int | None = None
    cached = _voices_cache.get(user.business_id)
    if cached is not None:
        voices, fetched_at = cached
        if datetime.now(timezone.utc) - fetched_at < _VOICES_CACHE_TTL:
            voice_count = len(voices)
    return TtsStatus(
        provider=tts_provider, configured=configured, voice_count=voice_count
    )


def _config_status(
    db: Session, user: CurrentUser, settings
) -> ProviderStatus:
    row = _business_config(db, user.business_id)
    latency = _last_test_latency.get(user.business_id)
    latency_ms = latency[0] if latency else None
    tts = _tts_status(db, user, settings)
    ck = settings.DRAVEN_CONFIG_KEY
    if row is not None:
        p = row.provider
        if p == "stub":
            # Legacy stub rows count as not configured.
            configured = False
        elif p == "anthropic":
            configured = (
                vault.get_secret(db, user.business_id, "anthropic.api_key", ck)
                is not None or row.api_key_enc is not None
            )
        elif p == "gemini":
            configured = (
                vault.get_secret(db, user.business_id, "gemini.api_key", ck)
                is not None or row.api_key_enc is not None
            )
        elif p == "openrouter":
            configured = (
                vault.get_secret(db, user.business_id, "openrouter.api_key", ck)
                is not None or row.api_key_enc is not None
            ) and bool(row.model)
        elif p == "ollama":
            # No key needed; a model must be chosen (base URL optional).
            configured = bool(row.model)
        else:  # openai_compatible
            configured = (
                vault.get_secret(db, user.business_id, "openai.base_url", ck)
                is not None or row.base_url_enc is not None
            ) and bool(row.model)
        return ProviderStatus(
            provider=p,
            model=row.model,
            configured=configured,
            latency_ms=latency_ms,
            tts=tts,
        )
    # No business config — report the global env provider.
    name = (settings.LLM_PROVIDER or "").strip().lower()
    if name == "anthropic":
        configured = bool(settings.ANTHROPIC_API_KEY)
        model = settings.ANTHROPIC_MODEL
    elif name == "gemini":
        configured = bool(settings.GEMINI_API_KEY)
        model = settings.GEMINI_MODEL or "gemini-3.5-flash-lite"
    elif name == "openrouter":
        configured = bool(settings.OPENROUTER_API_KEY) and bool(
            settings.OPENROUTER_MODEL
        )
        model = settings.OPENROUTER_MODEL or None
    elif name == "ollama":
        configured = bool(settings.OLLAMA_MODEL)
        model = settings.OLLAMA_MODEL or None
    elif name == "openai_compatible":
        configured = bool(settings.OPENAI_COMPAT_BASE_URL) and bool(
            settings.OPENAI_COMPAT_MODEL
        )
        model = settings.OPENAI_COMPAT_MODEL or None
    else:
        # Default: gemini, unconfigured. Stub is never reported as working.
        return ProviderStatus(
            provider="gemini",
            model=None,
            configured=False,
            latency_ms=latency_ms,
            tts=tts,
        )
    return ProviderStatus(
        provider=name,
        model=model,
        configured=configured,
        latency_ms=latency_ms,
        tts=tts,
    )


# ---------------------------------------------------------------------------
# Reply composition
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Assistant personas: Draven (executive) and Calcifer (living fire model).
# Personality scope is deliberately narrow — name, orb theme, identity
# wording, system-prompt name. Tool behavior, safety rules, approvals, and
# response substance are identical for both.
# ---------------------------------------------------------------------------

def time_salutation(now: datetime) -> str:
    """Time-aware greeting. Business manners: never 'good night' — the
    assistant is not signing off, so late night stays 'Good evening'."""
    h = now.hour
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 17:
        return "Good afternoon"
    return "Good evening"


_PERSONAS: dict[str, dict[str, str]] = {
    "draven": {
        "name": "Draven",
        "identity_reply": (
            "I'm Draven — your voice-first executive assistant inside "
            "ForgeOS. I can check approvals, campaigns, and analytics, "
            "run product research, process leads, and take actions for "
            "you (the risky ones need your approval first). What do you "
            "need?"
        ),
        "system_prompt": (
            "You are Draven, the voice-first executive assistant inside ForgeOS, a "
            "marketing automation platform. Conduct yourself with polished business "
            "manners: greet warmly, acknowledge each request, confirm before any "
            "consequential action, and thank the user — professional and courteous, "
            "never stiff or robotic. Summarize ONLY using the tool results "
            "below — never invent numbers, names, or statuses. If a result needs "
            "human approval, say so plainly. Keep the reply under 120 words, plain "
            "text, no markdown, no emojis."
        ),
    },
    "calcifer": {
        "name": "Calcifer",
        "identity_reply": (
            "I'm Calcifer — a living fire model, and your loyal assistant inside "
            "ForgeOS. I've got plenty of spark for the work: approvals, campaigns, "
            "analytics, product research, lead follow-ups — say the word and I'll "
            "get the flames going. The risky moves still need your approval first. "
            "What are we lighting up today?"
        ),
        "system_prompt": (
            "You are Calcifer, a living fire model and loyal assistant inside ForgeOS, a "
            "marketing automation platform. You are warm, spirited, and energetic — a "
            "wink of fire in your wording — but this is a business OS: stay professional, "
            "precise, and helpful, never a comedy act. Mind your manners throughout: "
            "greet warmly, acknowledge each request, confirm before any consequential "
            "action, and thank the user. Summarize ONLY using the tool results "
            "below — never invent numbers, names, or statuses. If a result needs "
            "human approval, say so plainly. Keep the reply under 120 words, plain "
            "text, no markdown, no emojis."
        ),
    },
}


def _persona_of(payload: DravenChatRequest) -> dict[str, str]:
    """Validated persona; unknown values fall back to Draven."""
    return _PERSONAS.get(payload.persona) or _PERSONAS["draven"]


_DRAVEN_SYSTEM = _PERSONAS["draven"]["system_prompt"]


def _deterministic_reply(results: list[dict[str, Any]]) -> str:
    """Compose a reply from real tool outputs when no LLM is configured."""
    parts: list[str] = []
    for r in results:
        tid = r["tool"]
        status_ = r["status"]
        if status_ == "error":
            # Missing research seeds → ask a clarification instead of erroring.
            if tid == "market.research_start" and "seed_terms" in str(
                r.get("error", "")
            ):
                parts.append(
                    "I can run that research — which product category should I "
                    "focus on? (e.g. home fitness, kitchen gadgets, pet supplies). "
                    "Or give me a few seed keywords and I'll dig in."
                )
                continue
            parts.append(f"{tid} ran into a problem: {r.get('error')}.")
            continue
        if status_ == "approval_required":
            ap = r.get("approval", {})
            parts.append(f"This needs your approval: {ap.get('description')}")
            continue
        out = r.get("output", {}) or {}
        if tid == "draven.business_summary":
            parts.append(
                f"{out.get('business_name') or 'Your business'}: "
                f"{out.get('campaigns', 0)} campaigns "
                f"({out.get('campaigns_running', 0)} running), "
                f"{out.get('contacts', 0)} contacts, "
                f"{out.get('assets', 0)} assets "
                f"({out.get('assets_in_review', 0)} in review), "
                f"{out.get('templates', 0)} templates."
            )
        elif tid == "draven.approvals_pending":
            n = out.get("count", 0)
            titles = ", ".join(a["title"] for a in out.get("assets", [])[:3])
            parts.append(
                f"{n} asset{'s' if n != 1 else ''} awaiting review"
                + (f": {titles}{' and more' if n > 3 else ''}." if titles else ".")
            )
        elif tid == "draven.assets_pending_review":
            parts.append(
                f"{out.get('count', 0)} assets in the review queue with full details ready."
            )
        elif tid == "draven.campaigns_status":
            items = out.get("campaigns", [])
            desc = ", ".join(f"{c['name']} ({c['status']})" for c in items[:5])
            parts.append(
                f"{out.get('count', 0)} campaigns"
                + (f": {desc}." if desc else ".")
            )
        elif tid == "draven.analytics_summary":
            parts.append(
                f"Last {out.get('days', 30)} days: {out.get('sent', 0)} sent, "
                f"{out.get('opened', 0)} opened "
                f"({round(out.get('open_rate', 0) * 100, 1)}% open rate), "
                f"{out.get('converted', 0)} conversions, "
                f"${out.get('spend_usd', 0):.2f} spend."
            )
        elif tid == "draven.contacts_search":
            n = out.get("count", 0)
            names = ", ".join(
                " ".join(
                    p for p in [c.get("first_name"), c.get("last_name")] if p
                )
                or c.get("email")
                or "unnamed"
                for c in out.get("contacts", [])[:3]
            )
            if n == 0:
                parts.append(
                    "No contacts matched. Give me a name or email to search for."
                )
            else:
                parts.append(f"Found {n} contact{'s' if n != 1 else ''}: {names}.")
        elif tid == "draven.autopilot_status":
            s = out.get("settings") or {}
            plan = out.get("latest_plan")
            plan_txt = (
                f"Latest plan: {plan['status']} for week of {plan['week_start']}."
                if plan
                else "No weekly plan drafted yet."
            )
            parts.append(
                f"Autopilot auto-approve is "
                f"{'on' if s.get('auto_approve') else 'off'}. {plan_txt}"
            )
        elif tid == "market.research_start":
            n = out.get("opportunity_count", 0)
            conns = out.get("connectors_used") or []
            gaps = out.get("evidence_gaps") or []
            tops = out.get("top_opportunities") or []
            names = ", ".join(
                f"{t['name'][:40]} ({t['opportunity_score']:.0f})" for t in tops[:3]
            )
            live = (
                f"Live data from {', '.join(conns)}."
                if conns
                else "No live data source is configured — "
                "connect DataForSEO for live demand signals."
            )
            parts.append(
                f"Research for {out.get('target')} ({out.get('market')}) is done: "
                f"{n} opportunit{'ies' if n != 1 else 'y'}. {live}"
                + (f" Top: {names}." if names else "")
                + (
                    f" Evidence gaps: {len(gaps)} noted in the full report."
                    if gaps
                    else ""
                )
            )
        elif tid == "market.research_status":
            if not out.get("found", True):
                parts.append("No research jobs yet.")
            else:
                parts.append(
                    f"Research job {str(out.get('job_id'))[:8]}: "
                    f"{out.get('status')} ({out.get('progress', 0)}%)."
                )
        elif tid == "market.top_opportunities":
            items = out.get("opportunities") or []
            desc = ", ".join(
                f"{o['name'][:40]} ({o['opportunity_score']:.0f})" for o in items[:5]
            )
            parts.append(
                f"{out.get('count', 0)} top opportunities"
                + (f": {desc}." if desc else
                   " — none scored yet. Run a product research first.")
            )
        elif tid == "alpha.lead_intake":
            q = out.get("qualification") or {}
            verdict = q.get("verdict", "pending")
            parts.append(
                f"Lead captured ({'new' if out.get('created') else 'existing'}). "
                f"Qualification: {verdict}"
                + (f" (score {q.get('score'):.2f})." if q.get("score") is not None else ".")
                + f" Run state: {out.get('run_state')}."
            )
        elif tid == "alpha.qualify_lead":
            q = out.get("qualification") or {}
            parts.append(
                f"Qualification: {q.get('verdict', 'unknown')}"
                + (f" (score {q.get('score'):.2f}, "
                   f"confidence {q.get('confidence'):.2f}, "
                   f"rules v{q.get('rules_version')})." if q.get("score") is not None else ".")
            )
        elif tid == "alpha.run_status":
            parts.append(
                f"Run {str(out.get('run_id'))[:8]}: {out.get('state')}"
                + (" (paused)" if out.get("paused") else "")
                + (f", {out.get('retry_count')} retries." if out.get("retry_count") else ".")
            )
        elif tid == "alpha.prepare_followup":
            parts.append(
                f"Follow-up draft ready — {out.get('approval_status', 'pending')} "
                f"approval. Nothing has been sent; approve it when ready."
            )
        # --- Voice parity tools ------------------------------------------------
        elif tid == "draven.compliance_check":
            n = len(out.get("violations", []))
            if out.get("compliant"):
                parts.append(
                    "That looks compliant — no disclosure violations found. "
                    "(Heuristic screen, not legal advice.)"
                )
            else:
                fixes = "; ".join(
                    v.get("fix", "") for v in out.get("violations", [])[:2]
                )
                parts.append(
                    f"Found {n} compliance issue{'s' if n != 1 else ''}: "
                    + "; ".join(
                        v.get("message", "")[:120]
                        for v in out.get("violations", [])[:2]
                    )
                    + (f" Fix: {fixes[:200]}" if fixes else "")
                )
        elif tid == "draven.compliance_rules":
            pack = out.get("pack")
            if pack:
                parts.append(
                    f"{pack.get('name')}: "
                    + " ".join(pack.get("notes", [])[:2])
                )
            else:
                names = ", ".join(p.get("name", "") for p in out.get("packs", []))
                parts.append(f"I can check against: {names}.")
        elif tid == "draven.web_search":
            results = out.get("results", [])
            if not results:
                parts.append("No web results found for that query.")
            else:
                tops = "; ".join(
                    f"{r.get('title', '')[:60]} ({r.get('url', '')[:50]})"
                    for r in results[:3]
                )
                parts.append(
                    f"Top web results: {tops}. "
                    "These are untrusted sources — I can dig deeper with research."
                )
        elif tid == "draven.web_research":
            srcs = out.get("sources", [])
            n = out.get("sources_fetched", 0)
            if not srcs:
                parts.append("Web research came back empty.")
            else:
                titles = "; ".join(s.get("title", "")[:50] for s in srcs[:3])
                parts.append(
                    f"Researched {n} of {len(srcs)} sources: {titles}. "
                    "I cross-checked them — where they agree that's the "
                    "consensus; where they conflict, I flag it rather than "
                    "stating it as fact."
                )
        elif tid == "draven.brandkit_list":
            n = out.get("count", 0)
            names = ", ".join(
                f"{k['name']} (v{k['version']})" for k in out.get("brand_kits", [])[:5]
            )
            parts.append(
                f"{n} brand kit{'s' if n != 1 else ''}"
                + (f": {names}." if names else ".")
            )
        elif tid in ("draven.brandkit_get", "draven.brandkit_create", "draven.brandkit_update"):
            k = out.get("brand_kit") or {}
            parts.append(
                f"Brand kit '{k.get('name')}' (v{k.get('version')})"
                + (f" — {out.get('note')}" if out.get("note") else ".")
            )
        elif tid in ("draven.contacts_list", "draven.contact_get",
                     "draven.contact_create", "draven.contact_update"):
            if "contacts" in out:
                n = out.get("count", 0)
                parts.append(f"{n} contact{'s' if n != 1 else ''} on file.")
            else:
                c = out.get("contact") or {}
                label = " ".join(
                    p for p in [c.get("first_name"), c.get("last_name")] if p
                ) or c.get("email") or "contact"
                parts.append(f"Contact: {label} ({c.get('email') or c.get('phone') or 'no address'}).")
        elif tid == "draven.contact_consent":
            parts.append(out.get("note", "Consent updated."))
        elif tid in ("draven.template_list", "draven.template_get",
                     "draven.template_create", "draven.template_update"):
            if "templates" in out:
                n = out.get("count", 0)
                names = ", ".join(
                    f"{t['name']} ({t['channel']})" for t in out.get("templates", [])[:5]
                )
                parts.append(
                    f"{n} template{'s' if n != 1 else ''}"
                    + (f": {names}." if names else ".")
                )
            else:
                t = out.get("template") or {}
                parts.append(f"Template '{t.get('name')}' ({t.get('channel')}) ready.")
        elif tid == "draven.template_preview":
            body = (out.get("body") or "")[:400]
            parts.append(
                f"Preview of '{out.get('template_name')}': {body}"
                + ("…" if len(out.get("body") or "") > 400 else "")
            )
        elif tid == "draven.assets_list":
            n = out.get("count", 0)
            titles = ", ".join(
                f"{a['title']} ({a['status']})" for a in out.get("assets", [])[:5]
            )
            parts.append(
                f"{n} asset{'s' if n != 1 else ''}"
                + (f": {titles}." if titles else ".")
            )
        elif tid in ("draven.asset_get", "draven.asset_submit"):
            a = out.get("asset") or {}
            parts.append(
                f"Asset '{a.get('title')}' ({a.get('kind')}, {a.get('status')})"
                + (f" — {out.get('note')}" if out.get("note") else ".")
            )
        elif tid == "draven.asset_versions":
            parts.append(f"{out.get('count', 0)} versions in this asset's lineage.")
        elif tid in ("draven.campaign_get", "draven.campaign_create", "draven.campaign_update"):
            c = out.get("campaign") or {}
            steps = out.get("steps")
            parts.append(
                f"Campaign '{c.get('name')}' ({c.get('status')})"
                + (f" with {len(steps)} steps" if steps is not None else "")
                + (f" — {out.get('sequence_summary')}" if out.get("sequence_summary") else "")
                + (f" — {out.get('note')}" if out.get("note") and not out.get("sequence_summary") else ".")
            )
        elif tid == "draven.campaign_steps_add":
            parts.append(
                f"Added {out.get('added', 0)} step{'s' if out.get('added', 0) != 1 else ''} "
                f"to the campaign."
            )
        elif tid == "draven.campaign_step_update":
            s = out.get("step") or {}
            parts.append(
                f"Step {s.get('position')} updated ({s.get('channel')}, "
                f"{s.get('delay_hours')}h delay)."
            )
        elif tid == "draven.campaign_enrollments":
            parts.append(f"{out.get('count', 0)} enrollments on this campaign.")
        elif tid == "draven.campaign_enroll":
            n = out.get("enrolled", 0)
            dup = out.get("skipped_already_enrolled", 0)
            unsub = out.get("skipped_unsubscribed", 0)
            parts.append(
                f"Enrolled {n} contact{'s' if n != 1 else ''} into "
                f"'{out.get('campaign_name')}'"
                + (f" ({dup} already enrolled skipped" if dup else "")
                + (f", {unsub} unsubscribed skipped" if unsub else "")
                + (")" if dup or unsub else "")
                + "."
            )
        elif tid == "draven.autopilot_update":
            s = out.get("settings") or {}
            parts.append(
                f"Autopilot updated: auto-approve "
                f"{'on' if s.get('auto_approve') else 'off'}, daily cap "
                f"{s.get('daily_send_cap')}."
            )
        elif tid in ("draven.affiliate_programs_list", "draven.affiliate_program_get",
                     "draven.affiliate_program_create", "draven.affiliate_program_update"):
            if "programs" in out:
                n = out.get("count", 0)
                names = ", ".join(p["name"] for p in out.get("programs", [])[:5])
                parts.append(
                    f"{n} affiliate program{'s' if n != 1 else ''}"
                    + (f": {names}." if names else ".")
                )
            else:
                p = out.get("program") or {}
                parts.append(
                    f"Affiliate program '{p.get('name')}' ({p.get('status')}, "
                    f"{p.get('default_commission_pct')}% default commission)."
                )
        elif tid in ("draven.affiliate_links_list", "draven.affiliate_link_get",
                     "draven.affiliate_link_create", "draven.affiliate_link_update"):
            if "links" in out:
                n = out.get("count", 0)
                parts.append(f"{n} trackable affiliate link{'s' if n != 1 else ''}.")
            else:
                link = out.get("link") or {}
                parts.append(
                    f"Affiliate link '{link.get('label')}' (/{link.get('slug')}) "
                    f"{'active' if link.get('is_active') else 'inactive'}."
                )
        elif tid == "draven.affiliate_earnings":
            t = out.get("totals") or {}
            parts.append(
                f"Last {out.get('days', 30)} days: {t.get('clicks', 0)} clicks, "
                f"{t.get('conversions', 0)} conversions "
                f"({round((t.get('conversion_rate') or 0) * 100, 1)}%), "
                f"${(t.get('earnings_usd') or 0):.2f} earnings."
            )
        elif tid == "draven.analytics_funnel":
            items = (out.get("funnel") or {}).get("items", [])
            desc = ", ".join(
                f"step {i.get('position')}: {i.get('sent')} sent / {i.get('opened')} opened"
                for i in items[:5]
            )
            parts.append(
                f"Funnel for '{out.get('campaign_name')}'"
                + (f": {desc}." if desc else " — no steps yet.")
            )
        elif tid == "draven.analytics_weekly_summary":
            if not out.get("found"):
                parts.append("No weekly summary has been cut yet.")
            else:
                rec = (out.get("recommendation") or "")[:300]
                parts.append(
                    f"Week of {out.get('week_start')}: "
                    f"{len(out.get('top_assets') or [])} top assets tracked. {rec}"
                )
        elif tid == "draven.interview_start":
            parts.append(
                f"Interview started (session {str(out.get('session_id'))[:8]}). "
                f"Question 1 of {out.get('total_questions')}: {out.get('question')}"
            )
        elif tid == "draven.interview_status":
            if not out.get("found"):
                parts.append("No interview session yet — say 'start the brand interview'.")
            else:
                parts.append(
                    f"Interview {out.get('status')}: question "
                    f"{out.get('question_index')} of {out.get('total_questions')}."
                )
        elif tid == "draven.ops_activity":
            c = out.get("counters") or {}
            st = out.get("stages") or {}
            running = (st.get("growth") or {}).get("campaigns_running", 0)
            parts.append(
                f"Mission control: {c.get('sends_today', 0)} sends today, "
                f"{c.get('generations_today', 0)} generations, "
                f"{running} campaigns running, {c.get('in_flight', 0)} in flight."
            )
        elif tid == "draven.ops_brain":
            layers = out.get("layers") or []
            desc = ", ".join(f"{ly['label']}: {ly['count']}" for ly in layers)
            parts.append(
                f"Knowledge graph ({out.get('link_count', 0)} links): {desc}."
            )
        elif tid in ("draven.business_get", "draven.business_update"):
            parts.append(
                f"Business: {out.get('name')} (timezone {out.get('timezone')})."
            )
        elif tid == "viator.product_import":
            n = out.get("programs_new", 0)
            found = out.get("products_found", 0)
            items = out.get("items") or []
            names = ", ".join(
                i.get("program_name", "")[:40] for i in items[:3] if i.get("program_name")
            )
            if found == 0:
                parts.append(
                    "Viator search returned no products — nothing to import. "
                    "Try a different destination or keyword."
                )
            else:
                parts.append(
                    f"Imported {n} Viator products as affiliate programs"
                    + (f": {names}." if names else ".")
                    + " All have trackable links. Nothing published."
                )
        elif tid == "viator.generate_ads":
            n = out.get("ads_created", 0)
            flagged = out.get("compliance_flagged", 0)
            parts.append(
                f"Generated {n} draft ads with FTC disclosure included"
                + (f" ({flagged} flagged for compliance review)." if flagged else ".")
                + " All are drafts — nothing published."
            )
        elif tid == "draven.campaign_create":
            name = out.get("name") or out.get("campaign_name") or "campaign"
            parts.append(
                f"Campaign '{name}' created as a draft. "
                "It needs your approval before anything sends."
            )
    if not parts:
        return (
            "I didn't find anything to act on. I can check approvals, "
            "campaigns, analytics, contacts, autopilot, brand kits, "
            "templates, assets, affiliates, interviews, or the ops "
            "dashboard — what do you need?"
        )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/tools", response_model=list[ToolInfo])
def list_tools(user: CurrentUser) -> list[ToolInfo]:
    """Registered Draven tools: id, description, risk, input schema."""
    return [
        ToolInfo(
            id=t.id,
            description=t.description,
            risk=t.risk,
            input_schema=tool_json_schema(t.id),
        )
        for t in TOOLS.values()
    ]


@router.post("/chat", response_model=DravenChatResponse)
@quota_limited("draven")
async def chat(
    payload: DravenChatRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: CurrentSettings,
) -> DravenChatResponse:
    global _kill_switch_at
    now = datetime.now(timezone.utc)
    persona = _persona_of(payload)
    pname = persona["name"]

    # Emergency stop: refuse new tool executions for 60s after POST /stop.
    if _kill_switch_at is not None and (now - _kill_switch_at).total_seconds() < 60:
        return DravenChatResponse(
            reply=(
                f"{pname} is stopped — the emergency stop is active. "
                "No tools were run. Say the word when you're ready to resume."
            ),
            tools_used=[],
            approvals_needed=[],
            estimated_cost_usd=0.0,
            provider="stopped",
        )

    routed = route_intent(payload.message)
    provider, provider_name, provider_configured = _resolve_provider(
        db, user, settings
    )

    # --- Conversational slot-filling -------------------------------------
    # A routed intent is a new command: it supersedes any pending question.
    # "cancel" drops the pending question. Anything else with no routed
    # intent is treated as the answer to the pending question.
    pending = conversation.get_pending(user.business_id)
    fill_note = ""
    if routed:
        conversation.clear_pending(user.business_id)
        pending = None
    elif pending and conversation.looks_like_cancel(payload.message):
        conversation.clear_pending(user.business_id)
        return DravenChatResponse(
            reply="No problem — dropped that.",
            tools_used=[],
            approvals_needed=[],
            estimated_cost_usd=0.0,
            provider=provider_name,
        )

    # Identity: the assistant knows its own name, with or without tools.
    # It never consumes a pending question. With a valid client_tz, the
    # reply opens with a time-aware salutation (proper manners); without
    # one, no salutation is guessed.
    if not routed and re.search(
        r"\byour name\b|who are you\b|what are you called\b|"
        r"introduce yourself\b|what is your name\b",
        payload.message,
        re.IGNORECASE,
    ):
        greeting = ""
        if payload.client_tz:
            greeting = (
                f"{time_salutation(datetime.now(ZoneInfo(payload.client_tz)))}! "
            )
        return DravenChatResponse(
            reply=f"{greeting}{persona['identity_reply']}",
            tools_used=[],
            approvals_needed=[],
            estimated_cost_usd=0.0,
            provider=provider_name,
        )

    # Fill the pending slot from the user's answer, then run the original
    # tool with the completed input. Unmappable answers get a specific
    # re-ask — the missing data is never invented.
    if not routed and pending:
        filled = conversation.fill_slot(pending, payload.message)
        if filled is not None:
            completed_input, fill_note = filled
            conversation.clear_pending(user.business_id)
            routed = [(pending.kind, completed_input)]
        else:
            conversation.refresh_pending(user.business_id, pending)
            return DravenChatResponse(
                reply=conversation.reask_text(pending),
                tools_used=[],
                approvals_needed=[],
                estimated_cost_usd=0.0,
                provider=provider_name,
            )

    results: list[dict[str, Any]] = []
    for tool_id, raw_input in routed:
        tool = TOOLS[tool_id]
        timeout = tool.timeout_s or DEFAULT_TOOL_TIMEOUT_S
        started = datetime.now(timezone.utc)
        try:
            result = await asyncio.wait_for(
                execute_tool(tool_id, db, user, raw_input), timeout=timeout
            )
        except asyncio.TimeoutError:
            duration_ms = int(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000
            )
            result = {
                "tool": tool_id,
                "risk": tool.risk,
                "status": "error",
                "error": f"tool timed out after {timeout:g}s",
                "duration_ms": duration_ms,
            }
            write_audit_row(
                db, user, tool_id, tool.risk, raw_input, result, duration_ms
            )
        results.append(result)

    # Record a pending question wherever this turn asked the user for input.
    for (tool_id, raw_input), r in zip(routed, results):
        if (
            r["status"] == "error"
            and tool_id == "market.research_start"
            and "seed_terms" in str(r.get("error", ""))
        ):
            conversation.set_pending(
                user.business_id,
                PendingIntent(
                    kind=tool_id,
                    missing_slots=["category"],
                    partial_input=dict(raw_input or {}),
                    ask_text="which product category should I research?",
                ),
            )
        elif (
            tool_id == "draven.contacts_search"
            and r["status"] != "error"
            and len((r.get("output") or {}).get("contacts", [])) == 0
        ):
            conversation.set_pending(
                user.business_id,
                PendingIntent(
                    kind=tool_id,
                    missing_slots=["query"],
                    partial_input=dict(raw_input or {}),
                    ask_text="which contact should I look up?",
                ),
            )

    # Compose the reply: real provider when configured. When unconfigured,
    # say plainly what's missing instead of the generic keyword fallback.
    estimated_cost = 0.0
    if not provider_configured:
        # No silent stub fallback: say plainly what's missing. Tools already
        # ran above (they're real), so summarize them deterministically after
        # the nudge instead of the generic "didn't find anything" fallback.
        reply = (
            f"{pname} isn't connected to an AI model yet — connect your "
            "Gemini API key in Settings → AI provider to enable conversation. "
        )
        if results:
            reply += _deterministic_reply(results)
        else:
            reply += (
                "Meanwhile I can still run tools for you — ask about "
                "approvals, campaigns, analytics, contacts, or autopilot."
            )
    else:
        try:
            slim = [
                {
                    "tool": r["tool"],
                    "status": r["status"],
                    "output": r.get("output"),
                    "approval": r.get("approval"),
                    "error": r.get("error"),
                }
                for r in results
            ]
            history_text = "\n".join(
                f"{'User' if m.role == 'user' else pname}: {m.content}"
                for m in payload.history[-8:]
            )
            prompt = (
                f"User message: {payload.message}\n\n"
                f"Tool results (JSON):\n{json.dumps(slim, default=str)}\n\n"
                f"{pname}:"
            )
            if history_text:
                prompt = f"Conversation so far:\n{history_text}\n\n{prompt}"
            gen = await provider.generate(
                GenerationRequest(
                    prompt=prompt,
                    system_prompt=persona["system_prompt"],
                    max_tokens=300,
                    temperature=0.4,
                )
            )
            reply = gen.text.strip()
            estimated_cost = float(gen.cost_usd or 0.0)
        except Exception as exc:
            # Never fail the chat because composition failed — fall back
            # to the deterministic real-data summary.
            reply = _deterministic_reply(results) + (
                f" (Note: live summarization failed: {str(exc)[:120]})"
            )

    # Slot-fill confirmation: state what was understood before the results.
    if fill_note:
        reply = f"{fill_note} {reply}"

    tools_used = [
        ToolUseOut(
            tool=r["tool"],
            risk=r["risk"],
            status=r["status"],
            duration_ms=r["duration_ms"],
        )
        for r in results
    ]
    approvals_needed = [
        ApprovalNeedOut(
            tool=r["tool"],
            description=(r.get("approval") or {}).get("description", ""),
            input=(r.get("approval") or {}).get("input", {}),
        )
        for r in results
        if r["status"] == "approval_required"
    ]

    return DravenChatResponse(
        reply=reply,
        tools_used=tools_used,
        approvals_needed=approvals_needed,
        estimated_cost_usd=estimated_cost,
        provider=provider_name,
    )


@router.get("/provider", response_model=ProviderStatus)
def provider_status(
    user: CurrentUser, db: DbSession, settings: CurrentSettings
) -> ProviderStatus:
    """Configured provider, model, credential presence, last test latency.

    Never returns secret values — only whether they are present.
    """
    return _config_status(db, user, settings)


@router.put("/provider", response_model=ProviderConfigOut)
def set_provider(
    payload: ProviderConfigIn,
    admin: AdminUser,
    db: DbSession,
    settings: CurrentSettings,
) -> ProviderConfigOut:
    """Save a per-business provider config (owner/admin only).

    ``api_key`` / ``base_url`` are Fernet-encrypted before storage; passing
    ``null`` clears that field. ``provider="elevenlabs"`` (or explicit
    ``tts_provider="elevenlabs"`` + ``tts_api_key``) configures the TTS voice
    provider instead of the chat provider; the ElevenLabs key is encrypted
    with the same Fernet mechanism. Secrets are never returned in any
    response.
    """
    # Shorthand: provider="elevenlabs" configures the TTS voice provider
    # and leaves the chat provider columns untouched.
    tts_shorthand = payload.provider == "elevenlabs"
    tts_provider = payload.tts_provider
    tts_api_key = payload.tts_api_key
    if tts_shorthand:
        tts_provider = "elevenlabs"
        tts_api_key = payload.api_key
    if tts_provider is not None and tts_provider not in ("elevenlabs", "none"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='tts_provider must be "elevenlabs" or "none"',
        )
    if tts_provider == "elevenlabs" and not tts_api_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="elevenlabs tts provider requires tts_api_key (or api_key)",
        )

    chat_provider = payload.provider
    # TTS-only update: an explicit tts_provider paired with the legacy "stub"
    # placeholder means "change TTS, leave the chat provider untouched".
    # (Stub itself is no longer a selectable chat provider.)
    tts_only = payload.tts_provider is not None and chat_provider == "stub"
    if not tts_shorthand and not tts_only:
        if chat_provider not in _ALLOWED_PROVIDERS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"provider must be one of: {', '.join(_ALLOWED_PROVIDERS)}",
            )
        if chat_provider == "anthropic" and not payload.api_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="anthropic provider requires api_key",
            )
        if chat_provider == "gemini" and not payload.api_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="gemini provider requires api_key",
            )
        if chat_provider == "openrouter":
            if not payload.api_key or not payload.model:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="openrouter provider requires api_key and model",
                )
        if chat_provider == "ollama":
            if not payload.model:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="ollama provider requires model",
                )
            if payload.base_url:
                scheme = payload.base_url.split("://")[0].lower()
                if scheme not in ("http", "https"):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="base_url must use http or https",
                    )
        if chat_provider == "openai_compatible":
            if not payload.base_url or not payload.model:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="openai_compatible provider requires base_url and model",
                )
            scheme = payload.base_url.split("://")[0].lower()
            if scheme not in ("http", "https"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="base_url must use http or https",
                )

    needs_secrets = tts_provider == "elevenlabs" or (
        not tts_shorthand
        and chat_provider != "stub"
        and (payload.api_key is not None or payload.base_url is not None)
    )
    fernet = None
    if needs_secrets:
        try:
            fernet = get_fernet(settings.DRAVEN_CONFIG_KEY)
        except ValueError as exc:
            # Fail closed: never store plaintext secrets.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    row = _business_config(db, admin.business_id)
    if row is None:
        row = DravenProviderConfig(
            business_id=admin.business_id,
            provider="stub" if (tts_shorthand or tts_only) else chat_provider,
            model=None if (tts_shorthand or tts_only) else payload.model,
        )
        db.add(row)
    elif not tts_shorthand and not tts_only:
        row.provider = chat_provider
        row.model = payload.model
    if tts_provider == "elevenlabs":
        # needs_secrets guarantees fernet is set here (fail-closed above).
        row.tts_provider = "elevenlabs"
        row.tts_api_key_enc = encrypt_secret(fernet, tts_api_key)
    elif tts_provider == "none":
        # Clearing needs no encryption key.
        row.tts_provider = None
        row.tts_api_key_enc = None
    if not tts_shorthand and not tts_only:
        if fernet is not None:
            row.api_key_enc = encrypt_secret(fernet, payload.api_key)
            row.base_url_enc = encrypt_secret(fernet, payload.base_url)
        else:
            row.api_key_enc = None
            row.base_url_enc = None
    # Write-through: the vault is the canonical store for key material.
    # The legacy columns above stay as the deprecated mirror.
    ck = settings.DRAVEN_CONFIG_KEY
    actor = f"user:{admin.id}"
    if tts_provider == "elevenlabs" and tts_api_key:
        vault.put_secret(
            db, admin.business_id, "elevenlabs.api_key", tts_api_key,
            actor=actor, config_key=ck,
            label="ElevenLabs API key (Draven provider panel)",
        )
    elif tts_provider == "none":
        vault.delete_secret(
            db, admin.business_id, "elevenlabs.api_key",
            actor=actor, config_key=ck,
        )
    if not tts_shorthand and not tts_only and fernet is not None:
        if chat_provider == "anthropic" and payload.api_key:
            vault.put_secret(
                db, admin.business_id, "anthropic.api_key", payload.api_key,
                actor=actor, config_key=ck,
                label="Anthropic API key (Draven provider panel)",
            )
        elif chat_provider == "gemini" and payload.api_key:
            vault.put_secret(
                db, admin.business_id, "gemini.api_key", payload.api_key,
                actor=actor, config_key=ck,
                label="Gemini API key (Draven provider panel)",
            )
        elif chat_provider == "openrouter" and payload.api_key:
            vault.put_secret(
                db, admin.business_id, "openrouter.api_key", payload.api_key,
                actor=actor, config_key=ck,
                label="OpenRouter API key (Draven provider panel)",
            )
        elif chat_provider == "ollama":
            if payload.base_url:
                vault.put_secret(
                    db, admin.business_id, "ollama.base_url", payload.base_url,
                    actor=actor, config_key=ck,
                    label="Ollama base URL (Draven provider panel)",
                )
        elif chat_provider == "openai_compatible":
            if payload.api_key:
                vault.put_secret(
                    db, admin.business_id, "openai.api_key", payload.api_key,
                    actor=actor, config_key=ck,
                    label="OpenAI-compatible API key (Draven provider panel)",
                )
            if payload.base_url:
                vault.put_secret(
                    db, admin.business_id, "openai.base_url", payload.base_url,
                    actor=actor, config_key=ck,
                    label="OpenAI-compatible base URL (Draven provider panel)",
                )
    db.commit()
    db.refresh(row)

    _key_name = {
        "anthropic": "anthropic.api_key",
        "gemini": "gemini.api_key",
        "openrouter": "openrouter.api_key",
    }.get(row.provider, "openai.api_key")
    _base_name = {
        "ollama": "ollama.base_url",
    }.get(row.provider, "openai.base_url")
    return ProviderConfigOut(
        provider=row.provider,
        model=row.model,
        has_api_key=(
            vault.get_secret(db, admin.business_id, _key_name, ck)
            is not None or row.api_key_enc is not None
        ),
        has_base_url=(
            vault.get_secret(db, admin.business_id, _base_name, ck)
            is not None or row.base_url_enc is not None
        ),
        tts=_tts_status(db, admin, settings),
        updated_at=row.updated_at,
    )


@router.post("/provider/test", response_model=ProviderTestOut)
async def test_provider(
    admin: AdminUser,
    db: DbSession,
    settings: CurrentSettings,
) -> ProviderTestOut:
    """Run a tiny generation through the configured provider (owner/admin).

    Returns ok/latency/error. Never returns secret values.
    """
    try:
        provider, provider_name, provider_configured = _resolve_provider(
            db, admin, settings
        )
    except ValueError as exc:
        return ProviderTestOut(ok=False, latency_ms=None, error=str(exc)[:300])
    if not provider_configured:
        return ProviderTestOut(
            ok=False,
            latency_ms=None,
            error=(
                f"provider '{provider_name}' is not configured — add its "
                "credentials in Settings → AI provider, then test again."
            ),
        )

    started = datetime.now(timezone.utc)
    try:
        gen = await asyncio.wait_for(
            provider.generate(
                GenerationRequest(
                    prompt="Reply with the word OK",
                    max_tokens=10,
                    temperature=0,
                )
            ),
            timeout=15,
        )
        latency_ms = float(gen.latency_ms)
        ok = "ok" in gen.text.strip().lower()
        _last_test_latency[admin.business_id] = (latency_ms, started)
        return ProviderTestOut(
            ok=ok,
            latency_ms=latency_ms,
            error=None if ok else f"unexpected reply: {gen.text.strip()[:120]}",
        )
    except asyncio.TimeoutError:
        latency_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        return ProviderTestOut(
            ok=False, latency_ms=latency_ms, error="provider timed out after 15s"
        )
    except Exception as exc:
        latency_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        return ProviderTestOut(
            ok=False, latency_ms=latency_ms, error=str(exc)[:300]
        )


# ---------------------------------------------------------------------------
# ElevenLabs TTS endpoints
# ---------------------------------------------------------------------------

# Allowed output formats: (media_type, is_lossless_pcm).
# Default is MP3 HD: it plays on every customer device (WAV caused support
# confusion on some devices). Lossless WAV stays available for archival use.
_TTS_FORMATS: dict[str, tuple[str, bool]] = {
    "pcm_44100": ("audio/wav", True),  # 44.1kHz 16-bit PCM, lossless
    "mp3_44100_192": ("audio/mpeg", False),  # 192kbps MP3, near-transparent
    "mp3_44100_128": ("audio/mpeg", False),  # 128kbps MP3, bandwidth saver
}


def _pcm_to_wav(pcm: bytes, sample_rate: int = 44100) -> bytes:
    """Wrap raw 16-bit mono PCM bytes in a WAV container.

    ElevenLabs' pcm_* outputs are 16-bit little-endian mono at the requested
    sample rate. The WAV header makes the lossless stream playable anywhere.
    """
    import struct

    num_channels, bits_per_sample = 1, 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        len(pcm),
    )
    return header + pcm


@router.get("/tts/voices", response_model=VoicesOut)
async def tts_voices(
    user: CurrentUser, db: DbSession, settings: CurrentSettings
) -> VoicesOut:
    """Live ElevenLabs voice list (cached in-process for 1 hour).

    Returns {configured: false, voices: []} when TTS is not configured.
    Upstream failures become a 502 — never a fabricated list.
    """
    api_key = _elevenlabs_tts_key(db, user, settings)
    if api_key is None:
        return VoicesOut(configured=False, voices=[])
    voices = await _cached_voices(db, user, settings, api_key)
    if voices is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ElevenLabs voice list unavailable (upstream error)",
        )
    return VoicesOut(
        configured=True, voices=[VoiceInfo(**v) for v in voices]
    )


@router.post("/tts/speak")
async def tts_speak(
    payload: SpeakIn,
    user: CurrentUser,
    db: DbSession,
    settings: CurrentSettings,
) -> Response:
    """Synthesize speech via ElevenLabs; MP3 HD 192kbps by default.

    Guards: text length (2000 chars, enforced by schema), strict voice_id
    format, membership in the cached voice list when available, per-business
    rate limit (20 req/min), output_format allowlist. Character usage +
    estimated cost are written to the Draven audit log. The API key never
    appears in responses or logs.

    Quality: ``mp3_44100_192`` is the default — near-transparent 192kbps
    MP3 that plays on every customer device (WAV caused support confusion
    on some devices). ``pcm_44100`` returns bit-perfect 44.1kHz 16-bit PCM
    in a WAV container for archival quality; ``mp3_44100_128`` is the
    bandwidth saver (e.g. satellite links on yachts/jets).
    """
    text = payload.text.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="text must not be blank",
        )
    if payload.voice_id is not None and not _VOICE_ID_RE.match(payload.voice_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid voice_id format",
        )
    fmt = _TTS_FORMATS.get(payload.output_format)
    if fmt is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown output_format (choose from {sorted(_TTS_FORMATS)})",
        )
    media_type, is_pcm = fmt
    if not _check_tts_rate_limit(user.business_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="TTS rate limit exceeded (20 requests/minute per business)",
        )

    api_key = _elevenlabs_tts_key(db, user, settings)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TTS is not configured for this business "
            "(PUT /draven/provider with provider=elevenlabs)",
        )

    # Validate the voice against the live/cached list rather than
    # proxying an arbitrary path. Fetch failure -> 502, unknown id -> 400.
    voices = await _cached_voices(db, user, settings, api_key)
    if voices is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="could not validate voice_id (ElevenLabs upstream error)",
        )
    # No voice selected yet (e.g. fresh setup): fall back to the account's
    # first voice instead of failing the request — the UI stayed silent.
    voice_id = payload.voice_id or (voices[0].get("voice_id") if voices else None)
    if voice_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no voices available on this ElevenLabs account",
        )
    if voice_id not in {v.get("voice_id") for v in voices}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unknown voice_id for this account",
        )

    path = _ELEVENLABS_TTS_PATH.format(voice_id=voice_id)
    started = time.monotonic()
    try:
        resp = await _elevenlabs_api(
            "POST",
            path,
            api_key,
            json_body={
                "text": text,
                "model_id": payload.model_id,
                "output_format": payload.output_format,
            },
            timeout=30.0,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"ElevenLabs TTS request failed: {type(exc).__name__}",
        ) from exc
    duration_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"ElevenLabs TTS failed ({resp.status_code}): "
            f"{resp.text[:200]}",
        )

    chars = len(text)
    cost_usd = _elevenlabs_cost_estimate(payload.model_id, chars)
    write_audit_row(
        db,
        user,
        "draven.tts_speak",
        "low",
        {"voice_id": voice_id, "model_id": payload.model_id,
         "output_format": payload.output_format, "chars": chars},
        {
            "tool": "draven.tts_speak",
            "risk": "low",
            "status": "ok",
            "output": {
                "chars": chars,
                "output_format": payload.output_format,
                "lossless": is_pcm,
                "estimated_cost_usd": round(cost_usd, 6),
                "note": "cost is an estimate from ElevenLabs published API rates",
            },
            "duration_ms": duration_ms,
        },
        duration_ms,
    )
    audio = _pcm_to_wav(resp.content) if is_pcm else resp.content
    return Response(content=audio, media_type=media_type)


@router.get("/audit", response_model=AuditOut)
def audit_log(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
) -> AuditOut:
    """Recent Draven tool runs for the caller's business (newest first)."""
    base = (
        db.query(DravenToolRun)
        .filter(DravenToolRun.business_id == user.business_id)
        .order_by(DravenToolRun.created_at.desc())
    )
    total = base.count()
    rows = base.limit(limit).all()
    return AuditOut(
        items=[
            AuditItem(
                tool=r.tool,
                risk=r.risk,
                status=r.status,
                duration_ms=r.duration_ms,
                created_at=r.created_at,
                output_summary=(r.output_summary or "")[:300],
            )
            for r in rows
        ],
        total=total,
    )


@router.post("/stop", response_model=StopOut)
def emergency_stop(user: CurrentUser) -> StopOut:
    """Emergency stop: block new Draven tool executions for 60 seconds.

    In-process scope only — in a multi-worker deployment each API process
    holds its own flag. A persistent, cross-process stop belongs in Redis
    or the database (documented limitation, not silent behavior).
    """
    global _kill_switch_at
    _kill_switch_at = datetime.now(timezone.utc)
    return StopOut(stopped=True, at=_kill_switch_at)


# ---------------------------------------------------------------------------
# Swarm endpoints
# ---------------------------------------------------------------------------


class SwarmAgentInfo(BaseModel):
    id: str
    name: str
    role: str
    allowed_tools: list[str]
    max_depth: int


class SwarmRunIn(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    context: dict[str, Any] = Field(default_factory=dict)
    demo: bool = Field(
        default=False,
        description="Use the seeded investor demo scenario as the goal.",
    )


class SwarmRunOut(BaseModel):
    run_id: str
    status: str


class SwarmRunDetail(BaseModel):
    run_id: str
    goal: str
    status: str
    current_phase: str | None
    agent_results: dict[str, Any]
    result_summary: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class SwarmEventOut(BaseModel):
    seq: int
    agent_id: str | None
    kind: str
    message: str
    data: dict[str, Any]
    created_at: datetime


class SwarmEventsOut(BaseModel):
    events: list[SwarmEventOut]
    latest_seq: int


def _swarm_stopped() -> bool:
    return (
        _kill_switch_at is not None
        and (datetime.now(timezone.utc) - _kill_switch_at).total_seconds() < 60
    )


def _get_swarm_run(
    db: Session, user: CurrentUser, run_id: str
) -> DravenSwarmRun:
    try:
        rid = uuid.UUID(str(run_id))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="swarm run not found"
        )
    run = (
        db.query(DravenSwarmRun)
        .filter(
            DravenSwarmRun.id == rid,
            DravenSwarmRun.business_id == user.business_id,
        )
        .first()
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="swarm run not found"
        )
    return run


async def _swarm_task(
    run_id: uuid.UUID, user_id: uuid.UUID, settings
) -> None:
    """Background swarm execution with its own sessions (never the request's).

    Every failure path marks the run failed with an honest error — a run
    never hangs silently, which is what the stage demo depends on.
    """
    factory = SessionLocal(settings.DATABASE_URL)
    db = factory()
    try:
        run = (
            db.query(DravenSwarmRun)
            .filter(DravenSwarmRun.id == run_id)
            .first()
        )
        user = db.query(User).filter(User.id == user_id).first()
        if run is None or user is None:
            return
        goal = run.goal
        context = run.context or {}
        try:
            provider, provider_name, provider_configured = _resolve_provider(
                db, user, settings
            )
        except ValueError as exc:
            # Honest degradation: say what's missing, run nothing.
            run.status = DravenSwarmRunStatus.failed
            run.error = f"LLM provider misconfigured: {exc}"
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return
        # Detach-safe: business_id/id are loaded (expire_on_commit=False).
        db.expunge(user)
        db.expunge(run)
    finally:
        db.close()

    await swarm.run_swarm(
        factory, user, provider, provider_name, run_id, goal, context,
        provider_configured=provider_configured,
    )


@router.get("/swarm/agents", response_model=list[SwarmAgentInfo])
def swarm_agents(user: CurrentUser) -> list[SwarmAgentInfo]:
    """The 12 registered swarm agents (id, name, role, toolset, depth)."""
    return [SwarmAgentInfo(**d) for d in swarm.agent_definitions()]


@router.post("/swarm/run", response_model=SwarmRunOut, status_code=202)
@quota_limited("draven")
async def swarm_run(
    payload: SwarmRunIn,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    settings: CurrentSettings,
    background: BackgroundTasks,
) -> SwarmRunOut:
    """Start a swarm run (202 + run_id; execution continues in background).

    ``demo=true`` runs the seeded investor scenario. Poll
    ``GET /swarm/runs/{id}/events`` for the live activity feed.
    """
    if _swarm_stopped():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Draven is stopped (emergency stop active) — no new runs.",
        )
    if payload.demo:
        goal, context = swarm.demo_goal()
        context = {**context, **(payload.context or {})}
    else:
        goal, context = payload.goal, payload.context or {}

    run = DravenSwarmRun(
        business_id=user.business_id,
        user_id=user.id,
        goal=goal,
        context=context,
        status=DravenSwarmRunStatus.queued,
        current_phase="queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    background.add_task(_swarm_task, run.id, user.id, settings)
    return SwarmRunOut(run_id=str(run.id), status=run.status.value)


@router.get("/swarm/runs/{run_id}", response_model=SwarmRunDetail)
def swarm_run_status(
    run_id: str, user: CurrentUser, db: DbSession
) -> SwarmRunDetail:
    """Run status + per-agent results + synthesized summary."""
    run = _get_swarm_run(db, user, run_id)
    return SwarmRunDetail(
        run_id=str(run.id),
        goal=run.goal,
        status=run.status.value,
        current_phase=run.current_phase,
        agent_results=run.agent_results or {},
        result_summary=run.result_summary,
        error=run.error,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


@router.get("/swarm/runs/{run_id}/events", response_model=SwarmEventsOut)
def swarm_run_events(
    run_id: str,
    user: CurrentUser,
    db: DbSession,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> SwarmEventsOut:
    """Live activity feed for a run. Poll with ``?after=<latest_seq>``."""
    run = _get_swarm_run(db, user, run_id)
    rows = (
        db.query(DravenSwarmEvent)
        .filter(
            DravenSwarmEvent.swarm_run_id == run.id,
            DravenSwarmEvent.seq > after,
        )
        .order_by(DravenSwarmEvent.seq.asc())
        .limit(limit)
        .all()
    )
    latest = (
        db.query(DravenSwarmEvent.seq)
        .filter(DravenSwarmEvent.swarm_run_id == run.id)
        .order_by(DravenSwarmEvent.seq.desc())
        .first()
    )
    return SwarmEventsOut(
        events=[
            SwarmEventOut(
                seq=r.seq,
                agent_id=r.agent_id,
                kind=r.kind,
                message=r.message,
                data=r.data or {},
                created_at=r.created_at,
            )
            for r in rows
        ],
        latest_seq=latest[0] if latest else 0,
    )
