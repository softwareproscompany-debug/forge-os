"""arq worker settings for ForgeOS.

Run with: ``arq worker.settings.WorkerSettings`` (from ``apps/worker`` so
the ``worker`` package is importable). The dotted path is overridable via
``ARQ_WORKER_SETTINGS`` (see docker-compose.yml).

* ``redis_settings`` — built from ``REDIS_URL`` (``redis://localhost:6379/0``
  fallback for local dev).
* ``functions`` — the four jobs in :mod:`worker.jobs`.
* ``cron_jobs`` — :func:`campaign_tick` every 60 seconds.
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
from worker.jobs import campaign_tick, generate_asset, handle_event, send_message

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

    functions = [generate_asset, send_message, campaign_tick, handle_event]
    cron_jobs = [cron(campaign_tick, minute={*range(60)})]  # every 60s, at :00
    redis_settings = _redis_settings()
    queue_name = os.environ.get("ARQ_QUEUE_NAME", "forge")
    on_startup = on_startup
    on_shutdown = on_shutdown
    # Single-process defaults are fine; tune via env in production if needed.
    job_timeout = int(os.environ.get("ARQ_JOB_TIMEOUT", "300"))
    max_tries = int(os.environ.get("ARQ_MAX_TRIES", "5"))
    health_check_interval = 30
