"""Email channel providers: stub (dev outbox), SMTP, SendGrid, SESv2."""

from __future__ import annotations

import asyncio
import json
import smtplib
import uuid
from email.message import EmailMessage
from email.utils import make_msgid
from typing import TYPE_CHECKING, Any

import httpx

from forge_channels._env import get_env, require_env
from forge_channels._stub import stub_context, stub_message_id
from forge_channels.outbox import DevOutbox
from forge_channels.ses_auth import sign_sesv2_request

if TYPE_CHECKING:  # pragma: no cover - avoids a runtime circular import
    from forge_channels import SendRequest, SendResult


class StubEmailProvider:
    """Dev-mode email provider: records to ``dev_outbox`` instead of sending."""

    name = "stub"

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        db, business_id = stub_context(req)
        DevOutbox.record(
            db,
            business_id,
            channel="email",
            to_address=req.to,
            subject=req.subject,
            body=req.body,
            provider=self.name,
        )
        message_id = stub_message_id("email", req.to, req.subject, req.body)
        return SendResult(
            provider_message_id=message_id,
            status="sent",
            raw={"to": req.to, "subject": req.subject, "via": "dev_outbox"},
        )

    async def parse_webhook(self, payload: dict) -> dict:
        return {
            "event": payload.get("event", "unknown"),
            "provider_message_id": payload.get("provider_message_id"),
            "contact": payload.get("contact"),
            "raw": dict(payload),
        }


class SMTPProvider:
    """Send via any SMTP relay with STARTTLS (blocking smtplib in a thread)."""

    name = "smtp"

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        from_address: str | None = None,
    ) -> None:
        resolved_host = (host if host is not None else get_env("SMTP_HOST")).strip()
        if not resolved_host:
            raise ValueError(
                "SMTP_HOST is not set. Set SMTP_HOST (and SMTP_PORT/SMTP_USER/"
                "SMTP_PASSWORD/SMTP_FROM) to use EMAIL_PROVIDER=smtp."
            )
        self.host = resolved_host
        self.port = port if port is not None else int(get_env("SMTP_PORT", "587") or 587)
        self.username = username if username is not None else get_env("SMTP_USER")
        self.password = password if password is not None else get_env("SMTP_PASSWORD")
        self.from_address = (
            from_address
            if from_address is not None
            else get_env("SMTP_FROM", "noreply@example.com")
        )

    def _send_sync(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = req.to
        message["Subject"] = req.subject or ""
        message["Message-ID"] = make_msgid(domain=self.host)
        message.set_content(req.body)
        try:
            await asyncio.to_thread(self._send_sync, message)
        except (smtplib.SMTPException, OSError) as exc:
            raise RuntimeError(f"SMTP send via {self.host}:{self.port} failed: {exc}") from exc
        return SendResult(
            provider_message_id=str(message["Message-ID"]).strip("<>"),
            status="sent",
            raw={"to": req.to, "subject": req.subject},
        )

    async def parse_webhook(self, payload: dict) -> dict:
        # Plain SMTP has no delivery webhooks; accept already-normalized payloads.
        return {
            "event": payload.get("event", "unknown"),
            "provider_message_id": payload.get("provider_message_id"),
            "contact": payload.get("contact"),
            "raw": dict(payload),
        }


class SendGridProvider:
    """SendGrid v3 ``/mail/send`` API via httpx."""

    name = "sendgrid"
    _ENDPOINT = "https://api.sendgrid.com/v3/mail/send"

    def __init__(
        self,
        api_key: str | None = None,
        from_address: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else require_env("SENDGRID_API_KEY")
        self.from_address = (
            from_address
            if from_address is not None
            else get_env("SMTP_FROM", "noreply@example.com")
        )
        self._client = client

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        payload = {
            "personalizations": [{"to": [{"email": req.to}]}],
            "from": {"email": self.from_address},
            "subject": req.subject or "",
            "content": [{"type": "text/plain", "value": req.body}],
        }
        client = self._client or httpx.AsyncClient(timeout=30.0)
        close_client = self._client is None
        try:
            response = await client.post(
                self._ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"SendGrid send failed ({response.status_code}): {response.text[:500]}"
                ) from exc
        finally:
            if close_client:
                await client.aclose()
        # SendGrid answers 202 with the id in the X-Message-Id header.
        message_id = response.headers.get("x-message-id") or f"sendgrid-{uuid.uuid4().hex[:16]}"
        return SendResult(
            provider_message_id=message_id,
            status="sent",
            raw={"to": req.to, "subject": req.subject, "status_code": response.status_code},
        )

    async def parse_webhook(self, payload: dict) -> dict:
        # SendGrid event webhook: {"event": "delivered"|"open"|"click"|"bounce"|...,
        # "sg_message_id": ..., "email": ...}
        return {
            "event": payload.get("event", "unknown"),
            "provider_message_id": payload.get("sg_message_id"),
            "contact": payload.get("email"),
            "raw": dict(payload),
        }


class SESProvider:
    """AWS SESv2 ``SendEmail`` via the HTTPS API with hand-rolled SigV4."""

    name = "ses"

    def __init__(
        self,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        from_address: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.region = region if region is not None else require_env("SES_REGION")
        self.access_key = access_key if access_key is not None else require_env("SES_ACCESS_KEY")
        self.secret_key = secret_key if secret_key is not None else require_env("SES_SECRET_KEY")
        self.from_address = (
            from_address
            if from_address is not None
            else get_env("SMTP_FROM", "noreply@example.com")
        )
        self._client = client

    async def send(self, req: "SendRequest") -> "SendResult":
        from forge_channels import SendResult

        url = f"https://email.{self.region}.amazonaws.com/v2/email/outbound-emails"
        payload = {
            "FromEmailAddress": self.from_address,
            "Destination": {"ToAddresses": [req.to]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": req.subject or ""},
                    "Body": {"Text": {"Data": req.body}},
                }
            },
        }
        body = json.dumps(payload).encode("utf-8")
        headers = sign_sesv2_request(
            method="POST",
            url=url,
            headers={"Content-Type": "application/json"},
            payload=body,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
        )
        client = self._client or httpx.AsyncClient(timeout=30.0)
        close_client = self._client is None
        try:
            response = await client.post(url, content=body, headers=headers)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"SES SendEmail failed ({response.status_code}): {response.text[:500]}"
                ) from exc
            data = response.json()
        finally:
            if close_client:
                await client.aclose()
        message_id = data.get("MessageId") or f"ses-{uuid.uuid4().hex[:16]}"
        return SendResult(
            provider_message_id=str(message_id),
            status="sent",
            raw={"to": req.to, "subject": req.subject, "region": self.region},
        )

    async def parse_webhook(self, payload: dict) -> dict:
        # SES delivers SNS notifications: {"Type": "Notification", "Message": "<json>"}
        # with mail.messageId and ses eventType inside the inner message.
        event = "unknown"
        message_id = None
        try:
            inner = json.loads(payload.get("Message", "{}"))
            mail = inner.get("mail", {})
            message_id = mail.get("messageId")
            event_type = str(inner.get("eventType", "")).lower()
            event = {
                "send": "sent",
                "delivery": "delivered",
                "bounce": "bounced",
                "complaint": "complained",
                "reject": "failed",
            }.get(event_type, event_type or "unknown")
        except (ValueError, AttributeError):
            pass
        return {
            "event": event,
            "provider_message_id": message_id,
            "contact": None,
            "raw": dict(payload),
        }
