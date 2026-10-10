"""Settings vault API — centralized encrypted API-key management.

Admin-only (owner/admin), rate-limited, audit-logged. Secret VALUES never
appear in any response, log, or error message — only metadata and masked
hints (``••••3f9a``).

* ``GET /settings/sections`` — all integrations with configured/not status
* ``PUT /settings/secrets/{key_name}`` — create/update a secret
* ``DELETE /settings/secrets/{key_name}`` — remove a secret
* ``POST /settings/secrets/{key_name}/test`` — live verification against
  the real provider (mocked HTTP in tests, real in production)
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forge_db.audit import log_action
from app.core.deps import CurrentSettings, DbSession, require_role
from app.core.rate_limit import client_ip
from app.draven_tools import write_audit_row
from app.settings_vault import service as vault
from forge_db.models import (
    BusinessSecret,
    DravenProviderConfig,
    LeadSource,
    User,
)

router = APIRouter(prefix="/settings", tags=["settings"])

AdminUser = Annotated[User, Depends(require_role("owner", "admin"))]

# ---------------------------------------------------------------------------
# Rate limiting (in-process sliding window; Redis for multi-worker — documented)
# ---------------------------------------------------------------------------

_rate: dict[tuple[uuid.UUID, str], list[float]] = {}
_RATE_LIMITS = {"read": (120, 60.0), "write": (30, 60.0), "test": (20, 60.0)}


def _check_rate(business_id: uuid.UUID, bucket: str) -> None:
    limit, window = _RATE_LIMITS[bucket]
    now = time.monotonic()
    key = (business_id, bucket)
    stamps = [s for s in _rate.get(key, []) if s > now - window]
    if len(stamps) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="settings rate limit exceeded — slow down",
        )
    stamps.append(now)
    _rate[key] = stamps


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SecretStatusOut(BaseModel):
    key_name: str
    label: str
    kind: str
    section: str
    configured: bool
    hint: str | None
    last_verified_at: str | None
    help: str = ""


class SectionOut(BaseModel):
    id: str
    label: str
    icon: str
    blurb: str
    secrets: list[SecretStatusOut]


class SectionsOut(BaseModel):
    sections: list[SectionOut]


class SecretPutIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, max_length=128)


class SecretWriteOut(BaseModel):
    key_name: str
    configured: bool
    hint: str | None


class SecretTestOut(BaseModel):
    ok: bool
    latency_ms: float | None = None
    detail: str | None = None


def _cfg_key(settings: CurrentSettings) -> str:
    return settings.DRAVEN_CONFIG_KEY


def _audit(
    db: Session,
    user: User,
    tool: str,
    key_name: str,
    result: dict[str, Any],
    duration_ms: int = 0,
) -> None:
    # raw_input carries only the key name — never the value.
    write_audit_row(
        db, user, tool, "medium", {"key_name": key_name}, result, duration_ms
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


@router.get("/sections", response_model=SectionsOut)
def list_sections(
    admin: AdminUser, db: DbSession, settings: CurrentSettings
) -> SectionsOut:
    """Every integration with its secrets' status (never values)."""
    _check_rate(admin.business_id, "read")
    ck = _cfg_key(settings)
    # One pass: migrate anything still living in legacy columns so the
    # listing reflects reality.
    vault.ensure_vault_migrated(db, admin.business_id, ck)

    by_key = {
        r.key_name: r
        for r in db.query(BusinessSecret)
        .filter(BusinessSecret.business_id == admin.business_id)
        .all()
    }
    # Webhook sources that exist as lead sources but have no vault row yet
    # (shouldn't happen after migration, but be explicit).
    webhook_sources = sorted(
        {r.key_name for r in by_key.values() if r.key_name.startswith("webhook.")}
        | {
            f"webhook.{s.source}.secret"
            for s in db.query(LeadSource)
            .filter(LeadSource.business_id == admin.business_id)
            .all()
        }
    )

    sections: list[SectionOut] = []
    for section in vault.SECTIONS:
        secrets: list[SecretStatusOut] = []
        for spec in vault.KNOWN_SECRETS:
            if spec.section != section["id"]:
                continue
            secrets.append(
                SecretStatusOut(
                    **vault.secret_status(
                        db, admin.business_id, spec.key, ck
                    )
                )
            )
        if section["id"] == "webhooks":
            for key_name in webhook_sources:
                try:
                    secrets.append(
                        SecretStatusOut(
                            **vault.secret_status(
                                db, admin.business_id, key_name, ck
                            )
                        )
                    )
                except ValueError:
                    continue  # skip malformed legacy key names
        sections.append(SectionOut(
            id=section["id"],
            label=section["label"],
            icon=section["icon"],
            blurb=section["blurb"],
            secrets=secrets,
        ))
    db.commit()
    return SectionsOut(sections=sections)


# ---------------------------------------------------------------------------
# Write / delete
# ---------------------------------------------------------------------------


def _write_through_legacy(
    db: Session,
    business_id: uuid.UUID,
    key_name: str,
    token_enc: str,
) -> None:
    """Mirror a vault write into the deprecated legacy columns.

    Write-through keeps old readers working during the migration window.
    Never changes provider *selection* — only key material. ``token_enc``
    is already a Fernet token, so it is copied as-is (legacy columns hold
    the same token format under the same master key).
    """
    row = (
        db.query(DravenProviderConfig)
        .filter(DravenProviderConfig.business_id == business_id)
        .first()
    )
    if row is None:
        row = DravenProviderConfig(business_id=business_id, provider="stub")
        db.add(row)
    # token_enc is already the Fernet token; legacy columns hold the same
    # tokens, so copy it directly.
    if key_name == "elevenlabs.api_key":
        row.tts_api_key_enc = token_enc
        if not row.tts_provider:
            row.tts_provider = "elevenlabs"
    elif key_name == "anthropic.api_key" and row.provider == "anthropic":
        row.api_key_enc = token_enc
    elif key_name == "openai.api_key" and row.provider == "openai_compatible":
        row.api_key_enc = token_enc
    elif key_name == "openai.base_url" and row.provider == "openai_compatible":
        row.base_url_enc = token_enc
    db.flush()


@router.put("/secrets/{key_name}", response_model=SecretWriteOut)
def put_secret(
    key_name: str,
    payload: SecretPutIn,
    request: Request,
    admin: AdminUser,
    db: DbSession,
    settings: CurrentSettings,
) -> SecretWriteOut:
    """Create or update a secret. The value is encrypted before storage."""
    _check_rate(admin.business_id, "write")
    ck = _cfg_key(settings)
    try:
        spec = vault.validate_key_name(key_name)
        vault.validate_value(spec, payload.value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    try:
        row = vault.put_secret(
            db,
            admin.business_id,
            key_name,
            payload.value,
            actor=f"user:{admin.id}",
            config_key=ck,
            label=payload.label,
        )
    except ValueError as exc:
        # Fail closed: missing/malformed master key -> no plaintext storage.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    # Write-through to deprecated legacy columns (migration window).
    if key_name in (
        "elevenlabs.api_key", "anthropic.api_key",
        "openai.api_key", "openai.base_url",
    ):
        _write_through_legacy(db, admin.business_id, key_name, row.value_enc)
    _audit(
        db, admin, "settings.secret_put", key_name,
        {"status": "ok", "output": {"key_name": key_name}},
    )
    log_action(
        db,
        action="secrets.saved",
        actor_id=str(admin.id),
        actor_email=admin.email,
        business_id=admin.business_id,
        resource_type="secret",
        resource_id=key_name,
        details={"key_name": key_name},
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    st = vault.secret_status(db, admin.business_id, key_name, ck)
    return SecretWriteOut(
        key_name=key_name, configured=st["configured"], hint=st["hint"]
    )


@router.delete("/secrets/{key_name}", response_model=SecretWriteOut)
def delete_secret(
    key_name: str,
    request: Request,
    admin: AdminUser,
    db: DbSession,
    settings: CurrentSettings,
) -> SecretWriteOut:
    """Delete a secret. Afterwards it is unusable everywhere."""
    _check_rate(admin.business_id, "write")
    ck = _cfg_key(settings)
    try:
        removed = vault.delete_secret(
            db, admin.business_id, key_name,
            actor=f"user:{admin.id}", config_key=ck,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="secret not found"
        )
    # Clear the deprecated legacy mirror too.
    row = (
        db.query(DravenProviderConfig)
        .filter(DravenProviderConfig.business_id == admin.business_id)
        .first()
    )
    if row is not None:
        if key_name == "elevenlabs.api_key":
            row.tts_api_key_enc = None
        elif key_name in ("anthropic.api_key", "openai.api_key"):
            row.api_key_enc = None
        elif key_name == "openai.base_url":
            row.base_url_enc = None
    _audit(
        db, admin, "settings.secret_delete", key_name,
        {"status": "ok", "output": {"key_name": key_name}},
    )
    log_action(
        db,
        action="secrets.removed",
        actor_id=str(admin.id),
        actor_email=admin.email,
        business_id=admin.business_id,
        resource_type="secret",
        resource_id=key_name,
        details={"key_name": key_name},
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    return SecretWriteOut(key_name=key_name, configured=False, hint=None)


# ---------------------------------------------------------------------------
# Live verification
# ---------------------------------------------------------------------------


async def _test_elevenlabs(api_key: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": api_key},
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return False, f"network error: {type(exc).__name__}"
    if r.status_code == 200:
        try:
            n = len(r.json().get("voices", []))
        except Exception:
            n = 0
        return True, f"connected — {n} voices available"
    if r.status_code == 401:
        return False, "invalid API key (401 unauthorized)"
    return False, f"ElevenLabs error ({r.status_code})"


async def _test_anthropic(api_key: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return False, f"network error: {type(exc).__name__}"
    if r.status_code == 200:
        return True, "connected — models endpoint reachable"
    if r.status_code == 401:
        return False, "invalid API key (401 unauthorized)"
    return False, f"Anthropic error ({r.status_code})"


async def _test_openai(api_key: str, base_url: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return False, f"network error: {type(exc).__name__}"
    if r.status_code == 200:
        return True, "connected — models endpoint reachable"
    if r.status_code == 401:
        return False, "invalid API key (401 unauthorized)"
    return False, f"provider error ({r.status_code})"


async def _test_dataforseo(login: str, password: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.dataforseo.com/v3/appendix/user_data",
                auth=(login, password),
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return False, f"network error: {type(exc).__name__}"
    if r.status_code == 200:
        try:
            balance = (
                r.json().get("tasks", [{}])[0]
                .get("result", [{}])[0]
                .get("money", {})
                .get("balance")
            )
        except Exception:
            balance = None
        detail = "connected"
        if balance is not None:
            detail += f" — balance ${balance}"
        return True, detail
    if r.status_code == 401:
        return False, "invalid credentials (401 unauthorized)"
    return False, f"DataForSEO error ({r.status_code})"


async def _test_brave(api_key: str) -> tuple[bool, str]:
    """Live-verify a Brave Search API key with a minimal query."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": "test", "count": 1},
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                },
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return False, f"network error: {type(exc).__name__}"
    if r.status_code == 200:
        return True, "connected — Brave Search API key is valid"
    if r.status_code == 401:
        return False, "invalid API key (401 unauthorized)"
    if r.status_code == 422:
        return False, "request rejected (422) — check API key format"
    return False, f"Brave Search error ({r.status_code})"


def _test_webhook(secret: str) -> tuple[bool, str]:
    """HMAC self-check: sign a canary and verify with the real verifier."""
    from app.alpha.service import verify_webhook_signature

    canary = b"forgeos-settings-vault-self-check"
    sig = "sha256=" + hmac.new(secret.encode(), canary, hashlib.sha256).hexdigest()
    if verify_webhook_signature(secret, canary, sig):
        return True, "HMAC self-check passed — secret signs and verifies"
    return False, "HMAC self-check failed"


@router.post("/secrets/{key_name}/test", response_model=SecretTestOut)
async def test_secret(
    key_name: str, admin: AdminUser, db: DbSession, settings: CurrentSettings
) -> SecretTestOut:
    """Live-verify a secret against its real provider.

    Hits the actual provider API shape (mocked HTTP in tests). A test that
    always returns ok would be a lie — failures surface here with the
    provider's real status.
    """
    _check_rate(admin.business_id, "test")
    ck = _cfg_key(settings)
    try:
        vault.validate_key_name(key_name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    value = vault.get_secret(db, admin.business_id, key_name, ck)
    if not value:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="secret not configured — save it before testing",
        )

    started = time.monotonic()
    if key_name == "elevenlabs.api_key":
        ok, detail = await _test_elevenlabs(value)
    elif key_name == "anthropic.api_key":
        ok, detail = await _test_anthropic(value)
    elif key_name == "openai.api_key":
        base_url = vault.get_secret(
            db, admin.business_id, "openai.base_url", ck
        )
        if not base_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="configure openai.base_url before testing the key",
            )
        ok, detail = await _test_openai(value, base_url)
    elif key_name == "openai.base_url":
        api_key = vault.get_secret(
            db, admin.business_id, "openai.api_key", ck
        )
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="configure openai.api_key before testing the URL",
            )
        ok, detail = await _test_openai(api_key, value)
    elif key_name == "dataforseo.login":
        password = vault.get_secret(
            db, admin.business_id, "dataforseo.password", ck
        )
        if not password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="configure dataforseo.password before testing",
            )
        ok, detail = await _test_dataforseo(value, password)
    elif key_name == "dataforseo.password":
        login = vault.get_secret(
            db, admin.business_id, "dataforseo.login", ck
        )
        if not login:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="configure dataforseo.login before testing",
            )
        ok, detail = await _test_dataforseo(login, value)
    elif key_name == "brave.api_key":
        ok, detail = await _test_brave(value)
    elif key_name.startswith("webhook."):
        ok, detail = _test_webhook(value)
    else:  # pragma: no cover — registry is closed
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no verification available for this secret",
        )
    latency_ms = (time.monotonic() - started) * 1000

    if ok:
        row = (
            db.query(BusinessSecret)
            .filter(
                BusinessSecret.business_id == admin.business_id,
                BusinessSecret.key_name == key_name,
            )
            .first()
        )
        if row is not None:
            row.last_verified_at = datetime.now(timezone.utc)
    _audit(
        db, admin, "settings.secret_test", key_name,
        {"status": "ok" if ok else "error",
         "output": {"verified": ok, "detail": detail[:200]}},
        int(latency_ms),
    )
    db.commit()
    return SecretTestOut(ok=ok, latency_ms=round(latency_ms, 1), detail=detail)
