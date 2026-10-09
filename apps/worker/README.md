# ForgeOS Worker

arq-based background worker for ForgeOS. Owns four jobs (per `CONTRACTS.md`):

| Job | Trigger | What it does |
|---|---|---|
| `generate_asset(ctx, asset_id)` | API `POST /assets/generate` | Renders a prompt from the asset kind + latest brand kit, calls `forge_llm.get_provider().generate()`, runs `check_guardrails`, saves body/tokens/cost, moves the asset to `in_review` — or `approved` when the business has autopilot auto-approve on. |
| `send_message(ctx, send_id)` | `campaign_tick`, API | Unsubscribe/consent gating, Jinja2 `StrictUndefined` content rendering (approved asset or step template), provider dispatch via `forge_channels`, sent/failed bookkeeping, arq `Retry` with backoff on transient provider errors. |
| `campaign_tick(ctx)` | cron, every 60s | Bulk-enrolls due contacts into running campaigns and advances due enrollments into queued `send_message` jobs, honoring quiet hours and the per-business daily send cap. |
| `handle_event(ctx, event_id)` | API `POST /events` | `contact_added` → enrolls the contact into running campaigns whose step 0 has `trigger_event='contact_added'`; `email_opened`/`email_clicked`/`converted` → stamps the matching send via `provider_message_id`. |

## Layout

```
apps/worker/
  worker/
    __init__.py    # re-exports WorkerSettings + jobs
    settings.py    # arq WorkerSettings (import path: worker.settings.WorkerSettings)
    jobs.py        # the four job functions (typed)
    timeutil.py    # pure quiet-hours / scheduling helpers
  tests/
    test_worker.py # unit + sqlite-backed job tests (no network)
  requirements.txt # pinned deps (+ editable local workspace packages)
  pytest.ini       # asyncio_mode=auto
  Dockerfile
  .dockerignore
```

## Running locally

```bash
cd apps/worker
pip install -r requirements.txt   # installs forge-db/forge-llm/forge-channels editable
export DATABASE_URL=postgresql+psycopg2://forge:forge@localhost:5432/forge
export REDIS_URL=redis://localhost:6379/0
# LLM_PROVIDER=stub and CHANNEL_MODE=stub are the zero-credential defaults.
arq worker.settings.WorkerSettings
```

The settings module is overridable: `ARQ_WORKER_SETTINGS` (compose) /
`ARQ_QUEUE_NAME` (default `forge`, per `CONTRACTS.md`).

## Tests

```bash
cd apps/worker
pytest -q
```

Tests invoke the arq job functions directly with a fake `ctx` dict against a
fresh SQLite database per test. Providers are the stubs (`LLM_PROVIDER=stub`,
`CHANNEL_MODE=stub`), so no network or credentials are needed.

## Docker

```bash
# from the repo root (forge-os/) — the build context must be the repo root
# so COPY packages/ resolves:
docker build -f apps/worker/Dockerfile -t forgeos-worker .
docker run --env-file .env forgeos-worker
```

The image runs `arq worker.settings.WorkerSettings` from `/srv/worker`.

## Failure behavior

* Missing/unreachable Postgres or Redis is logged as an error with the
  offending URL/cause — jobs fail loudly, never silently pass.
* `send_message` retries transient provider errors (`httpx` timeouts,
  connection errors, HTTP 429/5xx) with exponential backoff (60s → 8min),
  up to 5 tries; consent/unsubscribe/content errors fail immediately.
* `campaign_tick` commits per campaign, so one bad campaign cannot poison
  the whole tick.

## Notes / known contract gaps

* The asset transition table has no direct `draft -> approved` edge, so
  autopilot approval walks `draft -> in_review -> approved` inside the
  `generate_asset` job (every step asserted via `assert_asset_transition`).
* `generation_logs` has no metadata column in the v1 data model, so
  guardrail violations are logged and returned in the job result rather
  than stored on the log row.
* `docker-compose.yml` sets the worker build context to `./apps/worker`,
  under which this Dockerfile's `COPY packages/` cannot resolve (same is
  true of `apps/api/Dockerfile`). The compose context needs to be the repo
  root — flagged for the infra workstream.
