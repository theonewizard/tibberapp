from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services import prices as pr
from tests.conftest import first_home
from tests.fake_tibber import TZ, day_prices, price

Z = ZoneInfo(TZ)


# ---------------------------------------------------------------- levels


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("VERY_CHEAP", "VERY_CHEAP"),
        ("CHEAP", "CHEAP"),
        ("NORMAL", "NORMAL"),
        ("EXPENSIVE", "EXPENSIVE"),
        ("VERY_EXPENSIVE", "VERY_EXPENSIVE"),
        (None, None),
        ("SOMETHING_NEW", None),
        (3, None),
    ],
)
def test_price_level_is_passed_through_only_when_known(raw, expected):
    assert pr.normalize_level(raw) == expected


def test_missing_level_is_null_not_invented():
    p = pr.normalize_point(price(datetime(2026, 10, 28, 10, tzinfo=Z), 1.5, level=None), TZ)
    assert p["level"] is None and p["total"] == 1.5


def test_timestamps_are_rfc3339_with_home_offset():
    p = pr.normalize_point({"startsAt": "2026-10-28T09:00:00.000Z", "total": 1}, TZ)
    assert p["startsAt"] == "2026-10-28T10:00:00+01:00"


# ---------------------------------------------------------------- DST days


@pytest.mark.parametrize(
    "day,quarters,hours",
    [
        (datetime(2026, 3, 29), 92, 23),  # spring forward
        (datetime(2026, 10, 25), 100, 25),  # fall back
        (datetime(2026, 10, 28), 96, 24),
    ],
)
def test_dst_day_slot_counts(day, quarters, hours):
    assert len(pr.normalize_series(day_prices(day, 15), TZ)) == quarters
    assert len(pr.normalize_series(day_prices(day, 60), TZ)) == hours


def test_current_point_on_repeated_hour_of_fall_back_day():
    series = pr.normalize_series(day_prices(datetime(2026, 10, 25), 60), TZ)
    # 02:30 local happens twice; the second one is 01:30 UTC.
    now = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    cur = pr.current_point(series, now, timedelta(hours=1))
    assert cur["startsAt"] == "2026-10-25T02:00:00+01:00"


def test_cheapest_window_spans_the_dst_gap_correctly():
    series = pr.normalize_series(day_prices(datetime(2026, 3, 29), 60), TZ)
    # Make 01:00 and 03:00 (contiguous in real time: 02:00 does not exist) the cheapest pair.
    for p in series:
        if p["startsAt"] in ("2026-03-29T01:00:00+01:00", "2026-03-29T03:00:00+02:00"):
            p["total"] = 0.01
    start = datetime(2026, 3, 28, 23, tzinfo=UTC)
    win = pr.cheapest_window(series, 120, start, None, timedelta(hours=1))
    assert win["start"].astimezone(Z).isoformat() == "2026-03-29T01:00:00+01:00"
    assert win["end"] - win["start"] == timedelta(hours=2)
    assert win["averageTotal"] == pytest.approx(0.01)


def test_cheapest_window_mixes_hourly_prices_into_quarters_and_respects_bounds():
    base = datetime(2026, 10, 28, 0, tzinfo=Z)
    series = pr.normalize_series([price(base + timedelta(hours=h), t) for h, t in enumerate([3, 1, 2, 0.5, 5])], TZ)
    win = pr.cheapest_window(series, 60, base, base + timedelta(hours=3), timedelta(hours=1))
    assert win["start"] == base + timedelta(hours=1)  # 03:00 is cheaper but outside `latest`
    win = pr.cheapest_window(series, 45, base, None, timedelta(hours=1))
    assert win["start"] == base + timedelta(hours=3) and win["averageTotal"] == 0.5
    assert pr.cheapest_window(series, 600, base, None, timedelta(hours=1)) is None
    with pytest.raises(ValueError):
        pr.cheapest_window(series, 20, base, None, timedelta(hours=1))


def test_cheapest_window_skips_gaps():
    base = datetime(2026, 10, 28, 0, tzinfo=Z)
    series = pr.normalize_series(
        [price(base, 0.1), price(base + timedelta(hours=2), 0.1), price(base + timedelta(hours=3), 9)], TZ
    )
    win = pr.cheapest_window(series, 120, base, None, timedelta(hours=1))
    assert win["start"] == base + timedelta(hours=2)


# ---------------------------------------------------------------- refresh timing


def test_ttl_is_hourly_when_tomorrow_known():
    now = datetime(2026, 10, 28, 14, 20, tzinfo=Z)
    assert pr.price_ttl(now, True, TZ) == 40 * 60


def test_ttl_waits_for_publish_time_then_polls_every_15_minutes():
    before = datetime(2026, 10, 28, 12, 50, tzinfo=Z)
    assert pr.price_ttl(before, False, TZ) == 10 * 60
    after = datetime(2026, 10, 28, 13, 5, tzinfo=Z)
    assert pr.price_ttl(after, False, TZ) == 15 * 60


def test_ttl_never_crosses_local_midnight():
    now = datetime(2026, 10, 28, 23, 50, tzinfo=Z)
    assert pr.price_ttl(now, True, TZ) == 10 * 60


# ---------------------------------------------------------------- endpoint


def test_prices_endpoint(connected, fake, frozen_now):
    now = datetime(2026, 10, 28, 10, 20, tzinfo=Z)
    frozen_now(now.astimezone(UTC))
    fake.prices_today = day_prices(datetime(2026, 10, 28), 15)
    fake.prices_tomorrow = []
    hid = first_home(connected)
    body = connected.get(f"/v1/homes/{hid}/prices").json()
    assert body["available"] and body["resolution"] == "quarter_hourly"
    assert len(body["today"]) == 96 and body["tomorrow"] == []
    assert body["current"]["startsAt"] == "2026-10-28T10:15:00+01:00"
    assert body["current"]["level"] == "NORMAL"
    assert body["tomorrowExpectedAfter"] == "2026-10-28T13:00:00+01:00"
    assert body["currency"] == "SEK"
    # Served from the shared cache the second time
    n = len(fake.calls)
    connected.get(f"/v1/homes/{hid}/prices")
    assert len(fake.calls) == n

    win = connected.get(f"/v1/homes/{hid}/prices/cheapest-window", params={"duration": 120}).json()
    assert win["window"]["start"].endswith("+01:00") and win["durationMinutes"] == 120


def test_no_price_contract(connected, fake):
    fake.no_contract = True
    hid = first_home(connected)
    body = connected.get(f"/v1/homes/{hid}/prices").json()
    assert body["available"] is False and body["current"] is None and body["today"] == []


def test_stale_prices_are_served_with_stale_since(connected, fake, frozen_now):
    frozen_now(datetime(2026, 10, 28, 9, 20, tzinfo=UTC))
    fake.prices_today = day_prices(datetime(2026, 10, 28), 60)
    hid = first_home(connected)
    assert connected.get(f"/v1/homes/{hid}/prices?resolution=hourly").json()["meta"]["stale"] is False
    frozen_now(datetime(2026, 10, 28, 10, 20, tzinfo=UTC))
    import time as _t

    db = connected.app.state.db
    key = f"gql:prices:{hid}:hourly"
    db._x("UPDATE cache SET fetched_at=? WHERE key=?", (_t.time() - 7200, key))
    fake.gql_fail = True
    body = connected.get(f"/v1/homes/{hid}/prices?resolution=hourly").json()
    assert body["meta"]["stale"] is True and body["meta"]["staleSince"]
    assert body["current"]["startsAt"] == "2026-10-28T11:00:00+01:00"


def test_bad_resolution_is_rejected(connected):
    hid = first_home(connected)
    r = connected.get(f"/v1/homes/{hid}/prices?resolution=minutely")
    assert r.status_code == 422 and r.headers["content-type"] == "application/problem+json"
