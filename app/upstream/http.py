"""Upstream HTTP: timeouts, honest User-Agent, and retries with full jitter.

Per the Data API docs: retry only 429/500/502/503 (and transport errors), honour
Retry-After plus 0-250 ms jitter, otherwise full-jitter exponential backoff, cap attempts.
"""

from __future__ import annotations

import asyncio
import email.utils
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

RETRY_STATUSES = {429, 500, 502, 503}
TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class UpstreamError(Exception):
    """An upstream call failed. The message is safe to log; it never contains tokens or bodies."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UpstreamAuthError(UpstreamError):
    """401/403 from upstream: credentials missing, expired or lacking scope."""


class NotConnected(Exception):
    """No credentials are stored for the upstream this request needs."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def full_jitter(attempt: int, base: float = 0.4, cap: float = 15.0, rng: Callable[[], float] = random.random) -> float:
    return rng() * min(cap, base * (2**attempt))


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, when - (time.time() if now is None else now))


class UpstreamHTTP:
    def __init__(
        self,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.client = httpx.AsyncClient(
            timeout=TIMEOUT, headers={"User-Agent": user_agent}, transport=transport, follow_redirects=False
        )
        self.max_retries = max_retries
        self._sleep = sleep
        self._rng = rng

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send with retries. Returns the final response for non-retryable statuses (incl. 4xx)."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.request(method, url, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    break
                await self._sleep(full_jitter(attempt, rng=self._rng))
                continue
            if resp.status_code not in RETRY_STATUSES or attempt == self.max_retries:
                return resp
            ra = retry_after_seconds(resp.headers.get("Retry-After"))
            delay = ra + self._rng() * 0.25 if ra is not None else full_jitter(attempt, rng=self._rng)
            await resp.aclose()
            await self._sleep(min(delay, 60.0))
        raise UpstreamError(f"{method} {httpx.URL(url).host} failed: {type(last_exc).__name__}")
