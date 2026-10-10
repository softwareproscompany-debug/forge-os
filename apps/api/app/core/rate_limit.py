"""Login rate limiting: per-IP + per-account sliding windows.

M1 (security hardening): throttle brute-force attempts on the login
endpoints. Uses Redis (sorted-set sliding window) when reachable,
otherwise an in-memory store with periodic cleanup.

Limits (per task spec):
  - 5 attempts per minute per IP
  - 10 attempts per hour per account (email)

Exceeding a limit raises ``HTTPException`` 429 with a ``Retry-After``
header. Failed AND successful attempts both count toward the window —
counting only failures would let an attacker probe usernames for free.

OWASP / NIST mapping:
  - OWASP ASVS 2.2.1 (anti-automation), NIST SP 800-63B §5.2.2
    (rate limiting / throttling of authentication attempts).

Part 3 adds per-business hourly quotas for expensive endpoints
(Draven chat, research, generation) to bound AI/provider spend per
tenant. Same store machinery, namespaced keys, env-overridable
defaults. 429s carry ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import functools
import os
import time
import uuid
from collections import defaultdict, deque
from typing import Any

from fastapi import HTTPException, Request, status

# Sliding-window budgets: (max_attempts, window_seconds)
IP_LIMIT = (5, 60)  # 5 attempts / minute / IP
ACCOUNT_LIMIT = (10, 3600)  # 10 attempts / hour / account

_cleanup_interval_s = 300.0


class _MemoryStore:
    """Thread-local-process in-memory sliding-window store.

    Not shared across workers — acceptable fallback when Redis is
    unavailable; the Redis path is authoritative in production.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_cleanup = time.monotonic()

    def _cleanup(self, now: float, max_window: float) -> None:
        if now - self._last_cleanup < _cleanup_interval_s:
            return
        self._last_cleanup = now
        cutoff = now - max_window
        for key in list(self._hits):
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if not dq:
                del self._hits[key]

    def hit(self, key: str, max_attempts: int, window_s: int) -> tuple[bool, float]:
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        self._cleanup(now, _max_window_s())
        dq = self._hits[key]
        cutoff = now - window_s
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_attempts:
            retry_after = max(1, int(dq[0] + window_s - now))
            return False, retry_after
        dq.append(now)
        return True, 0.0


class _RedisStore:
    """Sliding window over a Redis sorted set (score = timestamp)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def hit(self, key: str, max_attempts: int, window_s: int) -> tuple[bool, float]:
        now = time.time()
        cutoff = now - window_s
        pipe = self._client.pipeline()
        zkey = f"forgeos:ratelimit:{key}"
        pipe.zremrangebyscore(zkey, 0, cutoff)
        pipe.zcard(zkey)
        pipe.expire(zkey, window_s + 60)
        _, count, _ = pipe.execute()
        if count >= max_attempts:
            oldest = self._client.zrange(zkey, 0, 0, withscores=True)
            retry_after = 1
            if oldest:
                retry_after = max(1, int(oldest[0][1] + window_s - now))
            return False, retry_after
        member = f"{now}:{time.monotonic_ns()}"
        self._client.zadd(zkey, {member: now})
        return True, 0.0


def _make_store(redis_url: str):
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(redis_url, socket_connect_timeout=1.0)
        client.ping()
        return _RedisStore(client)
    except Exception:
        return _MemoryStore()


_store: Any = None
_store_url: str | None = None


def get_store(redis_url: str):
    """Process-wide store, rebuilt if the Redis URL changes (tests)."""
    global _store, _store_url
    if _store is None or _store_url != redis_url:
        _store = _make_store(redis_url)
        _store_url = redis_url
    return _store


def reset_store() -> None:
    """Test hook: drop the cached store so the next call rebuilds it."""
    global _store, _store_url
    _store = None
    _store_url = None


def client_ip(request: Request) -> str:
    """Best-effort client IP. Trusts X-Forwarded-For (Fly/sprite proxy)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client is not None:
        return request.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# Per-business quotas for expensive endpoints
# ---------------------------------------------------------------------------


def _quota_default(group: str, fallback: int) -> int:
    """Read ``RATE_LIMIT_<GROUP>_PER_HOUR`` from env, else the fallback."""
    try:
        return max(1, int(os.environ.get(f"RATE_LIMIT_{group.upper()}_PER_HOUR", fallback)))
    except (TypeError, ValueError):
        return fallback


#: Per-business hourly budgets: group -> (max_requests, window_seconds).
#: ``draven`` covers conversational chat endpoints, ``research`` covers
#: market-intel research runs, ``generation`` covers ad/asset generation.
QUOTAS: dict[str, tuple[int, int]] = {
    "draven": (_quota_default("draven", 300), 3600),
    "research": (_quota_default("research", 60), 3600),
    "generation": (_quota_default("generation", 120), 3600),
}


def _max_window_s() -> float:
    """Largest sliding window across login limits and quotas (cleanup)."""
    return float(
        max(
            [window for _, window in (IP_LIMIT, ACCOUNT_LIMIT)]
            + [window for _, window in QUOTAS.values()]
        )
    )


def check_business_quota(
    request: Request,
    *,
    redis_url: str,
    business_id: uuid.UUID | str,
    group: str,
) -> None:
    """Enforce the per-business hourly quota for an expensive endpoint group.

    Raises HTTPException 429 with ``Retry-After`` when the budget is
    exhausted. Redis-backed when reachable; otherwise the in-memory
    fallback still applies limits per process (never crashes).
    """
    if group not in QUOTAS:
        raise ValueError(f"unknown quota group: {group!r}")
    limit, window = QUOTAS[group]
    store = get_store(redis_url)
    key = f"quota:{group}:biz:{business_id}"
    allowed, retry_after = store.hit(key, limit, window)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Hourly quota exceeded for {group} "
                f"({limit} requests/hour per business). Try again later."
            ),
            headers={"Retry-After": str(retry_after)},
        )


def quota_limited(group: str):
    """Decorator: enforce the per-business quota for ``group`` on an endpoint.

    Pulls ``request``, ``user`` (or ``admin``), and ``settings`` from the
    endpoint's keyword arguments. If any are missing the check is skipped
    (defensive — endpoints should pass all three).
    """

    def decorator(func):
        @functools.wraps(func)
        def _enforce(kwargs: dict) -> None:
            request = kwargs.get("request")
            user = kwargs.get("user") or kwargs.get("admin")
            settings = kwargs.get("settings")
            if request is None or user is None:
                return
            redis_url = getattr(settings, "REDIS_URL", None) or os.environ.get(
                "REDIS_URL", "redis://localhost:6379/0"
            )
            check_business_quota(
                request,
                redis_url=redis_url,
                business_id=user.business_id,
                group=group,
            )

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def _async_wrapper(*args, **kwargs):
                _enforce(kwargs)
                return await func(*args, **kwargs)

            return _async_wrapper

        @functools.wraps(func)
        def _sync_wrapper(*args, **kwargs):
            _enforce(kwargs)
            return func(*args, **kwargs)

        return _sync_wrapper

    return decorator


def check_login_rate_limit(
    request: Request,
    *,
    redis_url: str,
    account: str,
    scope: str = "login",
) -> None:
    """Enforce per-IP and per-account login throttles.

    Raises HTTPException 429 with Retry-After when either budget is
    exhausted. ``account`` is the normalized login identifier (email).
    ``scope`` namespaces keys (e.g. "login" vs "portal-login").
    """
    store = get_store(redis_url)
    ip = client_ip(request)
    acct_key = account.strip().lower() or "unknown"

    for key, (limit, window) in (
        (f"{scope}:ip:{ip}", IP_LIMIT),
        (f"{scope}:acct:{acct_key}", ACCOUNT_LIMIT),
    ):
        allowed, retry_after = store.hit(key, limit, window)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )
