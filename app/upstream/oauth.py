"""Data API OAuth 2.0: Authorization Code + PKCE (S256), refresh with rotation, encrypted storage."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from ..config import Settings
from ..crypto import SecretBox, pkce_pair
from ..db import Database
from .http import NotConnected, UpstreamAuthError, UpstreamError, UpstreamHTTP

BASELINE_SCOPES = ["openid", "profile", "email", "offline_access", "data-api-user-read", "data-api-homes-read"]
# Names and meanings from https://data-api.tibber.com/docs/scopes/ and the Data API OpenAPI document.
CATEGORY_SCOPES = {
    "data-api-vehicles-read": "Vehicles",
    "data-api-chargers-read": "EV chargers",
    "data-api-thermostats-read": "Thermostats, heat pumps, water and space heaters",
    "data-api-energy-systems-read": "Home batteries and hybrid energy systems",
    "data-api-inverters-read": "Legacy (solar) inverters",
    "data-api-meters-read": "Live data from Pulse / Watty (Data API stream)",
}
SECRET_NAME = "dataapi.tokens"
REFRESH_MARGIN_S = 60


class OAuthClient:
    def __init__(self, settings: Settings, db: Database, box: SecretBox, http: UpstreamHTTP) -> None:
        self.s = settings
        self.db = db
        self.box = box
        self.http = http
        self._refresh_lock = asyncio.Lock()

    # --- flow start / finish ---
    def authorize_url(self, extra_scopes: list[str]) -> str:
        scopes = BASELINE_SCOPES + [s for s in CATEGORY_SCOPES if s in extra_scopes]
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        self.db.put_oauth_pending(state, self.box.seal(verifier), scopes)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.s.oauth_client_id,
                "redirect_uri": self.s.oauth_redirect_uri,
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.s.oauth_authorize_url}?{query}"

    async def finish(self, state: str, code: str) -> None:
        pending = self.db.pop_oauth_pending(state)
        if pending is None:
            raise UpstreamAuthError("unknown or expired OAuth state")
        sealed_verifier, requested = pending
        verifier = self.box.open(sealed_verifier)
        if verifier is None:
            raise UpstreamAuthError("PKCE verifier could not be decrypted")
        tokens = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.s.oauth_redirect_uri,
                "code_verifier": verifier,
            }
        )
        if not tokens.get("refresh_token"):
            raise UpstreamAuthError("token response had no refresh token (offline_access missing?)")
        self._store(tokens, previous_refresh=None, requested_scopes=requested)

    # --- token use ---
    def connected(self) -> bool:
        return self._load() is not None

    def granted_scopes(self) -> list[str]:
        data = self._load()
        return data.get("scope", []) if data else []

    def disconnect(self) -> None:
        """No revocation endpoint is documented; deleting the stored tokens is all we can do."""
        self.db.delete_secret(SECRET_NAME)
        self.db.cache_clear("data:")

    async def access_token(self, force_refresh: bool = False) -> str:
        data = self._load()
        if data is None:
            raise NotConnected("data-api")
        if not force_refresh and data["expires_at"] - REFRESH_MARGIN_S > time.time():
            return data["access_token"]
        async with self._refresh_lock:
            # Another task may have refreshed while we waited; refresh tokens can be one-time use.
            current = self._load()
            if current is None:
                raise NotConnected("data-api")
            if current["refresh_token"] != data["refresh_token"] or (
                not force_refresh and current["expires_at"] - REFRESH_MARGIN_S > time.time()
            ):
                return current["access_token"]
            try:
                tokens = await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": current["refresh_token"]}
                )
            except UpstreamAuthError:
                # Refresh token expired, rotated away or client deleted: treat as revoked.
                self.disconnect()
                raise NotConnected("data-api") from None
            self._store(tokens, previous_refresh=current["refresh_token"], requested_scopes=current.get("scope", []))
            return tokens["access_token"]

    # --- internals ---
    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        form = {**form, "client_id": self.s.oauth_client_id, "client_secret": self.s.oauth_client_secret}
        resp = await self.http.request("POST", self.s.oauth_token_url, data=form)
        if resp.status_code in (400, 401, 403):
            raise UpstreamAuthError("token endpoint rejected the request", resp.status_code)
        if resp.status_code != 200:
            raise UpstreamError("token endpoint error", resp.status_code)
        body = resp.json()
        if "access_token" not in body:
            raise UpstreamError("token endpoint returned no access token")
        return body

    def _store(self, tokens: dict[str, Any], previous_refresh: str | None, requested_scopes: list[str]) -> None:
        scope = tokens.get("scope")
        data = {
            "access_token": tokens["access_token"],
            # Rotation: always keep the newest refresh token; keep the old one only if none came back.
            "refresh_token": tokens.get("refresh_token") or previous_refresh,
            "expires_at": time.time() + float(tokens.get("expires_in", 3600)),
            "scope": scope.split() if isinstance(scope, str) else requested_scopes,
        }
        self.db.put_secret(SECRET_NAME, self.box.seal(json.dumps(data)))

    def _load(self) -> dict[str, Any] | None:
        raw = self.db.get_secret(SECRET_NAME)
        if raw is None:
            return None
        plain = self.box.open(raw)
        return json.loads(plain) if plain else None
