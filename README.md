# ForgeOS

**A business operating system for AI-driven marketing automation.** One tenant =
one business. The loop: **generate** marketing copy with an LLM (brand-kit
aware, guardrailed, human-approved) → **launch** it as a multi-step campaign →
**measure** opens, clicks, and conversions → repeat. Every step runs on stubs
with zero external keys, so the whole loop works on a laptop in five minutes.

> Contract source of truth: [`CONTRACTS.md`](CONTRACTS.md) — repo layout, ports,
> env vars, data model, API routes, worker jobs, and package interfaces. Read it
> before changing anything shared.

## 5-minute quickstart

```bash
# 1. Start everything (postgres, redis, api, worker, web)
docker compose up --build

# 2. In another terminal: load the demo tenant (idempotent — safe to re-run)
make seed
#    business "Acme Demo Co", owner login below, brand kit, 12 contacts,
#    3 templates, 3 approved assets, 1 running campaign, 5 seeded sends.

# 3. Open the app
open http://localhost:8080          # web UI (nginx proxies /api/ -> api:8000)
```

Demo login: `demo@forgeos.local` / `demo1234`

**Try the full loop** (all on stubs — nothing leaves your machine):

1. **Generate** — `POST /api/v1/assets/generate` `{kind: "email_copy", title: "Spring offer"}`
   (or use the web UI `/assets`). The worker renders the prompt from your brand
   kit, calls the LLM provider (`stub` echoes a template-based draft), runs
   guardrails, and parks the asset in `in_review` — because `auto_approve=false`.
2. **Approve** — open `/approvals`, review, hit Approve (or
   `POST /api/v1/assets/{id}/approve`). Only `approved` assets may be sent.
3. **Launch** — create a campaign (`POST /api/v1/campaigns`), add steps
   (`POST /api/v1/campaigns/{id}/steps`), then `POST /api/v1/campaigns/{id}/launch`.
   The worker's `campaign_tick` (every 60s) creates queued sends and delivers
   them through the stub providers.
4. **Measure** — stub sends land in the **dev outbox** (`GET /api/v1/dev/outbox`,
   web `/outbox`). Fake engagement with `POST /api/v1/events`
   `{kind: "email_opened", ...}` or the webhook endpoint, then check
   `GET /api/v1/analytics/overview`.

## Repo map

```
forge-os/
  CONTRACTS.md            # shared contracts — read first
  docker-compose.yml      # local stack: postgres, redis, api, worker, web
  docker-entrypoint.sh    # api entrypoint: alembic upgrade head, then uvicorn
  .env.example            # every env var, documented (zero keys required)
  Makefile                # up / down / logs / seed / migrate / test / build-web
  apps/
    api/                  # FastAPI on :8000 (api workstream)
    worker/               # arq worker, queue `forge` (worker workstream)
    web/                  # React+Vite+TS SPA behind nginx :80 (web workstream)
  packages/
    forge-db/             # SQLAlchemy models + session + Alembic (api workstream)
    forge-llm/            # LLM providers: stub | anthropic | openai_compatible
    forge-channels/       # email/sms/social providers + stub dev-outbox
  infra/
    seed.py               # idempotent raw-SQL demo seed (psycopg2 only)
    k8s/base/             # namespace, configmap, postgres, redis, api, worker,
                          # web, ingress + kustomization
    k8s/overlays/prod/    # replicas, resources, HPAs, PDB, prod image tags, TLS
  ARCHITECTURE.md  RUNBOOK.md  KEYS.md
```

## Configuration

Everything is env-driven; see [`.env.example`](.env.example) and
[`KEYS.md`](KEYS.md). The important knobs:

| Variable | Default | Effect |
|---|---|---|
| `LLM_PROVIDER` | `stub` | `anthropic` / `openai_compatible` for real generation |
| `CHANNEL_MODE` | `stub` | `live` to actually send email/SMS/social |
| `DATABASE_URL` | compose postgres | SQLAlchemy + psycopg2 URL |
| `REDIS_URL` | compose redis | arq queue + cache |
| `JWT_SECRET` | `dev-secret-change-me` | sign/verify API tokens — rotate in prod |
| `VITE_API_URL` | `http://localhost:8000` | baked into the web image at build time |

## Test commands

```bash
make test        # pytest across packages/forge-db, forge-llm, forge-channels, apps/api
make build-web   # build the nginx web image only
docker compose config   # validate the compose file (no daemon needed)
```

Workstream-local tests live next to their code; `make test` is the aggregate
gate. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the system design,
[`RUNBOOK.md`](RUNBOOK.md) for operations, [`KEYS.md`](KEYS.md) for credentials.
