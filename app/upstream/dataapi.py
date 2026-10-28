"""Tibber Data API client (REST, read-only). https://data-api.tibber.com/docs/"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .http import UpstreamAuthError, UpstreamError, UpstreamHTTP
from .oauth import OAuthClient


@dataclass
class DataResponse:
    status: int  # 200 or 304
    body: Any
    etag: str | None


class NotFound(UpstreamError):
    pass


class DataAPI:
    def __init__(self, base: str, http: UpstreamHTTP, oauth: OAuthClient) -> None:
        self.base = base.rstrip("/")
        self.http = http
        self.oauth = oauth

    def _url(self, *segments: str) -> str:
        return self.base + "/v1/" + "/".join(quote(s, safe="") for s in segments)

    def same_origin(self, url: str) -> bool:
        return urlsplit(url).netloc == urlsplit(self.base).netloc

    async def get(self, url: str, params: dict[str, Any] | None = None, etag: str | None = None) -> DataResponse:
        if not self.same_origin(url):
            raise UpstreamError("refusing to follow a link to another host")
        for attempt in (0, 1):
            token = await self.oauth.access_token(force_refresh=attempt == 1)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if etag:
                headers["If-None-Match"] = etag
            resp = await self.http.request("GET", url, params=params, headers=headers)
            if resp.status_code == 401 and attempt == 0:
                continue  # access token expired early: refresh once, then retry
            break
        if resp.status_code == 304:
            return DataResponse(304, None, etag)
        if resp.status_code in (401, 403):
            raise UpstreamAuthError("Data API refused the token", resp.status_code)
        if resp.status_code == 404:
            raise NotFound("Data API: not found", 404)
        if resp.status_code != 200:
            raise UpstreamError("Data API error", resp.status_code)
        return DataResponse(200, resp.json(), resp.headers.get("ETag"))

    async def homes(self, etag: str | None = None) -> DataResponse:
        return await self.get(self._url("homes"), etag=etag)

    async def devices(self, home: str, etag: str | None = None) -> DataResponse:
        return await self.get(self._url("homes", home, "devices"), etag=etag)

    async def device(self, home: str, device: str, etag: str | None = None) -> DataResponse:
        return await self.get(self._url("homes", home, "devices", device), etag=etag)

    async def history(self, home: str, device: str, params: dict[str, str]) -> DataResponse:
        return await self.get(self._url("homes", home, "devices", device, "history"), params=params)

    def resolve_link(self, link: str) -> str:
        """`next`/`prev` links may be relative or absolute; only same-host links are followed."""
        url = str(httpx.URL(self.base + "/").join(link))
        if not self.same_origin(url):
            raise UpstreamError("refusing to follow a link to another host")
        return url

    async def live_devices(self, home: str) -> list[dict[str, Any]]:
        return (await self.get(self._url("homes", home, "live-events", "devices"))).body.get("devices", [])

    async def live_events(self, home: str, device_ids: list[str]) -> AsyncIterator[tuple[str, str]]:
        """Yield (event, data) SSE frames. Raises UpstreamError on non-200."""
        token = await self.oauth.access_token()
        url = self._url("homes", home, "live-events")
        params = [("deviceId", d) for d in device_ids[:5]]
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        timeout = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)  # pings every ~20 s
        async with self.http.client.stream("GET", url, params=params, headers=headers, timeout=timeout) as resp:
            if resp.status_code in (401, 403):
                raise UpstreamAuthError("Data API refused the live stream", resp.status_code)
            if resp.status_code != 200:
                raise UpstreamError("Data API live stream error", resp.status_code)
            async for frame in parse_sse(resp.aiter_lines()):
                yield frame


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    event, data = "message", []
    async for line in lines:
        if line == "":
            if data or event != "message":
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
