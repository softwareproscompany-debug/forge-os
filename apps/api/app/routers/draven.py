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
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forge_db.models import DravenProviderConfig, DravenToolRun, User
from forge_llm import GenerationRequest, get_provider

from app.core.deps import CurrentSettings, CurrentUser, DbSession, require_role
from app.draven_crypto import decrypt_secret, encrypt_secret, get_fernet
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

_ALLOWED_PROVIDERS = ("stub", "anthropic", "openai_compatible")

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
    """Decrypted ElevenLabs API key for the business, or None if unconfigured."""
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

    Chat LLM provider: ``provider`` in {"stub", "anthropic",
    "openai_compatible"}. Passing ``provider="elevenlabs"`` (optionally with
    ``api_key``) is accepted as a shorthand that configures the TTS voice
    provider instead, leaving the chat provider untouched.
    Explicit ``tts_provider``/``tts_api_key`` fields do the same job.
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
    voice_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field(default="eleven_multilingual_v2", max_length=64)


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
    row: DravenProviderConfig, settings
) -> tuple[Any, str | None]:
    """Build an LLM provider from a decrypted business config row.

    Returns (provider, model). Raises ValueError on missing/invalid config.
    """
    from forge_llm.providers import (
        AnthropicProvider,
        OpenAICompatibleProvider,
        StubProvider,
    )

    fernet = get_fernet(settings.DRAVEN_CONFIG_KEY)
    if row.provider == "stub":
        return StubProvider(), "stub"
    if row.provider == "anthropic":
        api_key = decrypt_secret(fernet, row.api_key_enc)
        if not api_key:
            raise ValueError("anthropic provider is missing its API key")
        return AnthropicProvider(api_key=api_key, model=row.model), row.model
    if row.provider == "openai_compatible":
        base_url = decrypt_secret(fernet, row.base_url_enc)
        api_key = decrypt_secret(fernet, row.api_key_enc)
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
) -> tuple[Any, str]:
    """Business config wins; otherwise fall back to the global env provider.

    Reads the DB per request so multi-worker deployments stay consistent.
    Returns (provider_instance, provider_display_name).
    """
    row = _business_config(db, user.business_id)
    if row is not None:
        provider, _ = _build_provider_from_config(row, settings)
        return provider, provider.name
    provider = get_provider()
    return provider, provider.name


def _tts_status(
    db: Session, user: CurrentUser
) -> TtsStatus:
    """TTS section for provider status/config responses. Never key material."""
    row = _business_config(db, user.business_id)
    tts_provider = (row.tts_provider if row else None) or "none"
    configured = tts_provider == "elevenlabs" and bool(
        row and row.tts_api_key_enc
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
    tts = _tts_status(db, user)
    if row is not None:
        if row.provider == "stub":
            configured = True
        elif row.provider == "anthropic":
            configured = row.api_key_enc is not None
        else:  # openai_compatible
            configured = row.base_url_enc is not None and bool(row.model)
        return ProviderStatus(
            provider=row.provider,
            model=row.model,
            configured=configured,
            latency_ms=latency_ms,
            tts=tts,
        )
    # No business config — report the global env provider.
    name = settings.LLM_PROVIDER
    if name == "anthropic":
        configured = bool(settings.ANTHROPIC_API_KEY)
        model = settings.ANTHROPIC_MODEL
    elif name == "openai_compatible":
        configured = bool(settings.OPENAI_COMPAT_BASE_URL) and bool(
            settings.OPENAI_COMPAT_MODEL
        )
        model = settings.OPENAI_COMPAT_MODEL or None
    else:
        configured = True
        model = "stub"
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


_DRAVEN_SYSTEM = (
    "You are Draven, the voice-first executive assistant inside ForgeOS, a "
    "marketing automation platform. Summarize ONLY using the tool results "
    "below — never invent numbers, names, or statuses. If a result needs "
    "human approval, say so plainly. Keep the reply under 120 words, plain "
    "text, no markdown, no emojis."
)


def _deterministic_reply(results: list[dict[str, Any]]) -> str:
    """Compose a reply from real tool outputs when no LLM is configured."""
    parts: list[str] = []
    for r in results:
        tid = r["tool"]
        status_ = r["status"]
        if status_ == "error":
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
    if not parts:
        return (
            "I didn't find anything to act on. I can check approvals, "
            "campaigns, analytics, contacts, or autopilot — what do you need?"
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
async def chat(
    payload: DravenChatRequest,
    user: CurrentUser,
    db: DbSession,
    settings: CurrentSettings,
) -> DravenChatResponse:
    global _kill_switch_at
    now = datetime.now(timezone.utc)

    # Emergency stop: refuse new tool executions for 60s after POST /stop.
    if _kill_switch_at is not None and (now - _kill_switch_at).total_seconds() < 60:
        return DravenChatResponse(
            reply=(
                "Draven is stopped — the emergency stop is active. "
                "No tools were run. Say the word when you're ready to resume."
            ),
            tools_used=[],
            approvals_needed=[],
            estimated_cost_usd=0.0,
            provider="stopped",
        )

    routed = route_intent(payload.message)
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

    provider, provider_name = _resolve_provider(db, user, settings)

    # Compose the reply: real provider when configured, deterministic
    # real-data summary in stub mode.
    estimated_cost = 0.0
    if provider_name == "stub":
        reply = _deterministic_reply(results)
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
                f"{'User' if m.role == 'user' else 'Draven'}: {m.content}"
                for m in payload.history[-8:]
            )
            prompt = (
                f"User message: {payload.message}\n\n"
                f"Tool results (JSON):\n{json.dumps(slim, default=str)}\n\n"
                f"Draven:"
            )
            if history_text:
                prompt = f"Conversation so far:\n{history_text}\n\n{prompt}"
            gen = await provider.generate(
                GenerationRequest(
                    prompt=prompt,
                    system_prompt=_DRAVEN_SYSTEM,
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
    ``null`` clears that field. ``provider="stub"`` needs no secrets and no
    ``DRAVEN_CONFIG_KEY``. ``provider="elevenlabs"`` (or explicit
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
    if not tts_shorthand:
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
            provider="stub" if tts_shorthand else chat_provider,
            model=None if tts_shorthand else payload.model,
        )
        db.add(row)
    elif not tts_shorthand:
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
    if not tts_shorthand:
        if fernet is not None:
            row.api_key_enc = encrypt_secret(fernet, payload.api_key)
            row.base_url_enc = encrypt_secret(fernet, payload.base_url)
        else:
            row.api_key_enc = None
            row.base_url_enc = None
    db.commit()
    db.refresh(row)

    return ProviderConfigOut(
        provider=row.provider,
        model=row.model,
        has_api_key=row.api_key_enc is not None,
        has_base_url=row.base_url_enc is not None,
        tts=_tts_status(db, admin),
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
        provider, _ = _resolve_provider(db, admin, settings)
    except ValueError as exc:
        return ProviderTestOut(ok=False, latency_ms=None, error=str(exc)[:300])

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
    """Synthesize speech via ElevenLabs; returns audio/mpeg.

    Guards: text length (2000 chars, enforced by schema), strict voice_id
    format, membership in the cached voice list when available, per-business
    rate limit (20 req/min). Character usage + estimated cost are written
    to the Draven audit log. The API key never appears in responses or logs.
    """
    text = payload.text.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="text must not be blank",
        )
    if not _VOICE_ID_RE.match(payload.voice_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid voice_id format",
        )
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
    if payload.voice_id not in {v.get("voice_id") for v in voices}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unknown voice_id for this account",
        )

    path = _ELEVENLABS_TTS_PATH.format(voice_id=payload.voice_id)
    started = time.monotonic()
    try:
        resp = await _elevenlabs_api(
            "POST",
            path,
            api_key,
            json_body={"text": text, "model_id": payload.model_id},
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
        {"voice_id": payload.voice_id, "model_id": payload.model_id, "chars": chars},
        {
            "tool": "draven.tts_speak",
            "risk": "low",
            "status": "ok",
            "output": {
                "chars": chars,
                "estimated_cost_usd": round(cost_usd, 6),
                "note": "cost is an estimate from ElevenLabs published API rates",
            },
            "duration_ms": duration_ms,
        },
        duration_ms,
    )
    return Response(content=resp.content, media_type="audio/mpeg")


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
