import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.logging_setup import redact
from app.main import create_app
from app.security import RateLimiter
from app.upstream.http import UpstreamError, UpstreamHTTP, full_jitter, retry_after_seconds
from tests.conftest import ORIGIN, PASSWORD, first_home, login, make_settings


def test_everything_under_v1_requires_a_session(client):
    for path in ("/v1/homes", "/v1/settings"):
        r = client.get(path)
        assert r.status_code == 401
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json()["type"] == "/problems/unauthenticated"


def test_session_cookie_flags_and_logout(client):
    r = client.post("/v1/session", json={"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert client.get("/v1/session").json() == {"authenticated": True}
    assert client.delete("/v1/session").status_code == 204
    assert client.get("/v1/homes").status_code == 401


def test_wrong_password_and_unknown_fields(client):
    assert client.post("/v1/session", json={"password": "nope"}).status_code == 401
    r = client.post("/v1/session", json={"password": PASSWORD, "admin": True})
    assert r.status_code == 422 and r.json()["errors"]
    assert PASSWORD not in r.text  # inputs are not echoed back


def test_login_is_rate_limited(client):
    codes = [client.post("/v1/session", json={"password": "wrong"}).status_code for _ in range(12)]
    assert codes[-1] == 429


def test_cross_origin_writes_are_refused(client):
    login(client)
    r = client.put("/v1/settings/personal-token", json={"token": "x" * 40}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.put("/v1/settings/personal-token", json={"token": "x" * 40}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.get("/v1/homes", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_non_json_and_oversized_bodies_are_refused(client):
    assert (
        client.post(
            "/v1/session", content=b"password=x", headers={"Content-Type": "application/x-www-form-urlencoded"}
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/v1/session", content=b"{" + b" " * 20000 + b"}", headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )


def test_ratelimit_headers_and_429(tmp_path, fake):
    app = create_app(make_settings(tmp_path, RATE_LIMIT_PER_MINUTE="3"), transport=fake.transport())
    with TestClient(app, base_url=ORIGIN) as c:
        r = c.get("/v1/session")
        assert r.headers["RateLimit-Limit"] == "3" and r.headers["RateLimit-Remaining"] == "2"
        c.get("/v1/session"), c.get("/v1/session")
        r = c.get("/v1/session")
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        assert r.json()["type"] == "/problems/rate-limited"
        assert c.get("/livez").status_code == 200  # probes are not limited


def test_rate_limiter_window_resets():
    rl = RateLimiter(2, window_s=60)
    assert rl.hit("a", now=0)[0] and rl.hit("a", now=1)[0] and not rl.hit("a", now=2)[0]
    assert rl.hit("a", now=61)[0]


def test_security_headers(client):
    r = client.get("/livez")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    login(client)
    assert client.get("/v1/settings").headers["cache-control"] == "no-store"


def test_upstream_errors_do_not_leak_details(connected, fake):
    fake.gql_fail = True
    connected.app.state.db.cache_clear()  # nothing cached, so the failure cannot be served stale
    r = connected.get("/v1/homes")
    assert r.status_code == 502 and r.json() == {
        "type": "/problems/upstream-unavailable",
        "title": "Tibber is not responding",
        "status": 502,
    }


def test_nothing_connected_lists_no_homes_and_prices_need_a_token(connected):
    hid = first_home(connected)
    connected.delete("/v1/settings/personal-token")
    connected.app.state.db._x("UPDATE homes SET gql_id=NULL")
    r = connected.get(f"/v1/homes/{hid}/prices")
    assert r.status_code == 409 and "personal access token" in r.json()["detail"]


def test_log_redaction(caplog):
    text = (
        "Authorization: Bearer abc.def-123 eyJhbGciOi.eyJzdWIiOi.sig refresh_token=r1&code=c2 "
        "mail me@example.com home 00000000-0000-4000-8000-000000000001 meter 735999123456789012"
    )
    out = redact(text)
    for s in ("abc.def-123", "eyJhbGciOi", "r1", "c2", "me@example.com", "00000000-0000", "735999123456789012"):
        assert s not in out


def test_logs_from_a_full_session_contain_no_secrets(tmp_path, fake, caplog):
    from tests.conftest import connect_all

    app = create_app(make_settings(tmp_path), transport=fake.transport())
    with caplog.at_level(logging.DEBUG), TestClient(app, base_url=ORIGIN) as c:
        connect_all(c)
        c.get("/v1/homes")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    for s in (
        "fake-personal-access-token",
        "access-2",
        "refresh-2",
        "good-code",
        PASSWORD,
        "client-secret",
        "00000000-0000-4000-8000-000000000001",
    ):
        assert s not in logs


# ---------------------------------------------------------------- upstream retries


def test_full_jitter_bounds():
    assert full_jitter(0, rng=lambda: 1.0) == 0.4
    assert full_jitter(10, rng=lambda: 1.0) == 15.0
    assert full_jitter(3, rng=lambda: 0.0) == 0.0


def test_retry_after_parsing():
    assert retry_after_seconds("5") == 5
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470) == 10
    assert retry_after_seconds("soon") is None and retry_after_seconds(None) is None


def _http(responses, sleeps):
    seq = iter(responses)

    def handler(request):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r

    async def sleep(d):
        sleeps.append(d)

    return UpstreamHTTP("home-energy/0.1", transport=httpx.MockTransport(handler), sleep=sleep, rng=lambda: 0.5)


def test_retries_429_with_retry_after_then_succeeds():
    sleeps = []
    http = _http([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(503), httpx.Response(200)], sleeps)
    r = asyncio.run(http.request("GET", "https://data-api.tibber.com/v1/homes"))
    assert r.status_code == 200
    assert sleeps == [2.125, 0.4]  # Retry-After + jitter, then full jitter (0.5 * 0.8)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_no_retry_on_client_errors(status):
    sleeps = []
    http = _http([httpx.Response(status), httpx.Response(200)], sleeps)
    assert asyncio.run(http.request("GET", "https://data-api.tibber.com/v1/homes")).status_code == status
    assert sleeps == []


def test_transport_errors_are_retried_then_raised():
    sleeps = []
    http = _http([httpx.ConnectError("x")] * 5, sleeps)
    with pytest.raises(UpstreamError):
        asyncio.run(http.request("GET", "https://data-api.tibber.com/v1/homes"))
    assert len(sleeps) == 4
