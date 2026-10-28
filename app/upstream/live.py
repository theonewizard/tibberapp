"""Live measurements: one upstream stream per home, fanned out to every browser.

Sources, in order of preference:
1. GraphQL `liveMeasurement` over graphql-transport-ws (personal token, realTimeConsumptionEnabled).
2. Data API `live-events` SSE (OAuth with data-api-meters-read). Only one connection per home is
   allowed upstream, which is another reason to share a single stream between browsers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .gql import LIVE_SUBSCRIPTION, WS_URL_QUERY
from .http import NotConnected, UpstreamAuthError, UpstreamError

log = logging.getLogger("home_energy.live")

FIELDS = (
    "timestamp",
    "power",
    "powerProduction",
    "minPower",
    "averagePower",
    "maxPower",
    "accumulatedConsumption",
    "accumulatedProduction",
    "accumulatedConsumptionLastHour",
    "accumulatedProductionLastHour",
    "accumulatedCost",
    "accumulatedReward",
    "currency",
    "voltageL1",
    "voltageL2",
    "voltageL3",
    "currentL1",
    "currentL2",
    "currentL3",
    "powerFactor",
    "signalStrength",
)
GQL_RENAMES = {"voltagePhase1": "voltageL1", "voltagePhase2": "voltageL2", "voltagePhase3": "voltageL3"}
IDLE_GRACE_S = 30.0
HEALTHY_AFTER_S = 60.0
EVICTED_BACKOFF_S = 300.0


class StreamEnded(Exception):
    """Terminal upstream condition with an optional server-provided retry hint."""

    def __init__(self, reason: str, transient: bool, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.transient = transient
        self.retry_after = retry_after


def normalize_measurement(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        k = GQL_RENAMES.get(k, k)
        if k not in FIELDS:
            continue
        if k in ("timestamp", "currency"):
            if isinstance(v, str):
                out[k] = v
        elif isinstance(v, bool):
            continue
        elif isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            try:
                out[k] = float(v)
            except ValueError:
                pass
    return out


@dataclass
class Backoff:
    """Full-jitter exponential backoff that resets after a healthy connection."""

    base: float = 2.0
    cap: float = 300.0
    rng: Callable[[], float] = random.random
    attempt: int = 0

    def next_delay(self, hint: float | None = None) -> float:
        delay = self.rng() * min(self.cap, self.base * (2**self.attempt))
        self.attempt += 1
        if hint is not None:
            delay = max(delay, hint + self.rng() * 0.25)
        return delay

    def connected_for(self, seconds: float) -> None:
        if seconds >= HEALTHY_AFTER_S:
            self.attempt = 0


Source = Callable[[Any], Awaitable[tuple[str, AsyncIterator[dict[str, Any]]] | None]]


@dataclass
class HomeStream:
    home_id: str
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None
    last_measurement: dict[str, Any] | None = None
    last_status: dict[str, Any] = field(default_factory=lambda: {"state": "connecting", "source": None})
    idle_since: float | None = None


class LiveHub:
    def __init__(
        self,
        open_source: Source,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        backoff_factory: Callable[[], Backoff] = Backoff,
    ) -> None:
        self._open_source = open_source
        self._sleep = sleep
        self._backoff_factory = backoff_factory
        self._streams: dict[str, HomeStream] = {}
        self._closing = False

    async def subscribe(self, home: Any) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        hid = home["id"]
        stream = self._streams.get(hid)
        if stream is None:
            stream = self._streams[hid] = HomeStream(hid)
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        stream.subscribers.add(queue)
        stream.idle_since = None
        if stream.task is None or stream.task.done():
            stream.task = asyncio.create_task(self._run(stream, home), name=f"live:{hid}")
        try:
            yield "status", stream.last_status
            if stream.last_measurement:
                yield "measurement", stream.last_measurement
            while True:
                yield await queue.get()
        finally:
            stream.subscribers.discard(queue)
            if not stream.subscribers:
                stream.idle_since = time.monotonic()
                asyncio.get_running_loop().call_later(IDLE_GRACE_S, self._stop_if_idle, hid)

    def _stop_if_idle(self, hid: str) -> None:
        stream = self._streams.get(hid)
        if stream is not None and self._idle(stream):
            if stream.task is not None:
                stream.task.cancel()
            self._streams.pop(hid, None)

    def _publish(self, stream: HomeStream, event: str, data: dict[str, Any]) -> None:
        if event == "measurement":
            stream.last_measurement = data
        else:
            stream.last_status = data
        for q in list(stream.subscribers):
            if q.full():  # slow browser: drop its oldest frame rather than stall the others
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait((event, data))

    def _idle(self, stream: HomeStream) -> bool:
        return (
            not stream.subscribers
            and stream.idle_since is not None
            and time.monotonic() - stream.idle_since >= IDLE_GRACE_S
        )

    async def _run(self, stream: HomeStream, home: Any) -> None:
        try:
            await self._loop(stream, home)
        finally:
            if self._streams.get(stream.home_id) is stream and not stream.subscribers:
                self._streams.pop(stream.home_id, None)

    async def _loop(self, stream: HomeStream, home: Any) -> None:
        backoff = self._backoff_factory()
        while not self._closing and not self._idle(stream):
            hint: float | None = None
            started = time.monotonic()
            try:
                opened = await self._open_source(home)
                if opened is None:
                    self._publish(
                        stream,
                        "status",
                        {"state": "unavailable", "source": None, "reason": "no live source for this home"},
                    )
                    return
                source, frames = opened
                self._publish(stream, "status", {"state": "connected", "source": source})
                async for frame in frames:
                    self._publish(stream, "measurement", frame)
                    if self._idle(stream):
                        break
                backoff.connected_for(time.monotonic() - started)
                if self._idle(stream):
                    break
                reason = "stream closed"
            except NotConnected:
                self._publish(stream, "status", {"state": "unavailable", "source": None, "reason": "not connected"})
                return
            except StreamEnded as exc:
                backoff.connected_for(time.monotonic() - started)
                reason, hint = exc.reason, exc.retry_after
                if exc.reason == "evicted":
                    hint = max(hint or 0, EVICTED_BACKOFF_S)
                elif not exc.transient:
                    self._publish(stream, "status", {"state": "unavailable", "source": None, "reason": reason})
                    return
            except UpstreamAuthError:
                self._publish(
                    stream, "status", {"state": "unavailable", "source": None, "reason": "credentials rejected"}
                )
                return
            except (TimeoutError, UpstreamError, OSError, ValueError) as exc:
                backoff.connected_for(time.monotonic() - started)
                reason = type(exc).__name__
            delay = backoff.next_delay(hint)
            log.info("live stream for a home ended (%s); reconnecting in %.1fs", reason, delay)
            self._publish(
                stream,
                "status",
                {"state": "reconnecting", "source": None, "retryInSeconds": round(delay, 1), "reason": reason},
            )
            await self._sleep(delay)

    async def aclose(self) -> None:
        self._closing = True
        tasks = [s.task for s in self._streams.values() if s.task]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


# ---------------------------------------------------------------- concrete sources


class LiveSources:
    """Opens the best available upstream for a home."""

    def __init__(self, gql, data, db, user_agent: str) -> None:
        self.gql = gql
        self.data = data
        self.db = db
        self.user_agent = user_agent

    async def __call__(self, home: Any):
        if home["gql_id"] and home["realtime"] and self.gql.token():
            return "graphql", self._graphql(home["gql_id"])
        if home["data_id"] and self.data.oauth.connected():
            devices = await self.data.live_devices(home["data_id"])
            ids = [d["id"] for d in devices if isinstance(d, dict) and isinstance(d.get("id"), str)]
            if ids:
                return "data-api", self._data_api(home["data_id"], ids)
        return None

    async def _ws_url(self) -> str:
        cached = self.db.get_kv("gql.ws_url")
        if cached and cached.get("at", 0) > time.time() - 3600:
            return cached["url"]
        d = await self.gql.query(WS_URL_QUERY)
        url = (d.get("viewer") or {}).get("websocketSubscriptionUrl")
        if not url or not str(url).startswith("wss://"):
            raise UpstreamError("no websocket subscription URL")
        self.db.put_kv("gql.ws_url", {"url": url, "at": time.time()})
        return url

    async def _graphql(self, gql_home_id: str) -> AsyncIterator[dict[str, Any]]:
        import websockets

        token = self.gql.token()
        if not token:
            raise NotConnected("graphql")
        url = await self._ws_url()
        async with websockets.connect(
            url,
            subprotocols=["graphql-transport-ws"],
            additional_headers={"User-Agent": self.user_agent},
            open_timeout=10,
            ping_interval=30,
            max_size=2**20,
        ) as ws:
            await ws.send(json.dumps({"type": "connection_init", "payload": {"token": token}}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if ack.get("type") != "connection_ack":
                raise UpstreamAuthError("websocket connection_init rejected")
            await ws.send(
                json.dumps(
                    {
                        "id": "1",
                        "type": "subscribe",
                        "payload": {"query": LIVE_SUBSCRIPTION, "variables": {"homeId": gql_home_id}},
                    }
                )
            )
            async for raw in ws:
                async for frame in handle_gql_ws_message(json.loads(raw), ws.send):
                    yield frame

    async def _data_api(self, data_home_id: str, device_ids: list[str]) -> AsyncIterator[dict[str, Any]]:
        async for event, data in self.data.live_events(data_home_id, device_ids):
            frame = handle_sse_frame(event, data)
            if frame is not None:
                yield frame


async def handle_gql_ws_message(msg: dict[str, Any], send: Callable[[str], Awaitable[Any]]):
    t = msg.get("type")
    if t == "next":
        lm = ((msg.get("payload") or {}).get("data") or {}).get("liveMeasurement")
        if isinstance(lm, dict):
            yield normalize_measurement(lm)
        elif (msg.get("payload") or {}).get("errors"):
            raise StreamEnded("subscription error", transient=True)
    elif t == "ping":
        await send(json.dumps({"type": "pong"}))
    elif t == "error":
        raise StreamEnded("subscription error", transient=True)
    elif t == "complete":
        raise StreamEnded("subscription completed", transient=True)


def handle_sse_frame(event: str, data: str) -> dict[str, Any] | None:
    if event == "measurement":
        try:
            return normalize_measurement(json.loads(data))
        except ValueError:
            return None
    if event == "stream_error":
        try:
            err = json.loads(data)
        except ValueError:
            err = {}
        ra = err.get("retryAfter")
        raise StreamEnded(
            str(err.get("code") or "stream_error"),
            transient=bool(err.get("transient")),
            retry_after=float(ra) if isinstance(ra, (int, float)) else None,
        )
    return None  # ping and unknown events
