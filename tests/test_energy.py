from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services import energy as en
from app.services.timeutil import buckets, floor_bucket, shift
from tests.conftest import first_home
from tests.fake_tibber import TZ

Z = ZoneInfo(TZ)


def hourly_nodes(start_local: datetime, hours: int, kwh=1.0, price=1.0):
    out = []
    t = start_local.astimezone(UTC)
    for i in range(hours):
        a, b = t + timedelta(hours=i), t + timedelta(hours=i + 1)
        k = kwh(i) if callable(kwh) else kwh
        p = price(i) if callable(price) else price
        out.append(
            {
                "from": a.astimezone(Z).isoformat(),
                "to": b.astimezone(Z).isoformat(),
                "consumption": k,
                "cost": round(k * p, 6),
                "unitPrice": p,
                "currency": "SEK",
            }
        )
    return out


def daily_nodes(start_local: datetime, days: int, kwh=10.0):
    out = []
    d = start_local
    for _ in range(days):
        nxt = shift(d, "daily", TZ, 1)
        out.append(
            {
                "from": d.isoformat(),
                "to": nxt.isoformat(),
                "consumption": kwh,
                "cost": kwh * 2,
                "unitPrice": 2.0,
                "currency": "SEK",
            }
        )
        d = nxt
    return out


# ---------------------------------------------------------------- DST bucket arithmetic


@pytest.mark.parametrize("day,hours", [((2026, 3, 29), 23), ((2026, 10, 25), 25), ((2026, 10, 26), 24)])
def test_hours_per_local_day(day, hours):
    start = datetime(*day, tzinfo=Z)
    end = shift(start, "daily", TZ, 1)
    assert len(buckets(start, end, "hourly", TZ)) == hours
    assert len(buckets(start, end, "daily", TZ)) == 1


def test_daily_buckets_across_both_dst_changes_are_local_midnights():
    start = datetime(2026, 3, 28, tzinfo=Z)
    bs = buckets(start, datetime(2026, 3, 31, tzinfo=Z), "daily", TZ)
    assert [(b - a).total_seconds() / 3600 for a, b in bs] == [24, 23, 24]
    bs = buckets(datetime(2026, 10, 24, tzinfo=Z), datetime(2026, 10, 27, tzinfo=Z), "daily", TZ)
    assert [(b - a).total_seconds() / 3600 for a, b in bs] == [24, 25, 24]
    assert all(a.astimezone(Z).hour == 0 for a, _ in bs)


def test_calendar_floors_and_shifts():
    t = datetime(2026, 10, 28, 15, 42, tzinfo=Z)  # a Wednesday
    assert floor_bucket(t, "weekly", TZ) == datetime(2026, 10, 26, tzinfo=Z)
    assert floor_bucket(t, "monthly", TZ) == datetime(2026, 10, 1, tzinfo=Z)
    assert shift(datetime(2026, 1, 1, tzinfo=Z), "monthly", TZ, -1) == datetime(2025, 12, 1, tzinfo=Z)
    assert floor_bucket(t, "annual", TZ) == datetime(2026, 1, 1, tzinfo=Z)


def test_fall_back_hourly_floor_keeps_both_repeated_hours():
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 CET
    assert floor_bucket(first, "hourly", TZ) != floor_bucket(second, "hourly", TZ)


# ---------------------------------------------------------------- price performance


def test_price_performance_rewards_shifting_to_cheap_hours():
    rows = [
        en.normalize_node(n, "consumption", TZ)
        for n in hourly_nodes(
            datetime(2026, 10, 27, tzinfo=Z), 4, kwh=lambda i: [3, 1, 0, 0][i], price=lambda i: [1, 1, 3, 3][i]
        )
    ]
    perf = en.price_performance(rows)
    assert perf == {"averagePaid": 1.0, "averagePrice": 2.0, "percent": -50.0}


def test_price_performance_none_without_consumption_or_prices():
    rows = [en.normalize_node(n, "consumption", TZ) for n in hourly_nodes(datetime(2026, 10, 27, tzinfo=Z), 2, kwh=0)]
    assert en.price_performance(rows) is None
    assert en.price_performance([]) is None


def test_price_performance_falls_back_to_unit_price_when_cost_missing():
    rows = [{"energy": 2.0, "cost": None, "unitPrice": 1.0}, {"energy": 0.0, "cost": None, "unitPrice": 3.0}]
    assert en.price_performance(rows)["percent"] == -50.0


# ---------------------------------------------------------------- endpoint, paging, persistence


def test_energy_pages_through_cursor_and_keeps_closed_periods(connected, fake, frozen_now):
    frozen_now(datetime(2026, 10, 28, 12, tzinfo=UTC))
    start = datetime(2026, 10, 25, tzinfo=Z)  # 25-hour day
    fake.energy_nodes["HOURLY"] = hourly_nodes(datetime(2026, 10, 18, tzinfo=Z), 24 * 10 + 1)
    fake.energy_nodes["DAILY"] = daily_nodes(datetime(2026, 10, 1, tzinfo=Z), 27)
    fake.page_size_cap = 7
    hid = first_home(connected)

    r = connected.get(
        f"/v1/homes/{hid}/energy",
        params={
            "type": "consumption",
            "resolution": "hourly",
            "from": start.isoformat(),
            "to": shift(start, "daily", TZ, 1).isoformat(),
        },
    )
    body = r.json()
    assert r.status_code == 200, body
    assert len(body["buckets"]) == 25
    assert body["buckets"][0]["from"] == "2026-10-25T00:00:00+02:00"
    assert body["buckets"][-1]["from"] == "2026-10-25T23:00:00+01:00"
    assert body["totals"]["energy"] == 25.0
    assert body["previous"]["from"] == "2026-10-24T00:00:00+02:00"  # 25 hours earlier
    assert body["totals"]["pricePerformance"]["percent"] == 0.0
    gql_calls = sum(1 for _, u in fake.calls if "api.tibber.com" in u)
    assert gql_calls > 4, "should have paged (page size 7)"

    # Closed hours are in SQLite now: a repeat request needs no upstream call.
    connected.app.state.db.cache_clear("gql:energy")
    n = len(fake.calls)
    again = connected.get(
        f"/v1/homes/{hid}/energy",
        params={
            "type": "consumption",
            "resolution": "hourly",
            "from": start.isoformat(),
            "to": shift(start, "daily", TZ, 1).isoformat(),
        },
    ).json()
    assert again["buckets"] == body["buckets"]
    assert len(fake.calls) == n


def test_energy_daily_with_previous_period_and_production(connected, fake, frozen_now):
    frozen_now(datetime(2026, 10, 28, 12, tzinfo=UTC))
    fake.energy_nodes["DAILY"] = daily_nodes(datetime(2026, 10, 1, tzinfo=Z), 27)
    fake.energy_nodes["HOURLY"] = hourly_nodes(datetime(2026, 10, 1, tzinfo=Z), 27 * 24 + 1)
    hid = first_home(connected)
    params = {
        "type": "consumption",
        "resolution": "daily",
        "from": "2026-10-20T00:00:00+02:00",
        "to": "2026-10-27T00:00:00+01:00",
    }
    body = connected.get(f"/v1/homes/{hid}/energy", params=params).json()
    assert len(body["buckets"]) == 7 and body["totals"]["energy"] == 70.0
    assert body["previous"]["from"] == "2026-10-13T00:00:00+02:00" and body["previous"]["energy"] == 70.0
    assert body["currency"] == "SEK"
    prod = connected.get(f"/v1/homes/{hid}/energy", params={**params, "type": "production"}).json()
    assert prod["totals"]["energy"] == 70.0 and prod["totals"]["pricePerformance"] is None


@pytest.mark.parametrize(
    "params",
    [
        {
            "type": "consumption",
            "resolution": "hourly",
            "from": "2026-10-20T00:00:00",
            "to": "2026-10-21T00:00:00+02:00",
        },
        {
            "type": "consumption",
            "resolution": "hourly",
            "from": "2026-10-21T00:00:00+02:00",
            "to": "2026-10-20T00:00:00+02:00",
        },
        {"type": "gas", "resolution": "hourly", "from": "2026-10-20T00:00:00+02:00", "to": "2026-10-21T00:00:00+02:00"},
        {
            "type": "consumption",
            "resolution": "hourly",
            "from": "2026-01-01T00:00:00+01:00",
            "to": "2026-10-21T00:00:00+02:00",
        },
    ],
)
def test_energy_rejects_bad_input(connected, params):
    hid = first_home(connected)
    r = connected.get(f"/v1/homes/{hid}/energy", params=params)
    assert r.status_code == 422 and r.json()["status"] == 422
