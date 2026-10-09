"""Best-effort arq job enqueueing over Redis.

The API never 500s because the queue is down: enqueue helpers return
``None`` (and the caller surfaces ``job_id: null`` + a ``warning`` field)
when Redis is unreachable.
"""

from __future__ import annotations

import asyncio
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
            job = await pool.enqueue_job(job_name, *args, _queue_name="forge")
            return job.job_id
        finally:
            try:
                await pool.aclose()
            except Exception:  # noqa: BLE001 - closing must not fail the request
                log.debug("arq pool close failed", exc_info=True)
    except Exception as exc:  # noqa: BLE001 - queue outage is a soft failure
        log.warning("Could not enqueue arq job %r: %s", job_name, exc)
        return None


async def enqueue_and_await(
    redis_url: str,
    job_name: str,
    *args: Any,
    timeout: float = 60.0,
) -> tuple[str | None, Any]:
    """Enqueue ``job_name(*args)`` on the ``forge`` queue and wait for it.

    Returns ``(job_id, result)`` where ``result`` is whatever the job
    returned. ``(None, None)`` when Redis cannot be reached; ``(job_id,
    None)`` when the job did not finish within ``timeout`` seconds. A job
    that raised is re-raised to the caller (unlike :func:`enqueue_job`,
    a manual trigger needs the real answer, not a soft ``None``).
    """
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(redis_url))
        try:
            job = await pool.enqueue_job(job_name, *args, _queue_name="forge")
            try:
                result = await asyncio.wait_for(job.result(), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning(
                    "arq job %r (%s) did not finish within %ss",
                    job_name,
                    job.job_id,
                    timeout,
                )
                return job.job_id, None
            return job.job_id, result
        finally:
            try:
                await pool.aclose()
            except Exception:  # noqa: BLE001 - closing must not fail the request
                log.debug("arq pool close failed", exc_info=True)
    except Exception as exc:  # noqa: BLE001 - queue outage surfaces as (None, None)
        log.warning("Could not enqueue arq job %r: %s", job_name, exc)
        return None, None
