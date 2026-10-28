import asyncio
import json

import pytest

from app.upstream.dataapi import parse_sse
from app.upstream.http import UpstreamError
from app.upstream.live import (
    EVICTED_BACKOFF_S,
    Backoff,
    LiveHub,
    StreamEnded,
    handle_gql_ws_message,
    handle_sse_frame,
    normalize_measurement,
)

HOME = {"id": "home-opaque-0000000001", "gql_id": "g", "data_id": "d", "realtime": 1}


class Seq:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0) if self.values else 0.5


# ---------------------------------------------------------------- backoff


def test_backoff_is_exponential_full_jitter_and_capped():
    b = Backoff(base=2, cap=60, rng=lambda: 1.0)
    assert [b.next_delay() for _ in range(7)] == [2, 4, 8, 16, 32, 60, 60]
    b = Backoff(base=2, cap=60, rng=lambda: 0.25)
    assert [b.next_delay() for _ in range(3)] == [0.5, 1.0, 2.0]


def test_backoff_resets_only_after_a_healthy_connection():
    b = Backoff(base=2, cap=60, rng=lambda: 1.0)
    b.next_delay(), b.next_delay()
    b.connected_for(5)
    assert b.next_delay() == 8
    b.connected_for(120)
    assert b.next_delay() == 2


def test_backoff_respects_server_retry_hint():
    b = Backoff(base=2, cap=60, rng=lambda: 0.0)
    assert b.next_delay(hint=30) == 30


# ---------------------------------------------------------------- hub


def _run_hub(open_source, n_events, sleeps):
    async def sleep(d):
        sleeps.append(d)
        await asyncio.sleep(0)  # record the delay but let the subscriber run

    async def main():
        hub = LiveHub(open_source, sleep=sleep, backoff_factory=lambda: Backoff(base=2, cap=60, rng=lambda: 1.0))
        got = []
        agen = hub.subscribe(HOME)
        async for ev in agen:
            got.append(ev)
            if len(got) >= n_events:
                break
        await agen.aclose()
        await hub.aclose()
        return got

    return asyncio.run(asyncio.wait_for(main(), 5))


def test_hub_reconnects_with_backoff_after_failures():
    attempts = []

    async def frames():
        yield {"power": 100.0}

    async def open_source(home):
        attempts.append(1)
        if len(attempts) <= 3:
            raise UpstreamError("boom")
        return "graphql", frames()

    sleeps = []
    got = _run_hub(open_source, 6, sleeps)
    states = [d["state"] for e, d in got if e == "status"]
    assert states[:4] == ["connecting", "reconnecting", "reconnecting", "reconnecting"]
    assert sleeps[:3] == [2, 4, 8]
    assert ("measurement", {"power": 100.0}) in got


def test_hub_backs_off_long_when_evicted():
    async def open_source(home):
        async def frames():
            raise StreamEnded("evicted", transient=False)
            yield  # pragma: no cover

        return "data-api", frames()

    sleeps = []
    got = _run_hub(open_source, 3, sleeps)
    assert sleeps and sleeps[0] >= EVICTED_BACKOFF_S
    assert got[-1][1]["state"] == "reconnecting"


def test_hub_gives_up_on_non_transient_errors():
    async def open_source(home):
        async def frames():
            raise StreamEnded("device_not_streamable", transient=False)
            yield  # pragma: no cover

        return "data-api", frames()

    sleeps = []
    got = _run_hub(open_source, 3, sleeps)
    assert got[-1] == ("status", {"state": "unavailable", "source": None, "reason": "device_not_streamable"})
    assert sleeps == []


def test_one_upstream_per_home_is_shared_between_subscribers():
    opened = []

    async def main():
        release = asyncio.Event()

        async def frames():
            await release.wait()
            yield {"power": 1.0}
            await asyncio.Event().wait()

        async def open_source(home):
            opened.append(1)
            return "graphql", frames()

        hub = LiveHub(open_source)
        a, b = hub.subscribe(HOME), hub.subscribe(HOME)
        assert (await a.__anext__())[0] == "status"
        assert (await b.__anext__())[0] == "status"
        await asyncio.sleep(0)
        release.set()
        seen = []
        for gen in (a, b):
            while True:
                ev = await gen.__anext__()
                if ev[0] == "measurement":
                    seen.append(ev[1])
                    break
        await a.aclose()
        await b.aclose()
        await hub.aclose()
        return seen

    assert asyncio.run(asyncio.wait_for(main(), 5)) == [{"power": 1.0}, {"power": 1.0}]
    assert len(opened) == 1


# ---------------------------------------------------------------- protocol handling


def test_graphql_ws_messages():
    sent = []

    async def send(m):
        sent.append(json.loads(m))

    async def collect(msg):
        return [f async for f in handle_gql_ws_message(msg, send)]

    frame = asyncio.run(
        collect(
            {
                "type": "next",
                "id": "1",
                "payload": {
                    "data": {
                        "liveMeasurement": {
                            "timestamp": "2026-10-28T10:00:00+01:00",
                            "power": 1200,
                            "voltagePhase1": 231.0,
                            "currentL1": 5.2,
                            "lastMeterConsumption": 12345.6,
                            "unknownField": 1,
                        }
                    }
                },
            }
        )
    )
    assert frame == [{"timestamp": "2026-10-28T10:00:00+01:00", "power": 1200, "voltageL1": 231.0, "currentL1": 5.2}]
    asyncio.run(collect({"type": "ping"}))
    assert sent == [{"type": "pong"}]
    with pytest.raises(StreamEnded):
        asyncio.run(collect({"type": "complete", "id": "1"}))


def test_data_api_sse_frames():
    async def lines():
        for line in [
            ": comment",
            "event: ping",
            "data: ",
            "",
            "event: measurement",
            'data: {"deviceId":"x","power":"275.5","voltageL2":230}',
            "",
            "event: stream_error",
            'data: {"code":"upstream_unavailable","transient":true,"retryAfter":7}',
            "",
        ]:
            yield line

    async def collect():
        return [f async for f in parse_sse(lines())]

    frames = asyncio.run(collect())
    assert [e for e, _ in frames] == ["ping", "measurement", "stream_error"]
    assert handle_sse_frame(*frames[0]) is None
    assert handle_sse_frame(*frames[1]) == {"power": 275.5, "voltageL2": 230}
    with pytest.raises(StreamEnded) as ei:
        handle_sse_frame(*frames[2])
    assert ei.value.transient and ei.value.retry_after == 7


def test_normalize_measurement_ignores_garbage():
    assert normalize_measurement({"power": True, "minPower": "n/a", "currency": 3, "maxPower": 9}) == {"maxPower": 9}
