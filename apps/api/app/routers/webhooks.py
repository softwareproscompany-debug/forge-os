"""Delivery webhook — no JWT auth; shared-secret header instead.

``X-Webhook-Secret`` must equal ``WEBHOOK_SECRET``. The send is located by
``provider_message_id`` (globally unique); the owning business is derived
from the send row itself.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, status

from forge_db.models import Send

from app import schemas
from app.core.deps import CurrentSettings, DbSession

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/delivery", response_model=schemas.DeliveryWebhookResponse)
def delivery_webhook(
    payload: schemas.DeliveryWebhookRequest,
    db: DbSession,
    settings: CurrentSettings,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
):
    if not x_webhook_secret or not hmac.compare_digest(
        x_webhook_secret, settings.WEBHOOK_SECRET
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )

    send = (
        db.query(Send)
        .filter(Send.provider_message_id == payload.provider_message_id)
        .first()
    )
    if send is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Send not found for provider_message_id",
        )

    now = datetime.now(timezone.utc)
    if payload.event == "delivered":
        send.status = "delivered"
        if send.sent_at is None:
            send.sent_at = now
    elif payload.event == "opened":
        send.opened_at = now
        if send.status not in ("delivered",):
            send.status = "delivered"
    elif payload.event == "clicked":
        send.clicked_at = now
        if send.opened_at is None:
            send.opened_at = now
    elif payload.event == "bounced":
        send.status = "bounced"
    db.commit()
    return schemas.DeliveryWebhookResponse(ok=True)
