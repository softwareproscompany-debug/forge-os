"""arq worker settings for ForgeOS.

Run with: ``arq worker.settings.WorkerSettings`` (from ``apps/worker`` so
the ``worker`` package is importable). The dotted path is overridable via
``ARQ_WORKER_SETTINGS`` (see docker-compose.yml).

* ``redis_settings`` — built from ``REDIS_URL`` (``redis://localhost:6379/0``
  fallback for local dev).
* ``functions`` — the seven jobs in :mod:`worker.jobs`.
* ``cron_jobs`` — :func:`campaign_tick` every 60 seconds, plus
  :func:`autopilot_plan` hourly on the hour. The autopilot cron is hourly
  (not daily) on purpose: each business has its own timezone, and the job
  drafts the week's plan at the business's *local* Monday 06:00, so it must
  wake every hour to catch every timezone's 06:00. The job itself is a
  no-op outside that window.
* ``queue_name`` — ``forge`` per CONTRACTS.md (overridable via
  ``ARQ_QUEUE_NAME``).
"""

from __future__ import annotations

import logging
import os

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import text

from forge_db.session import SessionLocal
from worker.jobs import (
    autopilot_plan,
    autopilot_plan_now,
    campaign_tick,
    compliance_scan,
    generate_asset,
    handle_event,
    send_message,
    weekly_summary,
)

log = logging.getLogger("forgeos.worker")


def _redis_settings() -> RedisSettings:
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0").strip()
    try:
        return RedisSettings.from_dsn(url)
    except Exception as exc:
        raise RuntimeError(f"Invalid REDIS_URL={url!r}: {exc}") from exc


async def on_startup(ctx: dict) -> None:
    """Verify Postgres is reachable at worker boot; log loudly if not.

    Non-fatal: the process stays up so the outage is visible in the worker
    logs (and each job logs its own DB error), rather than silently doing
    nothing.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        log.error(
            "worker startup: DATABASE_URL is not set — jobs will fail until it "
            "is configured"
        )
        return
    try:
        db = SessionLocal(url)()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        log.info("worker startup: database connectivity OK")
    except Exception:
        log.exception(
            "worker startup: cannot reach Postgres (DATABASE_URL=%r). "
            "Jobs will fail until the database is reachable.",
            url,
        )


async def on_shutdown(ctx: dict) -> None:
    log.info("worker shutdown")


class WorkerSettings:
    """arq settings. Import path: ``worker.settings.WorkerSettings``."""

    functions = [
        generate_asset,
        send_message,
        campaign_tick,
        handle_event,
        autopilot_plan,
        autopilot_plan_now,
        weekly_summary,
    ]
    # campaign_tick: every 60s at :00. autopilot_plan: every 15 minutes — it
    # evaluates each business's configured plan schedule
    # (autopilot_settings.plan_day/plan_hour/plan_cadence, default local
    # Monday 06:00 weekly) and drafts whatever is due. The 15-minute engine
    # tick covers every timezone and every per-tenant schedule; the job
    # itself decides per business whether this tick is its moment.
    # autopilot_plan_now is *not* a cron: it is the manual "run now" trigger
    # enqueued by POST /api/v1/autopilot/plan/run-now.
    # weekly_summary: every hour at :00 — it cuts each business's weekly
    # evidence summary only at that business's local Sunday 23:00. A single
    # daily cron cannot honor per-business timezones, hence the hourly wake
    # with the gating inside the job. NOTE: minute={0} is load-bearing —
    # arq treats an omitted minute as a wildcard (every minute).
    cron_jobs = [
        cron(campaign_tick, minute={*range(60)}),
        cron(autopilot_plan, hour={*range(24)}, minute={0, 15, 30, 45}),
        cron(weekly_summary, hour={*range(24)}, minute={0}),
        # compliance_scan: daily at 03:00 UTC — flags FTC/affiliate issues
        # for human review (never auto-edits or auto-deletes).
        cron(compliance_scan, hour={3}, minute={0}),
    ]
    redis_settings = _redis_settings()
    queue_name = os.environ.get("ARQ_QUEUE_NAME", "forge")
    on_startup = on_startup
    on_shutdown = on_shutdown
    # Single-process defaults are fine; tune via env in production if needed.
    job_timeout = int(os.environ.get("ARQ_JOB_TIMEOUT", "300"))
    max_tries = int(os.environ.get("ARQ_MAX_TRIES", "5"))
    health_check_interval = 30
