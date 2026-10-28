from __future__ import annotations

import warnings
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security import hash_password  # noqa: E402
from tests.fake_tibber import PAT, FakeTibber  # noqa: E402

ORIGIN = "https://energy.example"
PASSWORD = "correct horse battery staple"
_HASH = hash_password(PASSWORD)


def make_settings(tmp_path, **overrides):
    env = {
        "TOKEN_ENC_KEY": Fernet.generate_key().decode(),
        "APP_PASSWORD_HASH": _HASH,
        "DB_PATH": str(tmp_path / "test.sqlite3"),
        "OAUTH_CLIENT_ID": "client-id",
        "OAUTH_CLIENT_SECRET": "client-secret",
        "OAUTH_REDIRECT_URI": f"{ORIGIN}/oauth/callback",
        "TZ": "Europe/Stockholm",
        "BIND": "127.0.0.1:8080",
        "USER_AGENT": "home-energy/0.1 (tests)",
        "RATE_LIMIT_PER_MINUTE": "1000",
    }
    env.update(overrides)
    return load_settings(env)


@pytest.fixture
def fake():
    return FakeTibber()


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze the service clock: frozen_now(datetime) sets it."""
    import app.services.core as core

    def set_now(dt: datetime):
        monkeypatch.setattr(core, "now_utc", lambda: dt)

    return set_now


@pytest.fixture
def client(tmp_path, fake):
    app = create_app(make_settings(tmp_path), transport=fake.transport())
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def login(c: TestClient) -> None:
    r = c.post("/v1/session", json={"password": PASSWORD})
    assert r.status_code == 204, r.text


def connect_all(c: TestClient) -> None:
    """Sign in, paste the personal token and run the OAuth flow against the fake."""
    login(c)
    assert c.put("/v1/settings/personal-token", json={"token": PAT}).status_code == 204
    r = c.post("/v1/settings/oauth", json={"scopes": ["data-api-chargers-read"]})
    assert r.status_code == 200, r.text
    state = parse_qs(urlsplit(r.json()["authorizeUrl"]).query)["state"][0]
    cb = c.get("/oauth/callback", params={"code": "good-code", "state": state})
    assert "oauth=ok" in cb.text


@pytest.fixture
def connected(client):
    connect_all(client)
    return client


def first_home(c: TestClient) -> str:
    homes = c.get("/v1/homes").json()["homes"]
    assert homes, "fake should expose one home"
    return homes[0]["id"]
