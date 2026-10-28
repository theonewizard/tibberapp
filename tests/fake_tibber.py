"""A hand-written fake of the Tibber endpoints this app calls. No real data, no real tokens."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx

TZ = "Europe/Stockholm"
GQL_HOME = "00000000-0000-4000-8000-000000000001"
DATA_HOME = GQL_HOME  # the fake keeps ids identical across both APIs
DEVICE = "dev-0001"
PAT = "fake-personal-access-token-0000000000"


def price(start: datetime, total: float, level: str | None = "NORMAL") -> dict[str, Any]:
    p = {
        "startsAt": start.isoformat(timespec="milliseconds"),
        "total": total,
        "energy": round(total * 0.8, 4),
        "tax": round(total * 0.2, 4),
        "currency": "SEK",
    }
    if level is not None:
        p["level"] = level
    return p


def day_prices(day: datetime, step_minutes: int, base: float = 1.0) -> list[dict[str, Any]]:
    """Prices for one local day, generated in UTC steps so DST days get 92/100 quarter slots."""
    zone = ZoneInfo(TZ)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end_local = (start + timedelta(days=1)).date()
    end = datetime(end_local.year, end_local.month, end_local.day, tzinfo=zone)
    out, t, i = [], start.astimezone(ZoneInfo("UTC")), 0
    while t < end:
        out.append(price(t.astimezone(zone), round(base + (i % 8) * 0.1, 4)))
        t += timedelta(minutes=step_minutes)
        i += 1
    return out


class FakeTibber:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.gql_fail = False
        self.no_contract = False
        self.prices_today: list[dict] = []
        self.prices_tomorrow: list[dict] = []
        self.energy_nodes: dict[str, list[dict]] = {"HOURLY": [], "DAILY": []}
        self.page_size_cap = 5  # force paging in tests
        self.tokens = {"access": "access-1", "refresh": "refresh-1"}
        self.token_counter = 1
        self.rotate_refresh = True
        self.refresh_invalid = False
        self.device_etag = '"v1"'
        self.history_pages = {
            "first": {
                "query": {
                    "resolution": "hour",
                    "effectiveStart": "2026-10-01T00:00:00+02:00",
                    "effectiveEnd": "2026-10-28T00:00:00+01:00",
                    "pageSize": 2,
                },
                "items": [
                    {"time": "2026-10-01T00:00:00+02:00", "data": {"energy": 1.0}},
                    {"time": "2026-10-01T01:00:00+02:00", "data": {"energy": 2.0}},
                ],
                "next": f"/v1/homes/{DATA_HOME}/devices/{DEVICE}/history?cursor=page2",
            },
            "page2": {
                "query": {
                    "resolution": "hour",
                    "effectiveStart": "2026-10-01T00:00:00+02:00",
                    "effectiveEnd": "2026-10-28T00:00:00+01:00",
                    "pageSize": 2,
                },
                "items": [{"time": "2026-10-01T02:00:00+02:00", "data": {"energy": 3.0}}],
                "prev": f"/v1/homes/{DATA_HOME}/devices/{DEVICE}/history?cursor=first-back",
            },
        }

    # ---------------------------------------------------------------- router
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url)))
        assert request.headers.get("User-Agent", "").startswith("home-energy/"), "honest User-Agent required"
        host = request.url.host
        if host == "api.tibber.com":
            return self._gql(request)
        if host == "thewall.tibber.com" and request.url.path == "/connect/token":
            return self._token(request)
        if host == "data-api.tibber.com":
            return self._data(request)
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ---------------------------------------------------------------- GraphQL
    def _gql(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {PAT}":
            return httpx.Response(
                200,
                json={
                    "errors": [{"message": "unauthenticated", "extensions": {"code": "UNAUTHENTICATED"}}],
                    "data": None,
                },
            )
        if self.gql_fail:
            return httpx.Response(503)
        body = json.loads(request.content)
        q, v = body["query"], body.get("variables") or {}
        if "query Homes" in q:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "viewer": {
                            "homes": [
                                {
                                    "id": GQL_HOME,
                                    "timeZone": TZ,
                                    "appNickname": "Test Home",
                                    "features": {"realTimeConsumptionEnabled": True},
                                }
                            ]
                        }
                    }
                },
            )
        if "query Prices" in q:
            if self.no_contract:
                return httpx.Response(
                    200, json={"data": {"viewer": {"home": {"timeZone": TZ, "currentSubscription": None}}}}
                )
            info = {
                "current": (self.prices_today or [None])[0],
                "today": self.prices_today,
                "tomorrow": self.prices_tomorrow,
            }
            return httpx.Response(
                200, json={"data": {"viewer": {"home": {"timeZone": TZ, "currentSubscription": {"priceInfo": info}}}}}
            )
        if "query Consumption" in q or "query Production" in q:
            kind = "consumption" if "Consumption" in q else "production"
            nodes = self.energy_nodes.get(v["resolution"], [])
            after = datetime.fromisoformat(base64.b64decode(v["after"]).decode()) if v.get("after") else None
            remaining = [n for n in nodes if after is None or datetime.fromisoformat(n["from"]) > after]
            first = min(v.get("first") or len(remaining), self.page_size_cap)
            page = remaining[:first]
            if kind == "production":
                page = [
                    {
                        "from": n["from"],
                        "to": n["to"],
                        "production": n["consumption"],
                        "profit": n["cost"],
                        "unitPrice": n["unitPrice"],
                        "currency": n["currency"],
                    }
                    for n in page
                ]
            end_cursor = base64.b64encode(page[-1]["from"].encode()).decode() if page else None
            return httpx.Response(
                200,
                json={
                    "data": {
                        "viewer": {
                            "home": {
                                kind: {
                                    "pageInfo": {"endCursor": end_cursor, "hasNextPage": len(remaining) > len(page)},
                                    "nodes": page,
                                }
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, json={"errors": [{"message": "unknown op"}], "data": None})

    # ---------------------------------------------------------------- OAuth token endpoint
    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form.get("client_id") == "client-id" and form.get("client_secret") == "client-secret"
        if form["grant_type"] == "authorization_code":
            assert form.get("code_verifier"), "PKCE verifier must be sent"
            if form.get("code") != "good-code":
                return httpx.Response(400, json={"error": "invalid_grant"})
        elif form["grant_type"] == "refresh_token":
            if self.refresh_invalid or form.get("refresh_token") != self.tokens["refresh"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
        self.token_counter += 1
        self.tokens["access"] = f"access-{self.token_counter}"
        body = {
            "access_token": self.tokens["access"],
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "openid offline_access data-api-user-read data-api-homes-read data-api-chargers-read",
        }
        if self.rotate_refresh or form["grant_type"] == "authorization_code":
            self.tokens["refresh"] = f"refresh-{self.token_counter}"
            body["refresh_token"] = self.tokens["refresh"]
        return httpx.Response(200, json=body)

    # ---------------------------------------------------------------- Data API
    def _data(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {self.tokens['access']}":
            return httpx.Response(401)
        path = request.url.path
        if path == "/v1/homes":
            return httpx.Response(200, json={"homes": [{"id": DATA_HOME, "name": "Test Home"}]})
        if path == f"/v1/homes/{DATA_HOME}/devices":
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {
                            "id": DEVICE,
                            "externalId": "SECRET-QR-CODE",
                            "info": {"name": "Garage charger", "brand": "Acme", "model": "X1"},
                            "supportedHistory": {
                                "resolutions": ["hour", "day", "futureResolution"],
                                "maxRetentionDays": 90,
                            },
                        }
                    ]
                },
            )
        if path == f"/v1/homes/{DATA_HOME}/devices/{DEVICE}":
            if request.headers.get("If-None-Match") == self.device_etag:
                return httpx.Response(304)
            return httpx.Response(
                200,
                headers={"ETag": self.device_etag},
                json={
                    "id": DEVICE,
                    "externalId": "SECRET-QR-CODE",
                    "info": {"name": "Garage charger", "brand": "Acme"},
                    "supportedHistory": {"resolutions": ["hour"], "maxRetentionDays": "90"},
                    "status": {"lastSeen": "2026-10-28T10:00:00+01:00"},
                    "attributes": [
                        {
                            "$type": "wifi",
                            "id": "connectivity.wifi",
                            "status": "connected",
                            "ssid": "MyWifi",
                            "bssid": "aa:bb:cc:dd:ee:ff",
                            "ipAddress": "192.0.2.10",
                        },
                        {"id": "firmware", "value": "1.2.3"},
                        {"id": "mode", "description": "Mode", "value": "eco", "availableValues": ["eco", "boost"]},
                        {"id": "something.new", "value": {"nested": True}},
                    ],
                    "capabilities": [
                        {"id": "power", "description": "Charging power", "value": 7.4, "unit": "kW"},
                        {"id": "sessions", "description": "Sessions", "value": "12", "unit": "count"},
                        {
                            "id": "state",
                            "description": "Charger state",
                            "value": "charging",
                            "availableValues": ["idle", "charging"],
                        },
                        {"id": "future", "description": "Unknown", "value": [1, 2]},
                        "not-even-an-object",
                    ],
                },
            )
        if path == f"/v1/homes/{DATA_HOME}/devices/{DEVICE}/history":
            key = request.url.params.get("cursor") or "first"
            if key not in self.history_pages:
                return httpx.Response(400)
            return httpx.Response(200, json=self.history_pages[key])
        return httpx.Response(404)
