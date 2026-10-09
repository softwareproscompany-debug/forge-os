"""Settings vault service — encrypt/decrypt/upsert/status for tenant secrets.

Server-side only. Nothing in this module ever returns a plaintext secret
to a caller that isn't the server itself; ``secret_status`` exposes only
metadata plus a masked hint (``••••3f9a``).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.draven_crypto import decrypt_secret, encrypt_secret, get_fernet
from forge_db.models import (
    BusinessSecret,
    DravenProviderConfig,
    DravenToolRun,
    LeadSource,
)

# ---------------------------------------------------------------------------
# Secret registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretSpec:
    key: str  # key_name in business_secrets
    section: str  # voice | ai | market_intel | webhooks
    label: str
    kind: str  # api_key | url | login | password | hmac_secret
    help: str = ""


SECTIONS: list[dict[str, str]] = [
    {"id": "voice", "label": "Voice", "icon": "◉",
     "blurb": "Text-to-speech for Draven's spoken replies."},
    {"id": "ai", "label": "AI providers", "icon": "⬢",
     "blurb": "Chat LLM providers for Draven's brain."},
    {"id": "market_intel", "label": "Market Intel", "icon": "◈",
     "blurb": "Live market data connectors."},
    {"id": "webhooks", "label": "Webhooks", "icon": "⚡",
     "blurb": "HMAC signing secrets for inbound lead webhooks."},
]

KNOWN_SECRETS: tuple[SecretSpec, ...] = (
    SecretSpec("elevenlabs.api_key", "voice", "ElevenLabs API key", "api_key",
               "Powers Draven's voice (TTS). Paste from elevenlabs.io → Profile."),
    SecretSpec("anthropic.api_key", "ai", "Anthropic API key", "api_key",
               "Claude access for Draven chat. Starts with sk-ant-."),
    SecretSpec("gemini.api_key", "ai", "Gemini API key", "api_key",
               "Google AI Studio key for Gemini chat."),
    SecretSpec("openrouter.api_key", "ai", "OpenRouter API key", "api_key",
               "Access to 100+ models via OpenRouter. Starts with sk-or-."),
    SecretSpec("ollama.base_url", "ai", "Ollama base URL", "local_url",
               "Your Ollama server, e.g. http://localhost:11434."),
    SecretSpec("openai.api_key", "ai", "OpenAI-compatible API key", "api_key",
               "Key for any OpenAI-compatible chat endpoint."),
    SecretSpec("openai.base_url", "ai", "OpenAI-compatible base URL", "url",
               "HTTPS endpoint, e.g. https://api.example.com/v1."),
    SecretSpec("dataforseo.login", "market_intel", "DataForSEO login", "login",
               "Pay-as-you-go market data (dataforseo.com)."),
    SecretSpec("dataforseo.password", "market_intel", "DataForSEO password",
               "password", "API password for the DataForSEO account."),
)

_KNOWN_BY_KEY: dict[str, SecretSpec] = {s.key: s for s in KNOWN_SECRETS}

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_WEBHOOK_KEY_RE = re.compile(r"^webhook\.([a-z0-9_-]{1,64})\.secret$")
_WEBHOOK_SOURCE_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def validate_key_name(key_name: str) -> SecretSpec | None:
    """Return the spec for a known key, a synthesized webhook spec, or raise.

    Raises ``ValueError`` for unknown/malformed key names. The vault never
    stores arbitrary keys — the registry is the allowlist.
    """
    if not isinstance(key_name, str) or not _KEY_RE.match(key_name):
        raise ValueError(f"invalid key name: {key_name!r}")
    spec = _KNOWN_BY_KEY.get(key_name)
    if spec is not None:
        return spec
    m = _WEBHOOK_KEY_RE.match(key_name)
    if m:
        return SecretSpec(
            key_name, "webhooks", f"Webhook secret ({m.group(1)})",
            "hmac_secret",
            "HMAC-SHA256 signing secret for this lead source's webhooks.",
        )
    raise ValueError(f"unknown secret key: {key_name!r}")


def validate_value(spec: SecretSpec, value: str) -> None:
    """Format validation per key kind. Raises ``ValueError`` on rejection.

    This is a shape check, not a proof the key works — live verification
    is ``POST /settings/secrets/{key}/test``.
    """
    if not isinstance(value, str):
        raise ValueError("secret value must be a string")
    v = value.strip()
    if not v:
        raise ValueError("secret value must not be blank")
    if spec.kind == "url":
        if not v.startswith("https://"):
            raise ValueError("base URL must use https://")
        if len(v) > 512:
            raise ValueError("base URL is too long")
        return
    if spec.kind == "local_url":
        # Ollama-style endpoints: https anywhere, or plain http on loopback.
        if v.startswith("https://"):
            pass
        elif v.startswith("http://localhost") or v.startswith("http://127.0.0.1"):
            pass
        else:
            raise ValueError(
                "base URL must use https://, or http://localhost for a local server"
            )
        if len(v) > 512:
            raise ValueError("base URL is too long")
        return
    if any(ch.isspace() for ch in v):
        raise ValueError("secret value must not contain whitespace")
    min_len = {
        "api_key": 8,
        "login": 1,
        "password": 8,
        "hmac_secret": 16,
    }[spec.kind]
    if len(v) < min_len:
        raise ValueError(
            f"{spec.label} looks too short (minimum {min_len} characters)"
        )


def _resolve_config_key(config_key: str | None) -> str:
    if config_key:
        return config_key
    from app.core.config import get_settings

    return get_settings().DRAVEN_CONFIG_KEY


def _get_row(
    db: Session, business_id: uuid.UUID, key_name: str
) -> BusinessSecret | None:
    return (
        db.query(BusinessSecret)
        .filter(
            BusinessSecret.business_id == business_id,
            BusinessSecret.key_name == key_name,
        )
        .first()
    )


def _audit(
    db: Session,
    business_id: uuid.UUID,
    tool: str,
    key_name: str,
    actor: str,
    status: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit row. The secret value is NEVER included."""
    try:
        db.add(
            DravenToolRun(
                business_id=business_id,
                user_id=None,
                tool=tool,
                input={"key_name": key_name, "actor": actor},
                output_summary=str({
                    "key_name": key_name,
                    "status": status,
                    **(extra or {}),
                })[:2000],
                risk="medium",
                status=status,
                duration_ms=0,
            )
        )
        db.flush()
    except Exception:
        db.rollback()


# ---------------------------------------------------------------------------
# Lazy migration from legacy columns
# ---------------------------------------------------------------------------


def _copy_token(
    db: Session,
    business_id: uuid.UUID,
    key_name: str,
    token_enc: str | None,
    label: str,
    migrated: list[str],
) -> None:
    """Copy an already-Fernet-encrypted token into the vault.

    No decryption round-trip is needed: legacy columns hold Fernet tokens
    under the same master key, so the ciphertext moves as-is. The vault
    wins on conflict — a key already present is never overwritten.
    """
    if not token_enc:
        return
    if _get_row(db, business_id, key_name) is not None:
        return
    try:
        spec = validate_key_name(key_name)
    except ValueError:
        return
    db.add(
        BusinessSecret(
            business_id=business_id,
            key_name=key_name,
            value_enc=token_enc,
            label=label or spec.label,
        )
    )
    migrated.append(key_name)


def ensure_vault_migrated(
    db: Session, business_id: uuid.UUID, config_key: str | None = None
) -> list[str]:
    """Idempotently copy legacy encrypted secrets into the vault.

    Sources: ``draven_provider_config`` (provider api_key/base_url,
    tts_api_key) and ``lead_sources.secret_enc`` (webhook HMAC secrets).
    The old columns are left untouched — they become write-through
    deprecated and are dropped in a future, separately-reviewed migration.
    Returns the key names that were migrated by this call.
    """
    migrated: list[str] = []
    row = (
        db.query(DravenProviderConfig)
        .filter(DravenProviderConfig.business_id == business_id)
        .first()
    )
    if row is not None:
        if row.provider == "anthropic":
            _copy_token(
                db, business_id, "anthropic.api_key", row.api_key_enc,
                "Anthropic API key (migrated)", migrated,
            )
        elif row.provider == "openai_compatible":
            _copy_token(
                db, business_id, "openai.api_key", row.api_key_enc,
                "OpenAI-compatible API key (migrated)", migrated,
            )
            _copy_token(
                db, business_id, "openai.base_url", row.base_url_enc,
                "OpenAI-compatible base URL (migrated)", migrated,
            )
        if row.tts_provider == "elevenlabs":
            _copy_token(
                db, business_id, "elevenlabs.api_key", row.tts_api_key_enc,
                "ElevenLabs API key (migrated)", migrated,
            )
    for src in (
        db.query(LeadSource)
        .filter(LeadSource.business_id == business_id)
        .all()
    ):
        _copy_token(
            db, business_id, f"webhook.{src.source}.secret", src.secret_enc,
            f"Webhook secret for {src.source} (migrated)", migrated,
        )
    db.flush()
    return migrated


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def put_secret(
    db: Session,
    business_id: uuid.UUID,
    key_name: str,
    value: str,
    actor: str,
    config_key: str | None = None,
    label: str | None = None,
) -> BusinessSecret:
    """Encrypt + upsert a secret, then audit. Never logs the value."""
    spec = validate_key_name(key_name)
    validate_value(spec, value)
    fernet = get_fernet(_resolve_config_key(config_key))
    token = encrypt_secret(fernet, value.strip())
    assert token is not None
    row = _get_row(db, business_id, key_name)
    now = datetime.now(timezone.utc)
    if row is None:
        row = BusinessSecret(
            business_id=business_id,
            key_name=key_name,
            value_enc=token,
            label=label or spec.label,
        )
        db.add(row)
        action = "created"
    else:
        row.value_enc = token
        row.label = label or row.label or spec.label
        row.last_verified_at = None  # new value invalidates prior verification
        row.updated_at = now
        action = "updated"
    db.flush()
    _audit(db, business_id, "settings.secret_put", key_name, actor, "ok",
           {"action": action})
    db.flush()
    return row


def get_secret(
    db: Session,
    business_id: uuid.UUID,
    key_name: str,
    config_key: str | None = None,
) -> str | None:
    """Decrypt a secret. Server-side only — never expose the return value."""
    # validate_key_name raises for unknown keys even on read: the registry
    # is the allowlist in both directions.
    validate_key_name(key_name)
    row = _get_row(db, business_id, key_name)
    if row is None:
        # Lazy migration: legacy columns may hold it.
        ensure_vault_migrated(db, business_id, config_key)
        row = _get_row(db, business_id, key_name)
    if row is None:
        return None
    fernet = get_fernet(_resolve_config_key(config_key))
    return decrypt_secret(fernet, row.value_enc)


def delete_secret(
    db: Session,
    business_id: uuid.UUID,
    key_name: str,
    actor: str,
    config_key: str | None = None,
) -> bool:
    """Delete a secret. Returns True when a row was removed."""
    validate_key_name(key_name)
    row = _get_row(db, business_id, key_name)
    if row is None:
        ensure_vault_migrated(db, business_id, config_key)
        row = _get_row(db, business_id, key_name)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    _audit(db, business_id, "settings.secret_delete", key_name, actor, "ok")
    db.flush()
    return True


def masked_hint(
    db: Session,
    business_id: uuid.UUID,
    key_name: str,
    config_key: str | None = None,
) -> str | None:
    """Last-4-characters hint (``••••3f9a``) for display. None if unset."""
    value = get_secret(db, business_id, key_name, config_key)
    if not value:
        return None
    tail = value[-4:] if len(value) >= 4 else "••••"
    return f"••••{tail}"


def secret_status(
    db: Session,
    business_id: uuid.UUID,
    key_name: str,
    config_key: str | None = None,
) -> dict[str, Any]:
    """Metadata + masked hint for one key. NEVER the value."""
    spec = validate_key_name(key_name)
    row = _get_row(db, business_id, key_name)
    if row is None:
        ensure_vault_migrated(db, business_id, config_key)
        row = _get_row(db, business_id, key_name)
    if row is None:
        return {
            "key_name": key_name,
            "label": spec.label,
            "kind": spec.kind,
            "section": spec.section,
            "configured": False,
            "hint": None,
            "last_verified_at": None,
            "help": spec.help,
        }
    hint = None
    try:
        value = decrypt_secret(
            get_fernet(_resolve_config_key(config_key)), row.value_enc
        )
        if value:
            hint = f"••••{value[-4:]}" if len(value) >= 4 else "••••••••"
    except ValueError:
        hint = None  # undecryptable (rotated master key) — surfaced as error
    return {
        "key_name": key_name,
        "label": row.label or spec.label,
        "kind": spec.kind,
        "section": spec.section,
        "configured": hint is not None,
        "hint": hint,
        "last_verified_at": (
            row.last_verified_at.isoformat() if row.last_verified_at else None
        ),
        "help": spec.help,
    }


# ---------------------------------------------------------------------------
# Connector helpers
# ---------------------------------------------------------------------------


def vault_dataforseo_settings(
    db: Session, business_id: uuid.UUID, settings: Any
) -> Any:
    """Return a settings copy with vault DataForSEO creds layered over env.

    The vault wins; env vars remain the fallback. A copy (never the shared
    singleton) is returned so per-tenant credentials can't leak across
    requests. The connector interface is unchanged.
    """
    ck = getattr(settings, "DRAVEN_CONFIG_KEY", "") or None
    login = get_secret(db, business_id, "dataforseo.login", ck)
    password = get_secret(db, business_id, "dataforseo.password", ck)
    if not login and not password:
        return settings
    update: dict[str, Any] = {}
    if login:
        update["DATAFORSEO_LOGIN"] = login
    if password:
        update["DATAFORSEO_PASSWORD"] = password
    try:
        return settings.model_copy(update=update)
    except Exception:
        # Non-pydantic settings stand-in (tests): shallow attribute copy.
        import copy

        clone = copy.copy(settings)
        for k, v in update.items():
            setattr(clone, k, v)
        return clone
