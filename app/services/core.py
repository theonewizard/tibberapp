"""Use cases behind the REST API: shared server-side cache, id mapping and freshness metadata."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import Settings
from ..db import Database
from ..upstream.dataapi import DataAPI
from ..upstream.gql import (
    CONSUMPTION_QUERY,
    HOMES_QUERY,
    PAGE_LIMITS,
    PRICES_QUERY,
    PRODUCTION_QUERY,
    GraphQL,
)
from ..upstream.http import NotConnected, UpstreamError
from . import energy as en
from . import prices as pr
from .timeutil import (
    buckets,
    floor_bucket,
    fmt,
    local_day_bounds,
    now_utc,
    parse_ts,
    previous_period,
    shift,
)

HOMES_TTL = 3600
DEVICE_TTL = 300
ENERGY_FETCH_TTL = 3600
MAX_SPAN_DAYS = {"hourly": 93, "daily": 3 * 366, "weekly": 10 * 366, "monthly": 20 * 366, "annual": 50 * 366}
PERFORMANCE_MAX_DAYS = 400
RESOLUTION_GQL = {"hourly": "HOURLY", "daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY", "annual": "ANNUAL"}
PRICE_RES = {"quarter_hourly": ("QUARTER_HOURLY", timedelta(minutes=15)), "hourly": ("HOURLY", timedelta(hours=1))}


class HomeNotFound(Exception):
    pass


class DeviceNotFound(Exception):
    pass


class BadRequest(Exception):
    pass


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts else None


def _meta(row) -> dict[str, Any]:
    return {
        "fetchedAt": _iso(row["fetched_at"]),
        "stale": row["stale_since"] is not None,
        "staleSince": _iso(row["stale_since"]),
    }


def fresh_meta() -> dict[str, Any]:
    return {"fetchedAt": _iso(time.time()), "stale": False, "staleSince": None}


class Service:
    def __init__(self, settings: Settings, db: Database, gql: GraphQL, data: DataAPI) -> None:
        self.s = settings
        self.db = db
        self.gql = gql
        self.data = data
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ cache
    async def cached(
        self,
        key: str,
        ttl: float | Callable[[Any], float],
        fetch: Callable[[str | None], Awaitable[tuple[Any, str | None] | None]],
        force: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        """Serve `key` from the shared cache, refreshing it once per TTL across all sessions.

        `fetch(etag)` returns (body, etag), or None for "304 Not Modified".
        If a refresh fails and an older copy exists, it is served with meta.stale=true.
        """
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            row = self.db.cache_get(key)
            if row is not None and not force:
                body = json.loads(row["body"])
                limit = ttl(body) if callable(ttl) else ttl
                if row["stale_since"] is None and time.time() - row["fetched_at"] < limit:
                    return body, _meta(row)
            try:
                result = await fetch(row["etag"] if row is not None else None)
            except NotConnected:
                raise
            except UpstreamError:
                if row is None:
                    raise
                self.db.cache_mark_stale(key)
                row = self.db.cache_get(key)
                return json.loads(row["body"]), _meta(row)
            if result is None and row is not None:
                self.db.cache_touch(key)
            else:
                assert result is not None
                self.db.cache_put(key, result[0], result[1])
            row = self.db.cache_get(key)
            return json.loads(row["body"]), _meta(row)

    # ------------------------------------------------------------------ homes
    async def homes(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        gql_homes: list[dict[str, Any]] | None = None
        data_homes: list[dict[str, Any]] | None = None
        metas = []
        if self.gql.token():

            async def fetch_gql(_etag):
                d = await self.gql.query(HOMES_QUERY)
                return [h for h in (d.get("viewer") or {}).get("homes") or [] if h], None

            gql_homes, m = await self.cached("gql:homes", HOMES_TTL, fetch_gql)
            metas.append(m)
        if self.data.oauth.connected():

            async def fetch_data(etag):
                r = await self.data.homes(etag)
                return None if r.status == 304 else (r.body.get("homes", []), r.etag)

            try:
                data_homes, m = await self.cached("data:homes", HOMES_TTL, fetch_data)
                metas.append(m)
            except NotConnected:
                data_homes = None
        merged = self._merge_homes(gql_homes or [], data_homes or [])
        meta = fresh_meta()
        if metas:
            stale = [m for m in metas if m["stale"]]
            meta = stale[0] if stale else min(metas, key=lambda m: m["fetchedAt"])
        return merged, meta

    def _merge_homes(self, gql_homes: list[dict], data_homes: list[dict]) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        by_name = {}
        for h in gql_homes:
            name = h.get("appNickname") or "Home"
            tz = h.get("timeZone") or self.s.tz
            rt = bool((h.get("features") or {}).get("realTimeConsumptionEnabled"))
            # The Data API documents its home as "the same home you see inside the Tibber app";
            # match on identical ids first, then on the user-visible name.
            match = next((d for d in data_homes if d.get("id") == h["id"]), None)
            hid = self.db.upsert_home(
                gql_id=h["id"], data_id=match["id"] if match else None, name=name, time_zone=tz, realtime=rt
            )
            out[hid] = {
                "id": hid,
                "name": name,
                "timeZone": tz,
                "realTimeConsumption": rt,
                "sources": {"prices": True, "devices": match is not None},
            }
            by_name.setdefault(name, hid)
        for d in data_homes:
            if any(h["id"] == d.get("id") for h in gql_homes):
                continue
            name = d.get("name") or "Home"
            existing = by_name.get(name)
            if existing and not out[existing]["sources"]["devices"]:
                row = self.db.get_home(existing)
                self.db.upsert_home(
                    gql_id=row["gql_id"],
                    data_id=d["id"],
                    name=name,
                    time_zone=row["time_zone"],
                    realtime=bool(row["realtime"]),
                )
                out[existing]["sources"]["devices"] = True
                continue
            hid = self.db.upsert_home(gql_id=None, data_id=d["id"], name=name, time_zone=self.s.tz, realtime=False)
            out[hid] = {
                "id": hid,
                "name": name,
                "timeZone": self.s.tz,
                "realTimeConsumption": False,
                "sources": {"prices": False, "devices": True},
            }
        return sorted(out.values(), key=lambda h: h["name"])

    def home(self, home_id: str):
        """Object-level authorisation: only homes this backend has itself listed from upstream exist."""
        row = self.db.get_home(home_id)
        if row is None:
            raise HomeNotFound()
        return row

    # ------------------------------------------------------------------ prices
    async def prices(self, home_id: str, resolution: str, now: datetime | None = None) -> dict[str, Any]:
        home = self.home(home_id)
        if not home["gql_id"]:
            raise NotConnected("graphql")
        now = now or now_utc()
        tz = home["time_zone"]
        gql_res, slot = PRICE_RES[resolution]
        today_start, _ = local_day_bounds(0, tz, now)

        async def fetch(_etag):
            d = await self.gql.query(PRICES_QUERY, {"homeId": home["gql_id"], "resolution": gql_res})
            h = (d.get("viewer") or {}).get("home") or {}
            sub = h.get("currentSubscription")
            info = (sub or {}).get("priceInfo")
            if not sub or not info:
                return {"available": False, "day": fmt(today_start, tz)}, None
            today = pr.normalize_series(info.get("today"), tz)
            tomorrow = pr.normalize_series(info.get("tomorrow"), tz)
            raw = (info.get("today") or [None])[0] or info.get("current") or {}
            return {
                "available": True,
                "day": fmt(today_start, tz),
                "currency": raw.get("currency"),
                "today": today,
                "tomorrow": tomorrow,
            }, None

        def ttl(body):
            if body.get("day") != fmt(today_start, tz):
                return 0  # cached copy is from another local day
            return pr.price_ttl(now, bool(body.get("tomorrow")), tz)

        body, meta = await self.cached(f"gql:prices:{home_id}:{resolution}", ttl, fetch)
        stale_day = body.get("day") != fmt(today_start, tz)
        if not body.get("available"):
            return {
                "available": False,
                "resolution": resolution,
                "timeZone": tz,
                "currency": None,
                "current": None,
                "today": [],
                "tomorrow": [],
                "tomorrowExpectedAfter": None,
                "meta": meta,
            }
        series = body["today"] + body["tomorrow"]
        if stale_day:  # served stale across midnight: re-split by the current local day
            series = [p for p in series if parse_ts(p["startsAt"]) >= today_start]
        day_end = shift(today_start, "daily", tz, 1)
        today = [p for p in series if parse_ts(p["startsAt"]) < day_end]
        tomorrow = [p for p in series if parse_ts(p["startsAt"]) >= day_end]
        expected = None if tomorrow else fmt(max(pr.tomorrow_expected_after(now), now), tz)
        return {
            "available": True,
            "resolution": resolution,
            "timeZone": tz,
            "currency": body.get("currency"),
            "current": pr.current_point(series, now, slot),
            "today": today,
            "tomorrow": tomorrow,
            "tomorrowExpectedAfter": expected,
            "meta": meta,
        }

    async def cheapest(
        self, home_id: str, duration: int, start: datetime | None, end: datetime | None, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or now_utc()
        p = await self.prices(home_id, "quarter_hourly", now)
        if start is not None and end is not None and end <= start:
            raise BadRequest("`to` must be after `from`")
        series = p["today"] + p["tomorrow"]
        win = pr.cheapest_window(series, duration, max(start or now, now), end, timedelta(minutes=15))
        tz = p["timeZone"]
        return {
            "durationMinutes": duration,
            "window": None
            if win is None
            else {
                "start": fmt(win["start"], tz),
                "end": fmt(win["end"], tz),
                "averageTotal": win["averageTotal"],
                "currency": p["currency"],
            },
            "meta": p["meta"],
        }

    # ------------------------------------------------------------------ energy
    async def energy(
        self, home_id: str, type_: str, resolution: str, start: datetime, end: datetime, now: datetime | None = None
    ) -> dict[str, Any]:
        home = self.home(home_id)
        if not home["gql_id"]:
            raise NotConnected("graphql")
        if end <= start:
            raise BadRequest("`to` must be after `from`")
        if end - start > timedelta(days=MAX_SPAN_DAYS[resolution]):
            raise BadRequest(f"range too long for {resolution} resolution")
        now = now or now_utc()
        tz = home["time_zone"]
        rows, meta = await self._series(home, type_, resolution, start, end, now)
        first = floor_bucket(start, resolution, tz)
        prev_start = previous_period(first, end, resolution, tz)
        prev_rows, prev_meta = await self._series(home, type_, resolution, prev_start, first, now)
        currency = next((r["currency"] for r in rows + prev_rows if r["currency"]), None)

        async def summary(a: datetime, b: datetime, rows_):
            e, c = en.totals(rows_)
            perf = None
            if type_ == "consumption" and b - a <= timedelta(days=PERFORMANCE_MAX_DAYS):
                hourly = rows_ if resolution == "hourly" else (await self._series(home, type_, "hourly", a, b, now))[0]
                perf = en.price_performance(hourly)
            return {"from": fmt(a, tz), "to": fmt(b, tz), "energy": e, "cost": c, "pricePerformance": perf}

        if prev_meta["stale"] and not meta["stale"]:
            meta = prev_meta
        return {
            "type": type_,
            "resolution": resolution,
            "timeZone": tz,
            "currency": currency,
            "unit": "kWh",
            "buckets": [{k: r[k] for k in ("from", "to", "energy", "cost", "unitPrice")} for r in rows],
            "totals": await summary(first, end, rows),
            "previous": await summary(prev_start, first, prev_rows),
            "meta": meta,
        }

    async def _series(self, home, type_: str, resolution: str, start: datetime, end: datetime, now: datetime):
        """Closed buckets come from SQLite (kept for good); missing or open ones are fetched."""
        tz = home["time_zone"]
        stored = {r["from"]: r for r in self.db.get_energy(home["id"], type_, resolution)}
        wanted = [(a, b) for a, b in buckets(start, end, resolution, tz) if a < now]
        missing = [a for a, b in wanted if b > now or fmt(a, tz) not in stored]
        meta = fresh_meta()
        fetched: dict[str, dict] = {}
        if missing:
            key = f"gql:energy:{home['id']}:{type_}:{resolution}:{fmt(missing[0], tz)}:{fmt(end, tz)}"

            async def fetch(_etag):
                return await self._fetch_energy(home, type_, resolution, missing[0], end), None

            try:
                nodes, meta = await self.cached(key, ENERGY_FETCH_TTL, fetch)
            except UpstreamError:
                nodes, meta = [], {"fetchedAt": None, "stale": True, "staleSince": _iso(time.time())}
            closed = [r for r in nodes if parse_ts(r["to"]) <= now]
            self.db.put_energy(home["id"], type_, resolution, closed)
            fetched = {r["from"]: r for r in nodes}
        rows = [fetched.get(fmt(a, tz)) or stored.get(fmt(a, tz)) for a, _ in wanted]
        return [r for r in rows if r is not None], meta

    async def _fetch_energy(self, home, type_: str, resolution: str, start: datetime, end: datetime):
        tz = home["time_zone"]
        gql_res = RESOLUTION_GQL[resolution]
        query = CONSUMPTION_QUERY if type_ == "consumption" else PRODUCTION_QUERY
        # `after` is exclusive, so start one bucket earlier and filter.
        cursor = base64.b64encode(fmt(shift(start, resolution, tz, -1), tz).encode()).decode()
        want = len(buckets(start, end, resolution, tz)) + 2
        out: list[dict[str, Any]] = []
        for _ in range(60):
            first = min(PAGE_LIMITS[gql_res], want)
            d = await self.gql.query(
                query, {"homeId": home["gql_id"], "resolution": gql_res, "first": first, "after": cursor}
            )
            conn = ((d.get("viewer") or {}).get("home") or {}).get(type_) or {}
            nodes = [en.normalize_node(n, type_, tz) for n in conn.get("nodes") or [] if n]
            nodes = [n for n in nodes if n]
            out.extend(en.in_range(nodes, start, end))
            page = conn.get("pageInfo") or {}
            if not nodes or not page.get("hasNextPage") or not page.get("endCursor"):
                break
            if parse_ts(nodes[-1]["from"]) >= end or page["endCursor"] == cursor:
                break
            cursor = page["endCursor"]
            want -= len(nodes)
            if want <= 0:
                break
        dedup = {r["from"]: r for r in out}
        return sorted(dedup.values(), key=lambda r: parse_ts(r["from"]))

    # ------------------------------------------------------------------ devices
    def _data_home(self, home_id: str) -> tuple[Any, str]:
        home = self.home(home_id)
        if not home["data_id"]:
            raise NotConnected("data-api")
        return home, home["data_id"]

    async def devices(self, home_id: str) -> dict[str, Any]:
        home = self.home(home_id)
        if not home["data_id"] or not self.data.oauth.connected():
            return {"available": False, "devices": [], "meta": fresh_meta()}

        async def fetch(etag):
            r = await self.data.devices(home["data_id"], etag)
            return None if r.status == 304 else (r.body, r.etag)

        body, meta = await self.cached(f"data:devices:{home_id}", DEVICE_TTL, fetch)
        devices = []
        for d in body.get("devices") or []:
            if not isinstance(d, dict) or not d.get("id"):
                continue
            devices.append(self._summary(home_id, d))
        return {"available": True, "devices": devices, "meta": meta}

    def _summary(self, home_id: str, d: dict[str, Any]) -> dict[str, Any]:
        info = d.get("info") or {}
        sh = d.get("supportedHistory") or {}
        return {
            "id": self.db.device_id_for(home_id, str(d["id"])),
            "name": str(info.get("name") or "Device"),
            "brand": str(info.get("brand") or ""),
            "model": info.get("model") if isinstance(info.get("model"), str) else None,
            "historyResolutions": [
                r for r in sh.get("resolutions") or [] if r in ("quarterHour", "hour", "day", "month")
            ],
        }

    async def device(self, home_id: str, device_id: str, force: bool = False) -> dict[str, Any]:
        home, data_home = self._data_home(home_id)
        dev = self.db.get_device(home_id, device_id)
        if dev is None:
            raise DeviceNotFound()

        async def fetch(etag):
            r = await self.data.device(data_home, dev["data_id"], etag)
            return None if r.status == 304 else (r.body, r.etag)

        body, meta = await self.cached(f"data:device:{device_id}", DEVICE_TTL, fetch, force=force)
        out = self._summary(home_id, {**body, "id": dev["data_id"]})
        sh = body.get("supportedHistory") or {}
        status = body.get("status") or {}
        out.update(
            {
                "lastSeen": status.get("lastSeen") if isinstance(status.get("lastSeen"), str) else None,
                "maxRetentionDays": _int_or_none(sh.get("maxRetentionDays")),
                "attributes": [a for a in (normalize_attribute(x) for x in body.get("attributes") or []) if a],
                "capabilities": [c for c in (normalize_capability(x) for x in body.get("capabilities") or []) if c],
                "meta": meta,
            }
        )
        return out

    async def history(
        self,
        home_id: str,
        device_id: str,
        cursor: str | None,
        resolution: str | None,
        since: datetime | None,
        until: datetime | None,
    ) -> dict[str, Any]:
        home, data_home = self._data_home(home_id)
        dev = self.db.get_device(home_id, device_id)
        if dev is None:
            raise DeviceNotFound()
        if cursor:
            if resolution or since or until:
                raise BadRequest("`cursor` cannot be combined with other parameters")
            url = self.db.get_cursor(device_id, cursor)
            if url is None:
                raise BadRequest("unknown or expired cursor")
            r = await self.data.get(self.data.resolve_link(url))
        else:
            if not resolution or (since is None) == (until is None):
                raise BadRequest("give `resolution` and exactly one of `since` or `until`")
            params = {"resolution": resolution}
            if since is not None:
                params["since"] = since.isoformat()
            else:
                params["until"] = until.isoformat()
            r = await self.data.history(data_home, dev["data_id"], params)
        body = r.body or {}
        q = body.get("query") or {}
        items = [
            {"time": i["time"], "data": i.get("data") if isinstance(i.get("data"), dict) else {}}
            for i in body.get("items") or []
            if isinstance(i, dict) and isinstance(i.get("time"), str)
        ]

        def link(name):
            v = body.get(name)
            return self.db.put_cursor(device_id, v) if isinstance(v, str) and v else None

        return {
            "resolution": q.get("resolution") or resolution or "hour",
            "effectiveStart": q.get("effectiveStart"),
            "effectiveEnd": q.get("effectiveEnd"),
            "items": items,
            "next": link("next"),
            "prev": link("prev"),
        }


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _number(v: Any) -> float | int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return None
        return int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f
    return None


def normalize_capability(c: Any) -> dict[str, Any] | None:
    """Classify by shape, not by name, so unknown capabilities still render."""
    if not isinstance(c, dict) or not isinstance(c.get("id"), str):
        return None
    desc = c.get("description") if isinstance(c.get("description"), str) else None
    if isinstance(c.get("availableValues"), list):
        return {
            "id": c["id"],
            "kind": "enum",
            "description": desc,
            "value": c.get("value") if isinstance(c.get("value"), str) else None,
            "unit": None,
            "availableValues": [str(v) for v in c["availableValues"]],
        }
    num = _number(c.get("value"))
    if num is not None:
        return {
            "id": c["id"],
            "kind": "integer" if isinstance(num, int) else "number",
            "description": desc,
            "value": num,
            "unit": c.get("unit") if isinstance(c.get("unit"), str) else None,
            "availableValues": None,
        }
    value = c.get("value")
    return {
        "id": c["id"],
        "kind": "unknown",
        "description": desc,
        "value": value if isinstance(value, (str, int, float, bool)) else None,
        "unit": c.get("unit") if isinstance(c.get("unit"), str) else None,
        "availableValues": None,
    }


def normalize_attribute(a: Any) -> dict[str, Any] | None:
    """Wi-Fi SSID/BSSID/IP address are dropped: they are personal data the UI does not need."""
    if not isinstance(a, dict) or not isinstance(a.get("id"), str):
        return None
    desc = a.get("description") if isinstance(a.get("description"), str) else None
    if "status" in a and "value" not in a:
        return {
            "id": a["id"],
            "kind": "connectivity",
            "description": desc,
            "value": a["status"] if isinstance(a["status"], str) else None,
            "availableValues": None,
        }
    v = a.get("value")
    if isinstance(a.get("availableValues"), list):
        return {
            "id": a["id"],
            "kind": "enum",
            "description": desc,
            "value": v if isinstance(v, str) else None,
            "availableValues": [str(x) for x in a["availableValues"]],
        }
    if isinstance(v, bool):
        return {"id": a["id"], "kind": "boolean", "description": desc, "value": v, "availableValues": None}
    if isinstance(v, str):
        kind = "string"
        try:
            parse_ts(v)
            kind = "datetime" if "T" in v else "string"
        except ValueError:
            pass
        return {"id": a["id"], "kind": kind, "description": desc, "value": v, "availableValues": None}
    return {
        "id": a["id"],
        "kind": "unknown",
        "description": desc,
        "value": v if isinstance(v, (int, float)) else None,
        "availableValues": None,
    }
