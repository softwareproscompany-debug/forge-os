# forge-channels

Multi-channel send abstraction for ForgeOS: email, SMS, and social providers
behind one `ChannelProvider` protocol.

## Install

```bash
pip install -e packages/forge-channels
pip install -e "packages/forge-channels[dev]"   # tests
```

## Quick start

```python
import asyncio
from forge_channels import SendRequest, get_email_provider

async def main():
    provider = get_email_provider()  # stub in dev, live per env (see below)
    result = await provider.send(SendRequest(
        channel="email",
        to="user@example.com",
        subject="Hello",
        body="Welcome aboard.",
        metadata={"db": session, "business_id": business_id},  # stub providers only
    ))
    print(result.provider_message_id, result.status)

asyncio.run(main())
```

`SendRequest`: `channel`, `to`, `subject` (`None` for SMS/social), `body`,
`metadata={}`. `SendResult`: `provider_message_id`, `status`, `raw={}`.

## Provider selection

| Factory | `CHANNEL_MODE=stub` (default) | live (`CHANNEL_MODE=live`) |
|---|---|---|
| `get_email_provider()` | `StubEmailProvider` → dev outbox | `EMAIL_PROVIDER`: `smtp` (default) · `sendgrid` · `ses` |
| `get_sms_provider()` | `StubSMSProvider` → dev outbox | `SMS_PROVIDER`: `twilio` (default) |
| `get_social_provider(network)` | `StubSocialProvider` → dev outbox | `meta` · `linkedin` · `x` |

Stub providers write to the `dev_outbox` table via `DevOutbox.record`, which
imports `forge_db` lazily. They need `metadata["db"]` (a **sync** session) and
`metadata["business_id"]`, and return deterministic `stub-<hash>` message ids.

### Live provider env vars

- **SMTP**: `SMTP_HOST` (required), `SMTP_PORT` (default 587), `SMTP_USER`,
  `SMTP_PASSWORD`, `SMTP_FROM` (default `noreply@example.com`) — STARTTLS via `smtplib`.
- **SendGrid**: `SENDGRID_API_KEY` (required); from-address reuses `SMTP_FROM`.
- **SES**: `SES_REGION`, `SES_ACCESS_KEY`, `SES_SECRET_KEY` (all required) —
  SESv2 HTTPS API with pure-Python SigV4 signing (`forge_channels.ses_auth`).
- **Twilio**: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`.
- **Meta**: `SOCIAL_META_PAGE_TOKEN` (required); page id from `SOCIAL_META_PAGE_ID`
  or `metadata["page_id"]`.
- **LinkedIn**: `SOCIAL_LINKEDIN_TOKEN` (required); author URN from
  `SOCIAL_LINKEDIN_AUTHOR_URN` or `metadata["author_urn"]`.
- **X**: `SOCIAL_X_API_KEY`, `SOCIAL_X_API_SECRET`, `SOCIAL_X_ACCESS_TOKEN`,
  `SOCIAL_X_ACCESS_TOKEN_SECRET` (all required) — OAuth 1.0a user context.

Every live provider raises `ValueError` naming the missing variable when its
credentials are absent, and wraps HTTP failures in `RuntimeError` with the
status code and a body excerpt. Each implements `parse_webhook(payload)` to
normalize provider webhooks into `{event, provider_message_id, contact, raw}`.

For tests, live providers accept an optional `httpx.AsyncClient` (e.g. with a
`MockTransport`) — no network calls in the test suite.

## Tests

```bash
cd packages/forge-channels && python -m pytest
```

Outbox tests use the real `forge_db` on sqlite in-memory when that package is
available, and otherwise fall back to a protocol-level fake `forge_db` module —
they never fail just because the sibling `forge-db` workstream isn't built yet.
