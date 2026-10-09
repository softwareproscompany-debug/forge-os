"""Pure scheduling helpers for the ForgeOS worker.

No DB, no network, no environment access — everything here is a pure
function of its arguments, which makes it trivially unit-testable.

Timezone contract: all datetimes are timezone-aware. Helpers that read
timestamps back from the database should first pass them through
:func:`ensure_aware` (SQLite round-trips ``timestamptz`` columns as naive
datetimes; Postgres returns them aware).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "UTC",
    "utcnow",
    "ensure_aware",
    "as_naive_utc",
    "to_campaign_tz",
    "start_of_day",
    "in_quiet_hours",
    "next_send_time",
    "add_delay_hours",
]

UTC = timezone.utc


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_aware(value: datetime | None, assume: str = "UTC") -> datetime | None:
    """Return ``value`` as a timezone-aware datetime.

    Naive values are assumed to already be in ``assume`` (UTC by
    convention: the worker always writes UTC). Returns ``None`` unchanged.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    try:
        tz = ZoneInfo(assume)
    except ZoneInfoNotFoundError:
        tz = UTC
    return value.replace(tzinfo=tz)


def as_naive_utc(value: datetime) -> datetime:
    """Convert an aware datetime to naive UTC.

    Used for SQL-side comparisons so the same parameter works on both
    Postgres (``timestamptz``, compared in the session timezone — UTC in
    our deployments) and SQLite (which stores datetimes naive).
    """
    aware = ensure_aware(value)
    assert aware is not None
    return aware.astimezone(UTC).replace(tzinfo=None)


def to_campaign_tz(now: datetime, tz_name: str | None) -> datetime:
    """Convert ``now`` to the campaign's timezone (falls back to UTC)."""
    aware = ensure_aware(now)
    assert aware is not None
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = UTC
    return aware.astimezone(tz)


def start_of_day(now: datetime) -> datetime:
    """Midnight at the start of ``now``'s calendar day (same tzinfo)."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def in_quiet_hours(now: datetime, quiet_start: int, quiet_end: int) -> bool:
    """True if ``now`` (already in the campaign's local timezone) falls in
    the quiet window ``[quiet_start, quiet_end)`` expressed in local hours.

    When ``quiet_start > quiet_end`` the window wraps past midnight
    (e.g. 22 -> 8 means 22:00-07:59). When the two are equal there is no
    quiet window and this always returns False.
    """
    if quiet_start == quiet_end:
        return False
    hour = now.hour + now.minute / 60 + now.second / 3600 + now.microsecond / 3_600_000_000
    if quiet_start < quiet_end:
        return quiet_start <= hour < quiet_end
    return hour >= quiet_start or hour < quiet_end


def next_send_time(now: datetime, quiet_start: int, quiet_end: int) -> datetime:
    """Return ``now`` when outside quiet hours, else the next quiet-end boundary.

    ``now`` must already be in the campaign's local timezone; the returned
    datetime keeps the same tzinfo. The boundary is always strictly after
    ``now``.
    """
    if not in_quiet_hours(now, quiet_start, quiet_end):
        return now
    if quiet_start < quiet_end:
        # Non-wrapping window (e.g. 9 -> 17): the end boundary is later today.
        boundary = now.replace(hour=quiet_end, minute=0, second=0, microsecond=0)
    elif now.hour >= quiet_start:
        # Overnight window, evening side (e.g. 23:30 with 22 -> 8): tomorrow.
        boundary = (now + timedelta(days=1)).replace(
            hour=quiet_end, minute=0, second=0, microsecond=0
        )
    else:
        # Overnight window, morning side (e.g. 07:30 with 22 -> 8): today.
        boundary = now.replace(hour=quiet_end, minute=0, second=0, microsecond=0)
    if boundary <= now:
        boundary += timedelta(days=1)
    return boundary


def add_delay_hours(now: datetime, delay_hours: int) -> datetime:
    """Schedule ``delay_hours`` after ``now`` (drip-step spacing)."""
    return now + timedelta(hours=delay_hours)
