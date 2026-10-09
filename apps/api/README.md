# ForgeOS API

FastAPI service implementing the REST contract in `../../CONTRACTS.md`
(all routes under `/api/v1`, JWT Bearer auth, tenant isolated by
`business_id` from the token).

## Layout

```
apps/api/
  app/
    main.py            # FastAPI app factory, CORS, /healthz, router mounting
    schemas.py         # pydantic v2 request/response schemas
    core/
      config.py        # pydantic-settings: every env var from CONTRACTS.md
      security.py       # JWT (PyJWT) + pbkdf2_sha256 password hashing (passlib)
      deps.py          # get_db, get_current_user, require_role, tenant scoping
      queue.py         # best-effort arq enqueue (never 500s on Redis outage)
    routers/           # one module per resource (auth, businesses, brand_kits,
                       # contacts, templates, assets, campaigns, autopilot,
                       # analytics, outbox, webhooks, events)
  tests/test_api.py    # TestClient + SQLite smoke tests
  requirements.txt     # pinned deps
  Dockerfile
```

`packages/forge-db` (owned by this workstream, imported by the worker)
holds the SQLAlchemy 2.0 models, session helpers, and the Alembic tree.

## Local dev

```bash
cd apps/api
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install -e ../../packages/forge-db

export DATABASE_URL="postgresql+psycopg2://forge:forge@localhost:5432/forge"
export REDIS_URL="redis://localhost:6379/0"
export JWT_SECRET="change-me" WEBHOOK_SECRET="change-me"

# migrate (from packages/forge-db)
(cd ../../packages/forge-db && alembic -c alembic/alembic.ini upgrade head)

uvicorn app.main:app --reload --port 8000
```

## Tests

```bash
pytest tests/ -q
```

Tests run against SQLite (models are dialect-portable: native `UUID`/`JSONB`
on Postgres, `CHAR(32)`/`JSON` on SQLite; the `gen_random_uuid()` server
default lives in the Alembic migration, which is postgres-only).

## Key behaviors

* **Approval state machine** (`assets`): `draft → in_review → approved|rejected`,
  plus `rejected → draft` rework via `POST /assets/{id}/submit`. Anything else
  is a 422. `POST /campaigns/{id}/launch` rejects (422) unless every
  asset-bearing step references an **approved** asset.
* **Queue resilience**: `POST /assets/generate` and `POST /events` persist
  their rows first; if Redis is unreachable they return `job_id: null` plus a
  `warning` field instead of 500ing.
* **Webhooks**: `POST /webhooks/delivery` is unauthenticated but requires
  header `X-Webhook-Secret == WEBHOOK_SECRET` (constant-time compare).
* **Analytics** (`GET /analytics/overview`): rates are divided by `delivered`
  (0.0 when nothing delivered); `spend_usd` = sum of
  `generation_logs.cost_usd` in the window (LLM generation spend).
* **Tenant isolation**: every query filters by the JWT's `business_id`;
  cross-tenant object access returns 404 (not 403) to avoid existence leaks.
* **Template preview** renders Jinja2 with `StrictUndefined`: a missing
  variable is a 422, not silent empty output.

## Deviations from CONTRACTS.md

1. `contacts.first_name`/`last_name` are nullable in the DB (contract doesn't
   mark them nullable). Rationale: phone-/email-only leads from the
   `contact_added` event must be storable.
2. `POST /assets/{id}/submit` also performs the `rejected → draft` rework
   transition (no separate endpoint exists for it in the contract).
3. `POST /assets/generate` and `POST /events` responses add an optional
   `warning: string | null` field (required by the API workstream spec for
   the Redis-down path; `job_id` is `null` in that case).
4. `GET /assets/{id}/versions` returns the lineage as `{items, total}` for
   consistency with the contract's pagination envelope.
5. Settings adds `CORS_ORIGINS` (default `*`) and `API_PREFIX` (default
   `/api/v1`) beyond the contract's env list.
