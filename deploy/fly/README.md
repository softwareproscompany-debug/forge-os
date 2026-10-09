# Deploying ForgeOS to Fly.io

Three apps + managed Postgres + Upstash Redis. Region `dfw` (Dallas) is the default — closest to Texas; change `primary_region` in the `.toml` files if you prefer elsewhere.

## 0. Prerequisites

- A [Fly.io](https://fly.io) account (the free tier asks for a credit card on file)
- `flyctl` installed, then `fly auth login`

All commands run from the **repo root**.

## 1. Postgres

```bash
fly postgres create --name forgeos-db --region dfw --vm-size shared-cpu-1x --volume-size 10
```

## 2. Create the apps (names must be globally unique — adjust if taken)

```bash
fly apps create forgeos-api
fly apps create forgeos-worker
fly apps create forgeos-web
```

## 3. Redis (Upstash)

```bash
fly redis create --name forgeos-redis --region dfw
```

That prints a `REDIS_URL`. (Alternatively `fly redis attach` only works for apps; we set the secret manually so API and worker share it.)

## 4. Attach Postgres + set secrets

```bash
# DATABASE_URL is set automatically by attach:
fly postgres attach --app forgeos-api forgeos-db

# Shared secrets (generate fresh values):
JWT_SECRET=$(openssl rand -base64 32)
WEBHOOK_SECRET=$(openssl rand -base64 32)

fly secrets set --app forgeos-api \
  REDIS_URL="<paste REDIS_URL>" \
  JWT_SECRET="$JWT_SECRET" \
  WEBHOOK_SECRET="$WEBHOOK_SECRET" \
  LLM_PROVIDER="stub" \
  CHANNEL_MODE="stub"

# Worker needs the same data-plane secrets (DATABASE_URL from its own attach):
fly postgres attach --app forgeos-worker forgeos-db
fly secrets set --app forgeos-worker \
  REDIS_URL="<paste REDIS_URL>" \
  JWT_SECRET="$JWT_SECRET" \
  WEBHOOK_SECRET="$WEBHOOK_SECRET" \
  LLM_PROVIDER="stub" \
  CHANNEL_MODE="stub"
```

To go live later, set `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`, or `CHANNEL_MODE=live` + provider keys, the same way.

## 5. Deploy (order matters — API first)

```bash
fly deploy --config deploy/fly/api.toml --dockerfile apps/api/Dockerfile .
fly deploy --config deploy/fly/worker.toml --dockerfile apps/worker/Dockerfile .
fly deploy --config deploy/fly/web.toml .
```

The API's `release_command` runs Alembic migrations on every deploy.

## 6. Seed demo data (optional)

```bash
# Proxy the Fly Postgres to localhost, then seed from your machine:
fly proxy 5432 -a forgeos-db &
DATABASE_URL="postgresql://forge:forge@localhost:5432/forge" make seed
kill %1
```

(The `forge` user/password are the compose defaults; use your actual `fly postgres attach` values if you changed them.)

## 7. Open it

- App: **https://forgeos-web.fly.dev**
- API: **https://forgeos-api.fly.dev** (`/healthz` for a quick check)
- Demo login: `demo@forgeos.local` / `demo1234` (after seeding)

## Notes

- `auto_stop_machines = "stop"` lets idle machines sleep; the API keeps `min_machines_running = 1` so the first click is fast. The worker never sleeps (cron jobs need it).
- Affiliate short links (`https://forgeos-api.fly.dev/r/{slug}`) work from anywhere — no login needed.
- Logs: `fly logs --app forgeos-api` (or `-worker`, `-web`).
- If an app name is taken, rename it in the `.toml` `app =` field and in `web.toml`'s `VITE_API_URL`.
