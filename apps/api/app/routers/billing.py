"""Stripe webhook ingestion: the order source of truth (Growth Engine P1).

Clients connect Stripe once (webhook secret stored in the encrypted
vault); order events flow in automatically. This endpoint verifies the
Stripe signature and stores raw events idempotently. It does NOT
compute commissions — that is P3's deterministic engine, which reads
from ``stripe_order_events``.

Stripe signature scheme: ``Stripe-Signature: t=<ts>,v1=<hex>[,v0=<hex>]``
where hex = HMAC-SHA256(webhook_secret, "<ts>.<raw_body>").
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from forge_db.models import StripeOrderEvent

from app import schemas
from app.core.deps import CurrentSettings, DbSession
from app.settings_vault import service as vault

router = APIRouter(prefix="/billing/stripe", tags=["billing"])

# Only these event types are ingested; anything else is acknowledged
# without storage so Stripe stops retrying it.
INGESTED_EVENT_TYPES = frozenset(
    {"checkout.session.completed", "payment_intent.succeeded"}
)


def _parse_stripe_signature(header_value: str) -> tuple[str, str] | None:
    """Extract (timestamp, v1) from a Stripe-Signature header."""
    timestamp: str | None = None
    v1: str | None = None
    for part in header_value.split(","):
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name == "t":
            timestamp = value
        elif name == "v1":
            v1 = value
    if not timestamp or not v1:
        return None
    return timestamp, v1


def verify_stripe_signature(raw_body: bytes, header_value: str, secret: str) -> bool:
    """Verify a Stripe webhook signature. Pure function (unit-testable)."""
    parsed = _parse_stripe_signature(header_value)
    if parsed is None or not secret:
        return False
    timestamp, v1 = parsed
    signed_payload = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(
        secret.encode(), signed_payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, v1)


@router.post(
    "/webhook/{business_id}",
    response_model=schemas.StripeOrderEventOut,
    status_code=201,
)
async def stripe_webhook(
    business_id: uuid.UUID,
    request: Request,
    db: DbSession,
    settings: CurrentSettings,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
):
    """Ingest a Stripe webhook event for ``business_id``.

    The business is identified by the path (each client registers their
    own webhook URL); the signing secret comes from that business's
    encrypted vault. No auth header — the HMAC signature IS the auth.
    """
    raw_body = await request.body()
    if not stripe_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe-Signature header",
        )
    secret = vault.get_secret(
        db, business_id, "stripe.webhook_secret", settings.DRAVEN_CONFIG_KEY
    )
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stripe is not connected for this business: add the "
            "webhook secret in Settings → Webhooks",
        )
    if not verify_stripe_signature(raw_body, stripe_signature, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Stripe signature",
        )

    try:
        import json as _json

        payload = _json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from None

    event_id = str(payload.get("id") or "")
    event_type = str(payload.get("type") or "")
    if not event_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stripe event has no id",
        )

    # Idempotent replay: a redelivered event returns the stored row.
    existing = (
        db.query(StripeOrderEvent)
        .filter(
            StripeOrderEvent.business_id == business_id,
            StripeOrderEvent.stripe_event_id == event_id,
        )
        .first()
    )
    if existing is not None:
        return existing

    if event_type not in INGESTED_EVENT_TYPES:
        # Acknowledge unhandled types without storing, so Stripe stops
        # retrying. Return 200 (not 201) to signal "not stored".
        raise HTTPException(
            status_code=status.HTTP_200_OK,
            detail=f"Ignored event type: {event_type}",
        )

    row = StripeOrderEvent(
        business_id=business_id,
        stripe_event_id=event_id,
        event_type=event_type,
        payload=payload,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent delivery of the same event:
        # re-read the winner instead of double-storing.
        db.rollback()
        existing = (
            db.query(StripeOrderEvent)
            .filter(
                StripeOrderEvent.business_id == business_id,
                StripeOrderEvent.stripe_event_id == event_id,
            )
            .first()
        )
        if existing is None:
            raise
        return existing
    db.refresh(row)
    return row
