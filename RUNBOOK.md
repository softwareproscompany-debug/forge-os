# ForgeOS — Runbook

Common operations for the local compose stack and the k8s deployment.
Assumes `kubectl` is pointed at the right cluster and the `forgeos` namespace
exists (`kubectl apply -k infra/k8s/base` or the prod overlay).

## Viewing logs

```bash
# compose — everything, or one service
make logs
docker compose logs -f api
docker compose logs -f worker

# k8s — everything, or one deployment (previous container on crash)
kubectl -n forgeos logs -l app=api --tail=200 -f
kubectl -n forgeos logs -l app=worker --tail=200 -f
kubectl -n forgeos logs deploy/api -p --tail=200   # crashed pod
```

The worker logs the arq job lifecycle (`generate_asset`, `send_message`,
`campaign_tick`, `handle_event`) with job ids — grep the `job_id` returned by
`POST /api/v1/assets/generate` to trace one generation end to end.

## Re-running a failed job

arq retries transient failures automatically (see `send_message` backoff in
ARCHITECTURE.md). For a job that exhausted retries or failed permanently:

1. Find the failure: worker logs, or query the DB:
   ```sql
   SELECT id, channel, to_address, status, error, updated_at
   FROM sends WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 20;
   ```
   (adjust column names to the api workstream's timestamp columns if they
   differ from `updated_at`.)
2. Fix the root cause (bad template variable, revoked provider key, …).
3. Re-enqueue: create a **new** `sends` row copying the failed one with
   `status='queued'` and `error=NULL`, or simply re-run the step for that
   contact. Never flip `failed` → `queued` in place — the audit trail matters.

For a stuck `generate_asset`, `POST /api/v1/assets/generate` again; the old
`draft` row can be deleted or left for forensics.

## Re-queueing sends

```sql
-- Re-queue one send (keeps history; the worker picks up status='queued'):
INSERT INTO sends (business_id, campaign_id, step_id, contact_id, channel,
                   asset_id, to_address, subject, body, status)
SELECT business_id, campaign_id, step_id, contact_id, channel,
       asset_id, to_address, subject, body, 'queued'
FROM sends WHERE id = '<failed-send-uuid>';

-- Pause a campaign immediately (stops campaign_tick from advancing it):
-- POST /api/v1/campaigns/{id}/pause
```

## When sends stall (nothing going out)

Check in this order:

1. **Worker alive?** `docker compose ps worker` / `kubectl -n forgeos get deploy worker`.
   No worker = queue grows silently; arq dashboard/logs show pending depth.
2. **Redis reachable?** From the worker container: `redis-cli -u "$REDIS_URL" ping`.
   If Redis was wiped, jobs are lost but recoverable: `campaign_tick`
   re-derives due sends from `campaign_enrollments.next_run_at`.
3. **Campaign actually running?** `GET /api/v1/campaigns/{id}` → `status`
   must be `running`; `starts_at` must be in the past.
4. **Quiet hours / daily cap**: `GET /api/v1/autopilot` — sends pause during
   quiet hours and stop when `daily_send_cap` is hit; they resume automatically.
5. **Consent gate**: `send_message` fails sends for contacts without
   `consent_email`/`consent_sms` or with `unsubscribed=true`. Check the
   `sends.error` column — consent rejections are explicit, not silent.
6. **Provider keys**: with `CHANNEL_MODE=live` but empty keys, providers raise
   auth errors and arq retries burn. Confirm with KEYS.md that the right vars
   are set, or drop back to `CHANNEL_MODE=stub` and watch `/dev/outbox`.

## Rotating the JWT secret

`JWT_SECRET` signs all API tokens; rotation invalidates every existing token
(users re-login — no data loss).

```bash
# 1. Generate and set the new secret (compose)
NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
# put it in .env as JWT_SECRET=<value>, then:
docker compose up -d api worker

# k8s:
kubectl -n forgeos create secret generic forgeos-secrets \
  --from-literal=JWT_SECRET="$NEW_SECRET" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n forgeos rollout restart deploy/api deploy/worker
```

Do this immediately if the dev default (`dev-secret-change-me`) ever reaches
any shared environment.

## Backup / restore postgres

```bash
# compose — backup (run from repo root)
docker compose exec postgres pg_dump -U forge forge | gzip > backup-$(date +%F).sql.gz

# restore into a fresh stack (down -v first if you want a clean slate)
gunzip -c backup-2026-10-08.sql.gz | docker compose exec -T postgres psql -U forge -d forge

# k8s — backup from the StatefulSet pod
kubectl -n forgeos exec postgres-0 -- pg_dump -U forge forge | gzip > backup-$(date +%F).sql.gz
```

The k8s StatefulSet keeps data on its PVC (`volumeClaimTemplates`), so plain
pod restarts and redeploys are safe. For disaster recovery, schedule the
`pg_dump` above as a CronJob writing to object storage (not included — do this
before prod holds real data).

## Scaling workers

```bash
# compose — more arq workers for a send burst
docker compose up -d --scale worker=4

# k8s — the prod overlay already ships an HPA (2–8 replicas, CPU 75%);
# to pin a fixed count temporarily:
kubectl -n forgeos scale deploy/worker --replicas=6
```

Watch `daily_send_cap` and provider rate limits first — adding workers past the
provider's quota just produces faster 429s (see KEYS.md per-provider notes).

## Autopilot planner (Card 4)

**Diagnosis, 2026-10-09** — the planner "wasn't working right" for two
code-level reasons (the job and its tests were sound):

1. **Approved plans never ran.** `POST /autopilot/plan/approve` created the
   campaign as `scheduled`, but `campaign_tick` only processes `running`
   campaigns and nothing ever promoted `scheduled → running`. Approving a
   plan silently did nothing until someone manually launched the campaign.
   Fix: approval now creates the campaign **running** — the human approval
   is the launch gate, so the week runs itself.
2. **Sends drifted off the planned days.** Steps stored `delay_hours=day*24`
   against a Monday 09:00 `starts_at`, but the tick sends the first due
   step immediately and spaces later steps from the *actual* send time — so
   a Tue/Thu/Sat plan actually sent Mon/Thu/Mon. Fix: `starts_at` anchors
   to the first item's day at 09:00 business-local and steps carry true
   inter-step gaps (`(day[i]-day[i-1])*24`).

**Scheduling is now per business** (`autopilot_settings`: `plan_day` 0–6,
`plan_hour` 0–23, `plan_cadence` weekly|biweekly; defaults Monday 06:00
weekly). The engine ticks every 15 minutes but each business drafts only
inside its own configured window; `biweekly` drafts at most once per ~13
days (tracked on `last_planned_at`). Edit it on the Autopilot page
("Planner schedule") or `PUT /api/v1/autopilot`.

**Manual trigger:** `POST /api/v1/autopilot/plan/run-now` (owner/admin)
drafts this week's plan immediately via the worker's `autopilot_plan_now`
job — ignores the schedule, stays idempotent per week
(`created=false` + the existing plan). 503 = worker queue unreachable
(Redis down / worker not running — this is also the first thing to check
when *no* plan ever appears: the cron only runs where the worker runs).

## Seeding / resetting demo data

```bash
make seed      # idempotent; safe to re-run any time
```

`infra/seed.py` checks existence before every insert. There is no "unseed":
to wipe demo data, `docker compose down -v` (destroys the pgdata volume) and
re-run `make migrate && make seed`.

## Migrations

```bash
make migrate   # alembic upgrade head via the api image entrypoint
```

Migrations live in `packages/forge-db/alembic` (api workstream). Keep them
backward-compatible with the running code (expand-then-contract) because
compose/k8s roll api and worker independently.
