# ForgeOS — Shared Contracts (v1)

All workstreams build against this file. If something here must change, update this file first.

## Repo layout

```
forge-os/
  CONTRACTS.md  ARCHITECTURE.md  README.md  RUNBOOK.md  KEYS.md  Makefile
  docker-compose.yml
  apps/
    api/      # FastAPI; owns packages/forge-db (SQLAlchemy models + session + Alembic)
    worker/   # arq worker; imports forge-db, forge-llm, forge-channels
    web/      # React + Vite + TS SPA
  packages/
    forge-db/        # models.py, session.py, alembic/  (owned by api workstream)
    forge-llm/       # forge_llm/ package
    forge-channels/  # forge_channels/ package
  infra/
    k8s/base  k8s/overlays/prod
    seed.py   # raw-SQL seed (psycopg2 only), idempotent
```

Python packages are installed with `pip install -e packages/forge-db -e packages/forge-llm -e packages/forge-channels`
inside api/worker images. Package import names: `forge_db`, `forge_llm`, `forge_channels`.

## Ports (docker compose service names)

| service | port |
|---|---|
| web (nginx) | 80 -> host 8080 |
| api | 8000 |
| worker | none |
| postgres | 5432 |
| redis | 6379 |

API base URL: `http://localhost:8080/api/v1` proxied? No — web calls api directly via `VITE_API_URL`
(default `http://localhost:8000`). In compose, nginx serves web and proxies `/api/` -> `api:8000`.

## Data model (table -> key columns)

All tables: `id UUID PK default gen_random_uuid()`, `created_at timestamptz default now()`.
Multi-tenant scoping column is `business_id UUID FK businesses.id` everywhere except
businesses/users/global. All queries filter by the JWT's business_id.

- `businesses(id, name, slug unique, timezone default 'UTC')`
- `users(id, email unique, password_hash, full_name, business_id FK, role in (owner,admin,member), is_active bool)`
- `brand_kits(id, business_id, name, voice_description text, tone_tags jsonb default [], primary_color, secondary_color, fonts jsonb default {}, icp_description text, do_list jsonb default [], dont_list jsonb default [], version int default 1)`
- `contacts(id, business_id, email nullable, phone nullable, first_name, last_name, source, tags jsonb default [], consent_email bool default false, consent_email_at nullable, consent_sms bool default false, consent_sms_at nullable, unsubscribed bool default false, custom_fields jsonb default {})`
- `asset_kinds`: email_copy | social_post | sms | blog | ad | image_prompt
- `asset_status`: draft | in_review | approved | rejected
- `assets(id, business_id, kind, title, body text, variables jsonb default {}, version int default 1, status, brand_kit_version int default 1, created_by FK users, approved_by nullable, rejection_reason nullable, parent_asset_id nullable FK assets, cost_usd numeric default 0, tokens_in int default 0, tokens_out int default 0, llm_provider, llm_model)`
- `asset_approvals(id, asset_id FK, reviewer_id FK users, decision in (approved,rejected), note, created_at)`
- `templates(id, business_id, name, channel in (email,sms,social), subject_template nullable text, body_template text (Jinja2), variables jsonb default [] (declared var names))`
- `campaign_status`: draft | scheduled | running | paused | completed
- `campaigns(id, business_id, name, description, status, autopilot bool default false, created_by FK users, starts_at nullable, timezone default 'UTC')`
- `campaign_steps(id, campaign_id FK, position int, channel in (email,sms,social), template_id FK templates nullable, asset_id FK assets nullable, delay_hours int default 24, trigger_event nullable text)`
- `campaign_enrollments(id, campaign_id FK, contact_id FK contacts, current_step int default 0, status in (active,paused,completed,unsubscribed), next_run_at nullable timestamptz)`
- `send_status`: queued | sending | sent | delivered | bounced | failed
- `sends(id, business_id, campaign_id nullable FK, step_id nullable FK campaign_steps, contact_id FK contacts, channel, asset_id nullable FK assets, to_address text, subject nullable, body text, status, provider_message_id nullable, error nullable, scheduled_for nullable, sent_at nullable, opened_at nullable, clicked_at nullable, converted_at nullable, meta jsonb default {})`
- `events(id, business_id, contact_id nullable FK, kind text, payload jsonb default {})`
  - kinds: `contact_added | email_opened | email_clicked | sms_replied | converted`
- `autopilot_settings(business_id PK FK, auto_approve bool default false, require_approval_for_channels jsonb default [], daily_send_cap int default 500, quiet_hours_start int default 22, quiet_hours_end int default 8)`
- `generation_logs(id, business_id, asset_id FK, provider, model, prompt_hash, tokens_in, tokens_out, cost_usd numeric, latency_ms int)`
- `dev_outbox(id, business_id, channel, to_address, subject nullable, body text, provider text default 'stub', created_at)` — written by stub channel providers only.
- `interview_status`: active | completed | abandoned
- `interview_sessions(id, business_id, user_id nullable FK users, status, current_index int default 0, answers jsonb default [] ([{question, answer, followup}]), draft_brand_kit jsonb nullable, brand_kit_id nullable FK brand_kits, completed_at nullable)` — Card 0 guided interview
- `weekly_summaries(id, business_id, week_start date (Monday), top_assets jsonb default [], bottom_assets jsonb default [], best_channel_per_segment jsonb default {}, recommendation text nullable)` — unique (business_id, week_start) — Card 5 weekly evidence summary
- `plan_status`: draft | approved | rejected
- `content_plans(id, business_id, week_start date (target Monday), status, items jsonb default [] ([{kind, channel, day (0=Monday), title, brief, asset_id?}]), created_by nullable FK users, approved_by nullable FK users, approved_at nullable, campaign_id nullable FK campaigns)` — Card 4 autopilot weekly plan

## Approval workflow

`draft -> in_review -> approved | rejected`. `rejected -> draft` allowed (rework).
Only `approved` assets may be sent. Autopilot (`autopilot_settings.auto_approve=true`)
lets the worker move a generated asset `draft -> approved` directly; otherwise the
generation job leaves it in `in_review` and a human approves via the approvals inbox.

## API contract (prefix `/api/v1`, JWT Bearer; tenant from token)

- `POST /auth/register` {email,password,full_name,business_name} -> {token,user}
- `POST /auth/login` (OAuth2 password form: username=email) -> {access_token, token_type}
- `GET /auth/me` -> user
- `GET/PUT /businesses/me`
- `GET/POST /brand-kits`, `GET/PUT /brand-kits/{id}`
- `GET/POST /contacts`, `GET/PUT/DELETE /contacts/{id}`, `POST /contacts/{id}/consent` {channel: email|sms, granted: bool}
- `GET/POST /templates`, `GET/PUT/DELETE /templates/{id}`, `POST /templates/{id}/preview` {variables} -> {subject, body}
- `POST /assets/generate` {kind, title, template_id?, prompt?, variables?} -> {asset_id, job_id} (enqueues worker job)
- `GET /assets?status=&kind=`, `GET /assets/{id}`, `POST /assets/{id}/submit`, `POST /assets/{id}/approve` {note?}, `POST /assets/{id}/reject` {reason}, `GET /assets/{id}/versions`
- `GET/POST /campaigns`, `GET/PUT /campaigns/{id}`, `POST /campaigns/{id}/steps` {steps:[...]}, `PUT /campaigns/{id}/steps/{step_id}`, `POST /campaigns/{id}/launch`, `POST /campaigns/{id}/pause`, `GET /campaigns/{id}/enrollments`
- `GET/PUT /autopilot`
- `GET /autopilot/plan` -> latest `content_plans` row for the caller's business by `week_start` desc ({week_start, status, items, created_at, campaign_id}); 404 when the planner has not drafted one yet
- `POST /autopilot/plan/approve` {plan_id} -> {plan, campaign_id}; 404 if not owned; 422 unless the plan is a draft. Creates a `scheduled` campaign (`autopilot=true`, starts_at = week_start Monday 09:00 business-local) with one step per item (position=i, delay_hours=day*24, asset_id re-validated as approved+owned else null); marks the plan approved with approved_by/at + campaign_id.
- `POST /interview/start` -> {session_id, status, question_index: 0, total_questions: 7, question, done: false} (201) — Card 0 guided interview
- `POST /interview/{id}/answer` {answer} -> next turn {session_id, status, question_index, total_questions, question, done}; `done: true, question: null` after the 7th main answer. 404 if not owned; 422 unless active with a pending question. On live LLM providers, one probing follow-up per answer may be generated (stub: strictly ordered questions).
- `POST /interview/{id}/finish` -> {session_id, status: "completed", draft_brand_kit} (422 unless active with all 7 main questions answered)
- `POST /interview/{id}/confirm` {overrides?} -> {brand_kit} — persists the draft as a `brand_kits` row (version 1), linked via `interview_sessions.brand_kit_id` (422 unless completed and not already confirmed)
- `GET /analytics/overview?days=30&campaign_id=` -> {sent, delivered, opened, clicked, converted, open_rate, ctr, conversion_rate, spend_usd, by_day:[...]}
- `GET /analytics/campaigns/{id}/funnel` -> per-step {step_id, position, sent, opened, clicked}
- `GET /analytics/weekly-summary` -> latest `weekly_summaries` row for the caller's business by `week_start` desc ({week_start, top_assets, bottom_assets, best_channel_per_segment, recommendation, created_at}); 404 {detail: "no weekly summary yet"} when the worker has not cut one yet
- `GET /ops/activity?limit=30` -> {as_of, stages: {foundation, origination, reach, growth, evidence} (each a {metric: count} map), counters: {sends_today, generations_today, in_flight}, activity: [{id, kind: generation|send|event, title, detail?, status?, at}]} — read-only mission-control aggregate; `as_of` is the data timestamp every panel shows
- `GET /dev/outbox?limit=50`
- `POST /webhooks/delivery` {provider_message_id, event: delivered|opened|clicked|bounced, contact?} — no auth (shared secret header `X-Webhook-Secret` = env WEBHOOK_SECRET)
- `POST /events` {kind, contact_id?, payload?} — enqueue event-trigger processing

Errors: JSON `{detail: ...}`, standard HTTP codes. Pagination: `?limit=&offset=` returning `{items, total}`.

## Worker jobs (arq, queue name `forge`)

- `generate_asset(ctx, asset_id: str)` — render prompt from template+brand kit -> `forge_llm.get_provider().generate()` -> guardrail check -> save body/tokens/cost -> status: `approved` if autopilot auto_approve else `in_review` -> log generation_logs.
- `send_message(ctx, send_id: str)` — load send+contact; consent check (email needs consent_email, sms needs consent_sms, unsubscribed blocks all); pick provider via `forge_channels`; retry with backoff on transient errors (arq retry); mark sent/failed; stub provider writes to dev_outbox.
- `campaign_tick(ctx)` — runs every 60s (arq cron): for running campaigns, enroll due contacts (starts_at passed, trigger contact_added), advance enrollments whose next_run_at <= now: create `sends` row (queued) + enqueue `send_message`; compute next_run_at from step delay_hours; respect quiet hours + daily_send_cap from autopilot_settings.
- `handle_event(ctx, event_id: str)` — `contact_added` -> enroll contact in campaigns whose step 0 has trigger_event='contact_added'; other events update send rows (opened/clicked) via provider_message_id lookup.
- `weekly_summary(ctx)` — Card 5. Hourly cron (self-gated): per business, convert now to the business timezone (stdlib `zoneinfo`, UTC fallback); if local Sunday 23:xx and no `weekly_summaries` row exists for (business_id, week_start=<last Monday>), aggregate the last 7 days `[now-7d, now)` into `top_assets`/`bottom_assets` (top/bottom 3 by conversion rate, delivered>=1, tiebreak rate then asset id), `best_channel_per_segment` (best channel per contact-tag segment, `untagged` included; tiebreak rate -> delivered desc -> channel name), and a `recommendation` paragraph (deterministic template on stub; one LLM call on live providers, stub fallback). Commits per business. Also feeds Origination: `generate_asset` appends the latest summary's recommendation to the prompt as "Last week's evidence".
- `autopilot_plan(ctx)` — Card 4. Hourly cron (self-gated): per business, if local Monday 06:xx and no draft/approved `content_plans` row exists for (business_id, week_start=<that Monday>), draft 3 items (Tue email_copy, Thu social_post, Sat sms) from the brand kit + latest weekly summary + approved-asset pool (email reuses the summary's top asset when kind matches, re-validated); stub is deterministic, live LLM may generate items JSON with stub fallback. Commits per business.

Cron note: `weekly_summary` and `autopilot_plan` are registered as `cron(job, hour={*range(24)}, minute={0})` — an hourly wake with per-business timezone gating inside the job, because a single daily cron cannot honor per-business timezones. `minute={0}` is load-bearing: arq treats an omitted `minute` as a wildcard (schedules every minute).

## forge_llm interface

```python
@dataclass GenerationRequest: prompt: str, system_prompt: str = "", max_tokens: int = 800,
    temperature: float = 0.7, variables: dict = {}
@dataclass GenerationResult: text: str, provider: str, model: str, tokens_in: int,
    tokens_out: int, cost_usd: float, latency_ms: int
class LLMProvider(Protocol):
    name: str
    async def generate(self, req: GenerationRequest) -> GenerationResult: ...
def get_provider() -> LLMProvider  # LLM_PROVIDER env: stub | anthropic | openai_compatible
# anthropic: ANTHROPIC_API_KEY, ANTHROPIC_MODEL (default claude-sonnet-4-5-20250929)
# openai_compatible: OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_API_KEY, OPENAI_COMPAT_MODEL
def build_brand_system_prompt(brand: dict) -> str   # voice, tone_tags, icp, do/don't lists
def check_guardrails(text: str, brand: dict) -> list[str]  # returns violation strings
class PromptRegistry:
    def register(self, name: str, template: str, defaults: dict | None = None): ...
    def render(self, name: str, variables: dict) -> str: ...
```

## forge_channels interface

```python
@dataclass SendRequest: channel: str, to: str, subject: str|None, body: str,
    metadata: dict = {}
@dataclass SendResult: provider_message_id: str, status: str, raw: dict = {}
class ChannelProvider(Protocol):
    name: str
    async def send(self, req: SendRequest) -> SendResult: ...
    async def parse_webhook(self, payload: dict) -> dict: ...  # normalized event
def get_email_provider() -> ChannelProvider   # CHANNEL_MODE=stub -> StubEmailProvider (dev_outbox)
def get_sms_provider() -> ChannelProvider     # EMAIL_PROVIDER=smtp|sendgrid|ses when live
def get_social_provider(network: str) -> ChannelProvider  # meta | linkedin | x
class DevOutbox:  # stub providers write via forge_db session passed in
    @staticmethod def record(db, business_id, channel, to_address, subject, body, provider="stub"): ...
```

## Environment variables (all; compose provides dev defaults)

```
DATABASE_URL=postgresql+psycopg2://forge:forge@postgres:5432/forge
REDIS_URL=redis://redis:6379/0
JWT_SECRET=dev-secret-change-me   JWT_EXPIRE_MINUTES=1440
WEBHOOK_SECRET=dev-webhook-secret
LLM_PROVIDER=stub  ANTHROPIC_API_KEY=  ANTHROPIC_MODEL=claude-sonnet-4-5-20250929
OPENAI_COMPAT_BASE_URL=  OPENAI_COMPAT_API_KEY=  OPENAI_COMPAT_MODEL=
CHANNEL_MODE=stub  EMAIL_PROVIDER=smtp
SMTP_HOST= SMTP_PORT=587 SMTP_USER= SMTP_PASSWORD= SMTP_FROM=noreply@example.com
SENDGRID_API_KEY=  SES_REGION=  SES_ACCESS_KEY=  SES_SECRET_KEY=
SMS_PROVIDER=twilio  TWILIO_ACCOUNT_SID=  TWILIO_AUTH_TOKEN=  TWILIO_FROM_NUMBER=
SOCIAL_META_PAGE_TOKEN=  SOCIAL_LINKEDIN_TOKEN=  SOCIAL_X_API_KEY=  SOCIAL_X_API_SECRET=
SEED_DEMO=true  VITE_API_URL=http://localhost:8000
```

## Dependency rules

Pure-Python-friendly: no pydantic-core-Rust surprises — but FastAPI needs pydantic v2
(linux wheels exist, fine in Docker). Prefer: `arq` (pure python) for queue, `PyJWT`
pure python, `passlib` with `pbkdf2_sha256` (pure python, NOT bcrypt), `Jinja2`,
`SQLAlchemy 2.0`, `psycopg2-binary`, `alembic`, `anthropic` SDK, `httpx`.
Pin versions in requirements.txt.

## Frontend routes

`/` dashboard · `/ops` mission control · `/onboarding` · `/campaigns` · `/campaigns/:id` · `/calendar`
· `/approvals` · `/assets` · `/analytics` · `/outbox`
Auth: JWT in localStorage, `Authorization: Bearer`. API client in `src/lib/api.ts`.
