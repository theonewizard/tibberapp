"""Price normalisation, current slot, cheapest window and refresh timing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .timeutil import fmt, local_day_bounds, parse_ts

LEVELS = ("VERY_CHEAP", "CHEAP", "NORMAL", "EXPENSIVE", "VERY_EXPENSIVE")
SLOT = timedelta(minutes=15)
# Nord Pool day-ahead results are published around 12:45 CET; Tibber shows them shortly after.
PUBLISH_TZ = "Europe/Oslo"
PUBLISH_LOCAL_TIME = (13, 0)
RETRY_AFTER_PUBLISH = timedelta(minutes=15)


def normalize_level(value: Any) -> str | None:
    """Pass through a known PriceLevel; anything else (absent, null, new enum value) becomes None."""
    return value if value in LEVELS else None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalize_point(raw: dict[str, Any], tz: str) -> dict[str, Any] | None:
    try:
        starts = parse_ts(str(raw.get("startsAt")))
    except (TypeError, ValueError):
        return None
    return {
        "startsAt": fmt(starts, tz),
        "total": _num(raw.get("total")),
        "energy": _num(raw.get("energy")),
        "tax": _num(raw.get("tax")),
        "level": normalize_level(raw.get("level")),
    }


def normalize_series(raw: list[dict[str, Any]] | None, tz: str) -> list[dict[str, Any]]:
    points = [p for p in (normalize_point(r, tz) for r in (raw or []) if isinstance(r, dict)) if p]
    return sorted(points, key=lambda p: parse_ts(p["startsAt"]))


def slot_ends(points: list[dict[str, Any]], default: timedelta) -> list[datetime]:
    """End of each slot = start of the next; the last slot gets the default length."""
    starts = [parse_ts(p["startsAt"]) for p in points]
    ends = []
    for i, s in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        ends.append(nxt if nxt is not None and nxt - s <= default else s + default)
    return ends


def current_point(points: list[dict[str, Any]], now: datetime, default: timedelta) -> dict[str, Any] | None:
    for p, end in zip(points, slot_ends(points, default), strict=True):
        if parse_ts(p["startsAt"]) <= now < end:
            return p
    return None


def quarter_slots(points: list[dict[str, Any]], default: timedelta) -> list[tuple[datetime, float]]:
    """Expand every price slot (15 or 60 min) into 15-minute sub-slots with the slot's total."""
    out: list[tuple[datetime, float]] = []
    for p, end in zip(points, slot_ends(points, default), strict=True):
        if p["total"] is None:
            continue
        t = parse_ts(p["startsAt"])
        while t < end:
            out.append((t, p["total"]))
            t += SLOT
    return out


def cheapest_window(
    points: list[dict[str, Any]],
    duration_minutes: int,
    earliest: datetime,
    latest: datetime | None,
    default: timedelta,
) -> dict[str, Any] | None:
    """Cheapest contiguous window by time-weighted average total price. Earliest start wins ties."""
    if duration_minutes <= 0 or duration_minutes % 15:
        raise ValueError("duration must be a positive multiple of 15 minutes")
    k = duration_minutes // 15
    # A window may start in the slot that is running now.
    slots = [(t, v) for t, v in quarter_slots(points, default) if t + SLOT > earliest]
    if latest is not None:
        slots = [(t, v) for t, v in slots if t + SLOT <= latest]
    best: tuple[float, int] | None = None
    for i in range(len(slots) - k + 1):
        window = slots[i : i + k]
        if window[-1][0] - window[0][0] != SLOT * (k - 1):
            continue  # gap in known prices
        avg = sum(v for _, v in window) / k
        if best is None or avg < best[0] - 1e-12:
            best = (avg, i)
    if best is None:
        return None
    start = slots[best[1]][0]
    return {"start": start, "end": start + SLOT * k, "averageTotal": round(best[0], 6)}


def tomorrow_expected_after(now: datetime) -> datetime:
    zone = ZoneInfo(PUBLISH_TZ)
    local = now.astimezone(zone)
    return local.replace(hour=PUBLISH_LOCAL_TIME[0], minute=PUBLISH_LOCAL_TIME[1], second=0, microsecond=0)


def price_ttl(now: datetime, has_tomorrow: bool, tz: str) -> float:
    """Seconds until the cached prices should be refreshed.

    Hourly as a baseline; never past local midnight (today/tomorrow roll over); and, while
    tomorrow is missing, at publish time and then every 15 minutes until it appears.
    """
    now = now.astimezone(UTC)
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - now
    _, midnight = local_day_bounds(0, tz, now)
    ttl = min(next_hour, midnight - now)
    if not has_tomorrow:
        publish = tomorrow_expected_after(now)
        ttl = min(ttl, publish - now) if now < publish else min(ttl, RETRY_AFTER_PUBLISH)
    return max(ttl.total_seconds(), 30.0)
