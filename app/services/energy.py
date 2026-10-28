"""Energy buckets, totals and price performance (pure functions)."""

from __future__ import annotations

from typing import Any

from .prices import _num
from .timeutil import fmt, parse_ts


def normalize_node(node: dict[str, Any], type_: str, tz: str) -> dict[str, Any] | None:
    try:
        start, end = parse_ts(str(node["from"])), parse_ts(str(node["to"]))
    except (KeyError, TypeError, ValueError):
        return None
    if type_ == "consumption":
        energy, cost = _num(node.get("consumption")), _num(node.get("cost"))
    else:
        energy, cost = _num(node.get("production")), _num(node.get("profit"))
    return {
        "from": fmt(start, tz),
        "to": fmt(end, tz),
        "energy": energy,
        "cost": cost,
        "unitPrice": _num(node.get("unitPrice")),
        "currency": node.get("currency") if isinstance(node.get("currency"), str) else None,
    }


def _sum(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present), 6) if present else None


def totals(buckets: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    return _sum([b["energy"] for b in buckets]), _sum([b["cost"] for b in buckets])


def price_performance(hourly: list[dict[str, Any]]) -> dict[str, float] | None:
    """Average price paid (Σcost/ΣkWh) vs the plain average hourly price over the same hours.

    `percent` < 0 means consumption was weighted towards cheaper hours.
    """
    priced = [b for b in hourly if b["unitPrice"] is not None]
    if not priced:
        return None
    kwh = 0.0
    cost = 0.0
    for b in priced:
        e = b["energy"] or 0.0
        kwh += e
        cost += b["cost"] if b["cost"] is not None else e * b["unitPrice"]
    if kwh <= 0:
        return None
    avg_paid = cost / kwh
    avg_price = sum(b["unitPrice"] for b in priced) / len(priced)
    if avg_price == 0:
        return None
    return {
        "averagePaid": round(avg_paid, 6),
        "averagePrice": round(avg_price, 6),
        "percent": round((avg_paid / avg_price - 1) * 100, 2),
    }


def in_range(buckets: list[dict[str, Any]], start, end) -> list[dict[str, Any]]:
    return [b for b in buckets if start <= parse_ts(b["from"]) < end]
