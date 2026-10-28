import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from app.crypto import SecretBox, pkce_pair
from app.db import Database
from app.upstream.http import NotConnected, UpstreamHTTP
from app.upstream.oauth import SECRET_NAME, OAuthClient
from tests.conftest import connect_all, login, make_settings
from tests.fake_tibber import FakeTibber


def test_pkce_s256_pair():
    import base64
    import hashlib

    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_has_pkce_state_and_baseline_scopes(client):
    login(client)
    url = client.post("/v1/settings/oauth", json={"scopes": ["data-api-vehicles-read"]}).json()["authorizeUrl"]
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://thewall.tibber.com/connect/authorize"
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://energy.example/oauth/callback"
    assert q["scope"].split() == [
        "openid",
        "profile",
        "email",
        "offline_access",
        "data-api-user-read",
        "data-api-homes-read",
        "data-api-vehicles-read",
    ]
    assert len(q["state"]) >= 32 and "client_secret" not in q


def test_unknown_scope_is_rejected(client):
    login(client)
    r = client.post("/v1/settings/oauth", json={"scopes": ["data-api-everything"]})
    assert r.status_code == 422


def test_callback_rejects_unknown_state_and_is_single_use(client, fake):
    login(client)
    assert "oauth=error" in client.get("/oauth/callback", params={"code": "good-code", "state": "nope"}).text
    url = client.post("/v1/settings/oauth", json={"scopes": []}).json()["authorizeUrl"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert "oauth=ok" in client.get("/oauth/callback", params={"code": "good-code", "state": state}).text
    assert "oauth=error" in client.get("/oauth/callback", params={"code": "good-code", "state": state}).text


def test_tokens_are_encrypted_at_rest_and_never_returned(connected, fake):
    db = connected.app.state.db
    raw = db.get_secret(SECRET_NAME)
    assert b"refresh-" not in raw and b"access-" not in raw
    pat = db.get_secret("gql.pat")
    assert b"fake-personal" not in pat
    body = connected.get("/v1/settings").text
    assert "refresh-" not in body and "access-" not in body and "fake-personal" not in body
    s = connected.get("/v1/settings").json()
    assert s["dataApiConnected"] and s["personalTokenSet"]
    assert {x["name"] for x in s["scopes"] if x["granted"]} >= {"data-api-chargers-read"}


def test_disconnect_and_token_removal(connected):
    assert connected.delete("/v1/settings/oauth").status_code == 204
    assert connected.delete("/v1/settings/personal-token").status_code == 204
    s = connected.get("/v1/settings").json()
    assert not s["dataApiConnected"] and not s["personalTokenSet"]
    db = connected.app.state.db
    assert db.get_secret(SECRET_NAME) is None and db.get_secret("gql.pat") is None


def test_bad_personal_token_is_not_stored(client):
    login(client)
    r = client.put("/v1/settings/personal-token", json={"token": "x" * 40})
    assert r.status_code == 422
    assert client.app.state.db.get_secret("gql.pat") is None


# ---------------------------------------------------------------- refresh with rotation


@pytest.fixture
def oauth_env(tmp_path):
    fake = FakeTibber()
    settings = make_settings(tmp_path)
    db = Database(":memory:")
    box = SecretBox(settings.token_enc_key)
    http = UpstreamHTTP("home-energy/0.1 (tests)", transport=fake.transport())
    client = OAuthClient(settings, db, box, http)
    # Pretend the code flow already ran: store an expired access token and refresh-1.
    db.put_secret(
        SECRET_NAME,
        box.seal(
            json.dumps(
                {"access_token": "access-1", "refresh_token": "refresh-1", "expires_at": time.time() - 10, "scope": []}
            )
        ),
    )
    return fake, client, db, box


def _stored(db, box):
    return json.loads(box.open(db.get_secret(SECRET_NAME)))


def test_refresh_rotates_and_persists_new_refresh_token(oauth_env):
    fake, client, db, box = oauth_env
    token = asyncio.run(client.access_token())
    assert token == "access-2"
    assert _stored(db, box)["refresh_token"] == "refresh-2"
    # Second refresh must use the rotated token; the old one is now invalid upstream.
    assert asyncio.run(client.access_token(force_refresh=True)) == "access-3"
    assert _stored(db, box)["refresh_token"] == "refresh-3"


def test_refresh_keeps_old_refresh_token_when_none_returned(oauth_env):
    fake, client, db, box = oauth_env
    fake.rotate_refresh = False
    asyncio.run(client.access_token())
    assert _stored(db, box)["refresh_token"] == "refresh-1"


def test_concurrent_requests_refresh_once(oauth_env):
    fake, client, db, box = oauth_env

    async def many():
        return await asyncio.gather(*(client.access_token() for _ in range(5)))

    tokens = asyncio.run(many())
    assert set(tokens) == {"access-2"}
    assert sum(1 for m, u in fake.calls if u.endswith("/connect/token")) == 1


def test_invalid_refresh_token_disconnects(oauth_env):
    fake, client, db, box = oauth_env
    fake.refresh_invalid = True
    with pytest.raises(NotConnected):
        asyncio.run(client.access_token())
    assert db.get_secret(SECRET_NAME) is None


def test_401_from_data_api_triggers_one_refresh(connected, fake):
    fake.tokens["access"] = "rotated-server-side"  # stored access token is now rejected
    fake.tokens["refresh"] = fake.tokens["refresh"]
    connected.app.state.db.cache_clear()
    r = connected.get("/v1/homes")
    assert r.status_code == 200 and r.json()["homes"][0]["sources"]["devices"] is True


def test_full_flow_via_api(client, fake):
    connect_all(client)
    assert client.get("/v1/settings").json()["dataApiConnected"] is True
