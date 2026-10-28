"""Time helpers that are correct across DST changes (23- and 25-hour days).

All returned datetimes are in UTC. Python compares and subtracts datetimes that share a ZoneInfo
by wall clock (ignoring DST), so local time is used only to find calendar boundaries and for display.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

RESOLUTIONS = ("hourly", "daily", "weekly", "monthly", "annual")


def parse_ts(value: str) -> datetime:
    """Parse an RFC 3339 / ISO 8601 timestamp; naive values are rejected."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return dt


def fmt(dt: datetime, tz: str) -> str:
    """RFC 3339 with the home's offset, second precision."""
    return dt.astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")


def now_utc() -> datetime:
    return datetime.now(UTC)


def _local_midnight(year: int, month: int, day: int, zone: ZoneInfo) -> datetime:
    return datetime(year, month, day, tzinfo=zone).astimezone(UTC)


def floor_bucket(dt: datetime, resolution: str, tz: str) -> datetime:
    zone = ZoneInfo(tz)
    local = dt.astimezone(zone)
    if resolution == "hourly":
        # Floor in UTC so that both repeated 02:00 hours on a 25-hour day stay distinct.
        return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if resolution == "daily":
        return _local_midnight(local.year, local.month, local.day, zone)
    if resolution == "weekly":
        monday = local.date() - timedelta(days=local.weekday())
        return _local_midnight(monday.year, monday.month, monday.day, zone)
    if resolution == "monthly":
        return _local_midnight(local.year, local.month, 1, zone)
    if resolution == "annual":
        return _local_midnight(local.year, 1, 1, zone)
    raise ValueError(resolution)


def shift(start: datetime, resolution: str, tz: str, n: int) -> datetime:
    """Move a bucket start by n buckets. Calendar units are shifted in local time."""
    zone = ZoneInfo(tz)
    if resolution == "hourly":
        return start.astimezone(UTC) + timedelta(hours=n)
    local = start.astimezone(zone)
    if resolution == "daily":
        d = local.date() + timedelta(days=n)
    elif resolution == "weekly":
        d = local.date() + timedelta(days=7 * n)
    elif resolution == "monthly":
        m = local.year * 12 + (local.month - 1) + n
        return _local_midnight(m // 12, m % 12 + 1, 1, zone)
    elif resolution == "annual":
        return _local_midnight(local.year + n, 1, 1, zone)
    else:
        raise ValueError(resolution)
    return _local_midnight(d.year, d.month, d.day, zone)


def buckets(start: datetime, end: datetime, resolution: str, tz: str) -> list[tuple[datetime, datetime]]:
    """Bucket [from, to) pairs covering [start, end); start is floored to a bucket boundary."""
    out = []
    cur = floor_bucket(start, resolution, tz)
    while cur < end:
        nxt = shift(cur, resolution, tz, 1)
        out.append((cur, nxt))
        cur = nxt
    return out


def local_day_bounds(day_offset: int, tz: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    start = floor_bucket(now or now_utc(), "daily", tz)
    start = shift(start, "daily", tz, day_offset)
    return start, shift(start, "daily", tz, 1)


def previous_period(start: datetime, end: datetime, resolution: str, tz: str) -> datetime:
    """Start of the period of the same calendar length that ends at `start`.

    Uses the coarsest calendar unit both ends align to, so "this month" compares with last month
    and "this day" with yesterday even when the two differ in length (DST, month lengths).
    """
    for unit in ("annual", "monthly", "weekly", "daily"):
        if floor_bucket(start, unit, tz) == start and floor_bucket(end, unit, tz) == end:
            n = len(buckets(start, end, unit, tz))
            return shift(start, unit, tz, -n)
    n = len(buckets(start, end, resolution, tz))
    return shift(start, resolution, tz, -n)
