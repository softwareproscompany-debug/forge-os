# Contributing to ForgeOS

Thanks for contributing. This is a monorepo with strict shared contracts —
read [`CONTRACTS.md`](CONTRACTS.md) before changing anything cross-cutting
(ports, env vars, API routes, DB models, queue names, package interfaces).

## Dev setup

```bash
# Full stack
docker compose up --build
make seed   # demo tenant: demo@forgeos.local / demo1234

# Backend only (fast iteration)
python3 -m venv .venv && source .venv/bin/activate
pip install -e packages/forge-db -e packages/forge-llm -e packages/forge-channels
pip install -r apps/api/requirements.txt
cd apps/api && uvicorn app.main:app --reload   # needs postgres + redis running

# Frontend only
cd apps/web && npm install && npm run dev
```

## Layout & ownership

| Path | Owner notes |
|---|---|
| `apps/api/` | FastAPI, `:8000`. Routers per domain in `app/routers/`. |
| `apps/worker/` | arq worker, queue name `forge`. Jobs in `worker/jobs.py`. |
| `apps/web/` | React + Vite + TS. Pages in `src/pages/`, API client in `src/lib/api.ts`. |
| `packages/forge-db/` | SQLAlchemy models + Alembic. **All schema changes need a migration.** |
| `packages/forge-llm/` | LLM provider abstraction. New provider = new module + registry entry. |
| `packages/forge-channels/` | Channel abstraction. New channel = new module + stub + tests. |
| `infra/k8s/` | Kustomize base + prod overlay. |

## Conventions

- **Python**: type-annotate public functions, keep deps pure-Python-friendly
  (no Rust-extension surprises), pytest for everything with logic.
- **DB**: never edit a landed migration — write a new one
  (`alembic revision --autogenerate` from `packages/forge-db`).
- **API**: versioned under `/api/v1`; new routes go in `CONTRACTS.md` first.
- **Frontend**: `npm run build` must stay clean; no new top-level deps without
  noting why in the PR.
- **Commits**: short imperative subject (`Add SMS retry backoff`), body explains
  the why when non-obvious.

## Adding an LLM provider

1. Implement the provider interface in `packages/forge-llm/forge_llm/` (see
   `providers.py`).
2. Register it in the provider registry + document env vars in
   `.env.example` and `KEYS.md`.
3. Add unit tests with mocked HTTP in `packages/forge-llm/tests/`.

## Adding a channel

1. Implement the channel interface in `packages/forge-channels/`
   (see `email.py` for the pattern), including a stub variant.
2. Wire `CHANNEL_MODE=live` config, document credentials in `KEYS.md`.
3. Add tests in `packages/forge-channels/tests/` — stub path must work
   with zero keys.

## Pull requests

- `make test` green, `docker compose config` valid, `vite build` clean.
- Update `CONTRACTS.md` if you touched shared surface.
- Small, focused PRs beat big ones.
