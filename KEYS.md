# ForgeOS — External Credentials (KEYS.md)

Every third-party credential ForgeOS can consume: which env var, what breaks
without it, and where to obtain it.

> **Zero-key guarantee:** with every variable below empty/unset, the full loop
> (generate → approve → launch → outbox → analytics) runs on stubs. Nothing is
> sent anywhere; stub providers write to the `dev_outbox` table, inspectable at
> `GET /api/v1/dev/outbox` and the web `/outbox` page.

## LLM providers (`forge-llm`)

Selected by `LLM_PROVIDER` (`stub` default).

| Credential | Env var | Without it | Where to get it |
|---|---|---|---|
| Anthropic API key | `ANTHROPIC_API_KEY` | `LLM_PROVIDER=anthropic` fails at generation time; stub keeps working | [console.anthropic.com](https://console.anthropic.com) → API keys |
| Anthropic model | `ANTHROPIC_MODEL` (default `claude-sonnet-4-5-20250929`) | N/A — just a model name | Anthropic docs model list |
| OpenAI-compatible base URL | `OPENAI_COMPAT_BASE_URL` | `LLM_PROVIDER=openai_compatible` has nowhere to call | Your provider (e.g. `https://api.openai.com/v1`, Azure, vLLM, Ollama) |
| OpenAI-compatible key | `OPENAI_COMPAT_API_KEY` | auth fails at generation time | Same provider's dashboard |
| OpenAI-compatible model | `OPENAI_COMPAT_MODEL` | requests fail (empty model) | Same provider's model catalog |

## Channels (`forge-channels`)

Selected by `CHANNEL_MODE` (`stub` default). Set `CHANNEL_MODE=live` **and**
provide the keys below to actually send.

### Email — selected by `EMAIL_PROVIDER` (`smtp` default)

| Credential | Env var | Without it | Where to get it |
|---|---|---|---|
| SMTP host | `SMTP_HOST` | `smtp` provider can't connect | Your mail provider / Postfix / SES SMTP endpoint |
| SMTP port | `SMTP_PORT` (default `587`) | — | — |
| SMTP username | `SMTP_USER` | auth fails | Same |
| SMTP password | `SMTP_PASSWORD` | auth fails | Same |
| From address | `SMTP_FROM` (default `noreply@example.com`) | sends use the placeholder domain — set a real one | Your domain |
| SendGrid API key | `SENDGRID_API_KEY` | `EMAIL_PROVIDER=sendgrid` fails auth | [app.sendgrid.com](https://app.sendgrid.com) → Settings → API Keys |
| AWS SES region | `SES_REGION` | `EMAIL_PROVIDER=ses` can't sign requests | AWS console → SES |
| AWS SES access key | `SES_ACCESS_KEY` | auth fails | AWS IAM |
| AWS SES secret key | `SES_SECRET_KEY` | auth fails | AWS IAM |

Note: SES accounts start in **sandbox** (verified recipients only) — request
production access in the AWS console before real campaigns.

### SMS — `SMS_PROVIDER` (`twilio` default)

| Credential | Env var | Without it | Where to get it |
|---|---|---|---|
| Twilio Account SID | `TWILIO_ACCOUNT_SID` | live SMS fails auth | [console.twilio.com](https://console.twilio.com) |
| Twilio Auth Token | `TWILIO_AUTH_TOKEN` | live SMS fails auth | Same (keep secret — it *is* the password) |
| Twilio from number | `TWILIO_FROM_NUMBER` | Twilio rejects sends (no sender) | Twilio → Phone Numbers → buy/verify |

Trial accounts prepend a trial message and only reach verified numbers —
upgrade for production. All SMS sends honor `consent_sms` and STOP handling
per the consent gate in `send_message`.

### Social — per network via `get_social_provider(network)`

| Credential | Env var | Without it | Where to get it |
|---|---|---|---|
| Meta Page token | `SOCIAL_META_PAGE_TOKEN` | Meta/Facebook/Instagram posting fails | Meta developers → your app → Page access token (long-lived) |
| LinkedIn token | `SOCIAL_LINKEDIN_TOKEN` | LinkedIn posting fails | LinkedIn developer portal → OAuth 2.0 token for `w_member_social` |
| X API key | `SOCIAL_X_API_KEY` | X posting fails | [developer.x.com](https://developer.x.com) → project app keys |
| X API secret | `SOCIAL_X_API_SECRET` | X posting fails (OAuth signing) | Same |

## Platform secrets (not third-party, but treat like keys)

| Credential | Env var | Without it / rotation | Where to get it |
|---|---|---|---|
| JWT signing secret | `JWT_SECRET` | API auth doesn't work; default `dev-secret-change-me` is public — **rotate before any shared env** | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| Webhook shared secret | `WEBHOOK_SECRET` | `POST /api/v1/webhooks/delivery` rejects (expects `X-Webhook-Secret` header) | Same generation command |
| Postgres password | `POSTGRES_PASSWORD` / embedded in `DATABASE_URL` | DB unreachable | You set it (compose default `forge`) |

## Going live checklist

1. Set `LLM_PROVIDER` + key (or keep `stub` — stub generation is fine for demos,
   it just echoes template-based drafts).
2. Set `CHANNEL_MODE=live` and fill in the provider section you need.
3. Rotate `JWT_SECRET` and `WEBHOOK_SECRET` (see RUNBOOK.md).
4. Send one test campaign to yourself; confirm delivery, then check
   `analytics/overview` after exercising the webhook.
5. Set `daily_send_cap` / quiet hours in autopilot settings before any bulk run.
