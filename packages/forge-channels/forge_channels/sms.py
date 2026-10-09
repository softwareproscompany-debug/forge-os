"""SMS channel providers: stub (dev outbox) and Twilio."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import httpx

from forge_channels._env import require_env
from forge_channels._stub import stub_context, stub_message_id
from forge_channels.outbox import DevOutbox

if TYPE_CHECKING:  # pragma: no cover - avoids a runtime circular import
    from forge_channels import SendRequest, SendResult


class StubSMSProvider:
    """Dev-mode SMS provider: records to ``dev_outbox`` instead of sending."""

    name = "stub"

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        db, business_id = stub_context(req)
        DevOutbox.record(
            db,
            business_id,
            channel="sms",
            to_address=req.to,
            subject=None,
            body=req.body,
            provider=self.name,
        )
        return SendResult(
            provider_message_id=stub_message_id("sms", req.to, None, req.body),
            status="sent",
            raw={"to": req.to, "via": "dev_outbox"},
        )

    async def parse_webhook(self, payload: dict) -> dict:
        return {
            "event": payload.get("event", "unknown"),
            "provider_message_id": payload.get("provider_message_id"),
            "contact": payload.get("contact"),
            "raw": dict(payload),
        }


class TwilioProvider:
    """Twilio Programmable SMS via the 2010-04-01 REST API (httpx + basic auth)."""

    name = "twilio"

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.account_sid = (
            account_sid if account_sid is not None else require_env("TWILIO_ACCOUNT_SID")
        )
        self.auth_token = (
            auth_token if auth_token is not None else require_env("TWILIO_AUTH_TOKEN")
        )
        self.from_number = (
            from_number
            if from_number is not None
            else require_env("TWILIO_FROM_NUMBER")
        )
        self._client = client

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        )
        client = self._client or httpx.AsyncClient(timeout=30.0)
        close_client = self._client is None
        try:
            response = await client.post(
                url,
                auth=httpx.BasicAuth(self.account_sid, self.auth_token),
                data={"To": req.to, "From": self.from_number, "Body": req.body},
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Twilio SMS send failed ({response.status_code}): {response.text[:500]}"
                ) from exc
            data = response.json()
        finally:
            if close_client:
                await client.aclose()
        message_id = data.get("sid") or f"twilio-{uuid.uuid4().hex[:16]}"
        return SendResult(
            provider_message_id=str(message_id),
            status=str(data.get("status", "sent")),
            raw={"to": req.to, "from": self.from_number},
        )

    async def parse_webhook(self, payload: dict) -> dict:
        # Twilio status-callback form params: MessageSid, MessageStatus, From.
        status = str(payload.get("MessageStatus", "")).lower()
        event = {
            "queued": "queued",
            "sending": "sending",
            "sent": "sent",
            "delivered": "delivered",
            "undelivered": "failed",
            "failed": "failed",
        }.get(status, status or "unknown")
        return {
            "event": event,
            "provider_message_id": payload.get("MessageSid"),
            "contact": payload.get("From"),
            "raw": dict(payload),
        }
