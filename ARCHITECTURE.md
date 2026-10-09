# ForgeOS — Architecture

Authoritative contracts live in [`CONTRACTS.md`](CONTRACTS.md). This document
explains *why* the system is shaped the way it is and how the pieces interact.

## Services & responsibilities

| Service | Tech | Owns | Stateless? |
|---|---|---|---|
| `web` | React + Vite + TS, nginx | SPA; auth token in localStorage; calls `VITE_API_URL` directly; nginx proxies `/api/` → `api:8000` in compose | yes |
| `api` | FastAPI + SQLAlchemy 2.0 + Alembic | HTTP contract (`/api/v1`, JWT Bearer, tenant from token); owns `packages/forge-db` (models, session, migrations) | yes (migrate via entrypoint/initContainer) |
| `worker` | **arq** on asyncio, Redis queue `forge` | `generate_asset`, `send_message`, `campaign_tick` (cron 60s), `handle_event` | yes — scale horizontally freely |
| `postgres` | postgres:16 | system of record (all tables in CONTRACTS.md) | no (StatefulSet + PVC in k8s) |
| `redis` | redis:7 | arq job queue + result store; cache | no (AOF on; loss-tolerant — jobs are re-enqueueable) |

Packages (installed `-e` into api/worker images):

- **`forge-db`** — models, session factory, Alembic migrations. The *only* place
  that knows the schema. Seed (`infra/seed.py`) deliberately bypasses it
  (raw SQL + psycopg2) so fixtures load even when app code is broken.
- **`forge-llm`** — `LLMProvider` protocol + `get_provider()` factory keyed on
  `LLM_PROVIDER` (`stub | anthropic | openai_compatible`); brand-aware system
  prompt builder; guardrail checker returning violation strings; a
  `PromptRegistry` for named, versioned prompt templates.
- **`forge-channels`** — `ChannelProvider` protocol + factories
  (`get_email_provider`, `get_sms_provider`, `get_social_provider(network)`).
  Stub providers write to `dev_outbox` instead of the network.

## Data model summary

Multi-tenancy: every table except `businesses`/`users`/global carries
`business_id`; all queries filter by the JWT's `business_id`. Core entities:

- **Identity**: `businesses` → `users` (owner/admin/member) → `brand_kits`
  (voice, tone, colors, ICP, do/don't lists, versioned).
- **Audience**: `contacts` (email/phone, tags, per-channel consent + timestamps,
  `unsubscribed` kill-switch, `custom_fields` jsonb).
- **Content**: `assets` (kind, body, variables, `status` draft→in_review→
  approved|rejected, `brand_kit_version`, token/cost accounting, `parent_asset_id`
  lineage) + `asset_approvals` audit trail. Only `approved` assets may be sent.
- **Orchestration**: `templates` (Jinja2, per channel) → `campaigns`
  (draft|scheduled|running|paused|completed) → `campaign_steps`
  (position, channel, template/asset, `delay_hours`, `trigger_event`) →
  `campaign_enrollments` (per-contact progress, `next_run_at`).
- **Delivery**: `sends` (queued→sending→sent→delivered|bounced|failed, open/click/
  conversion timestamps, `provider_message_id`, `meta` jsonb) → `events`
  (`contact_added | email_opened | email_clicked | sms_replied | converted`).
- **Control plane**: `autopilot_settings` (per business: `auto_approve`,
  `require_approval_for_channels`, `daily_send_cap`, quiet hours,
  planner schedule `plan_day`/`plan_hour`/`plan_cadence` + `last_planned_at`),
  `generation_logs` (provider/model/tokens/cost/latency per generation),
  `dev_outbox` (stub provider writes — the "sent mail" of zero-key mode).

## Request / event flows

### Generation pipeline (`POST /api/v1/assets/generate`)

```
client → api: validate JWT + business scope, create asset row (status=draft)
       → enqueue generate_asset(asset_id) on arq queue `forge` → 202 {asset_id, job_id}
worker: generate_asset
       → load asset + template + brand_kit
       → PromptRegistry.render + build_brand_system_prompt(brand)
       → forge_llm.get_provider().generate()          (stub unless LLM_PROVIDER set)
       → check_guardrails(text, brand) → violations[]  (block or flag)
       → save body/tokens/cost → generation_logs row
       → status = approved   if autopilot.auto_approve
                = in_review  otherwise                  (human approves via inbox)
```

### Send pipeline (`send_message` with arq retries)

```
campaign_tick / event trigger → sends row (status=queued) → enqueue send_message(send_id)
worker: send_message
       → load send + contact
       → CONSENT GATE: email requires consent_email, sms requires consent_sms,
         unsubscribed blocks everything → status=failed + error if violated
       → forge_channels provider.send()                (stub → dev_outbox row)
       → sent: provider_message_id, sent_at | failed: error
       → transient provider errors raise → arq retries with backoff
webhook → POST /api/v1/webhooks/delivery (X-Webhook-Secret)
       → match provider_message_id → update send (delivered/opened/clicked/bounced)
         + append events row
```

### Campaign scheduling (`campaign_tick`, arq cron every 60s)

```
for each running campaign:
  enroll: contacts added since last tick (or trigger_event='contact_added'
          on step 0 via handle_event) → campaign_enrollments row
  advance: enrollments with next_run_at <= now and status=active
           → render step template with contact variables
           → sends row (queued) + enqueue send_message
           → next_run_at = now + delay_hours(next step)
  respect: quiet hours + daily_send_cap from autopilot_settings
           (sends deferred past quiet hours; cap stops new sends for the day)
```

### Event triggers (`POST /api/v1/events` → `handle_event`)

`contact_added` enrolls the contact in every running campaign whose step 0 has
`trigger_event='contact_added'`. Engagement events (`email_opened`, …) are
written by the webhook path and feed `analytics/overview` + per-step funnels.

## API contract overview

Prefix `/api/v1`, JWT Bearer, tenant from token. Resource groups
(auth, businesses, brand-kits, contacts + consent, templates + preview,
assets + generate/submit/approve/reject/versions, campaigns + steps/launch/pause/
enrollments, autopilot, analytics overview + funnel, dev/outbox, webhooks,
events). Errors are JSON `{detail}` with standard codes; list endpoints paginate
via `?limit=&offset=` → `{items, total}`. Full route table: CONTRACTS.md.

## Queue choice: why arq

The worker uses **arq** (asyncio-native, pure-Python, Redis-backed) rather than
Celery, for reasons that match this workload:

1. **asyncio-native** — providers are `httpx`-based async clients (Anthropic,
   Twilio, SendGrid); arq runs coroutines directly, no thread-pool bridging.
2. **Pure Python, zero broker extras** — no compiled deps, no separate broker
   process to operate; Redis (already required) is the only infra.
3. **Built-in cron + retries** — `campaign_tick` every 60s is a one-line cron
   config; transient send failures get exponential-backoff retries for free,
   which is exactly the `send_message` failure mode.
4. **Lighter ops surface** — no Celery beat/flower/worker topology to deploy,
   monitor, and version-skew; one Deployment scales with an HPA.

Celery's strengths (multi-broker, canvas workflows, huge ecosystem) don't pay
for a workload that is fundamentally "N independent async jobs + one cron tick."

## Scaling notes

- **api / web / worker** are stateless → `replicas` + HPA (see
  `infra/k8s/overlays/prod`). JWT auth means no session affinity.
- **Hot path** is `send_message` throughput: scale `worker` replicas; arq
  concurrency per worker is also tunable. `daily_send_cap` + provider rate
  limits bound real-world send rates before CPU does.
- **postgres** is the scaling ceiling: single StatefulSet today. Next steps if
  needed: read replica for analytics queries, or partition `sends`/`events` by
  month (both are append-mostly, time-ordered).
- **redis** holds transient queue state only; a failover loses at most
  in-flight jobs, which `campaign_tick`/`handle_event` re-derive from the DB.
- Migrations run in an initContainer (k8s) / entrypoint (compose) — exactly one
  writer at deploy time; rollouts of api/worker can overlap because Alembic
  migrations must stay backward-compatible (expand-then-contract).

## Extension points (for Michael's architects)

- **New LLM provider**: implement `LLMProvider`, register in `forge_llm`
  factory, add `LLM_PROVIDER=<name>` + env keys; document in KEYS.md.
- **New channel**: implement `ChannelProvider` (+ `parse_webhook`), wire into
  the `get_*_provider` factories; stub variant writes to `dev_outbox`.
- **New asset kind**: extend the `asset_kinds` enum + generation prompt
  template in `PromptRegistry`; the approval/send pipeline is kind-agnostic.
- **New campaign trigger**: add `trigger_event` values on step 0 and handle
  them in `handle_event`; enrollment stays centralized.
- **Guardrails**: `check_guardrails` returns violation strings — add brand or
  compliance rules there; the worker already blocks/fails closed on violations.
- **Analytics**: `analytics/overview` aggregates `sends` + `events`; new
  engagement kinds flow through `events.kind` without schema changes.
