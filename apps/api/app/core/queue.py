"""Best-effort arq job enqueueing over Redis.

The API never 500s because the queue is down: enqueue helpers return
``None`` (and the caller surfaces ``job_id: null`` + a ``warning`` field)
when Redis is unreachable.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("forgeos.queue")


async def enqueue_job(redis_url: str, job_name: str, *args: Any) -> str | None:
    """Enqueue ``job_name(*args)`` on the ``forge`` queue.

    Returns the arq job id, or ``None`` when Redis cannot be reached.
    """
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(redis_url))
        try:
            job = await pool.enqueue_job(job_name, *args)
            return job.job_id
        finally:
            try:
                await pool.aclose()
            except Exception:  # noqa: BLE001 - closing must not fail the request
                log.debug("arq pool close failed", exc_info=True)
    except Exception as exc:  # noqa: BLE001 - queue outage is a soft failure
        log.warning("Could not enqueue arq job %r: %s", job_name, exc)
        return None
