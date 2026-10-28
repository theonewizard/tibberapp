"""Single-user login, session cookies, rate limiting and problem+json errors."""

from __future__ import annotations

import ipaddress
import math
import secrets
import time
from collections import OrderedDict
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .crypto import sha256_hex
from .db import Database

COOKIE_NAME = "he_session"
SESSION_TTL_S = 30 * 24 * 3600
_hasher = PasswordHasher()


# ------------------------------------------------------------------ problems (RFC 9457)

PROBLEM_TYPES = {
    400: "bad-request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not-found",
    405: "method-not-allowed",
    409: "not-connected",
    413: "too-large",
    415: "unsupported-media-type",
    422: "validation-error",
    429: "rate-limited",
    500: "internal",
    502: "upstream-unavailable",
    503: "unavailable",
}
TITLES = {
    400: "Bad request",
    401: "Sign-in required",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Tibber connection required",
    413: "Request too large",
    415: "Unsupported media type",
    422: "Invalid request",
    429: "Too many requests",
    500: "Internal error",
    502: "Tibber is not responding",
    503: "Service unavailable",
}


def problem(
    status: int,
    detail: str | None = None,
    *,
    instance: str | None = None,
    errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"/problems/{PROBLEM_TYPES.get(status, 'error')}",
        "title": TITLES.get(status, "Error"),
        "status": status,
    }
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


# ------------------------------------------------------------------ password + sessions


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


class Sessions:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self.db.put_session(sha256_hex(token), time.time() + SESSION_TTL_S)
        return token

    def valid(self, token: str | None) -> bool:
        return bool(token) and self.db.session_valid(sha256_hex(token))

    def delete(self, token: str | None) -> None:
        if token:
            self.db.delete_session(sha256_hex(token))


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_TTL_S, httponly=True, secure=True, samesite="strict", path="/"
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, httponly=True, secure=True, samesite="strict", path="/")


def require_session(request: Request) -> None:
    sessions: Sessions = request.app.state.sessions
    if not sessions.valid(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401)


# ------------------------------------------------------------------ rate limiting


class RateLimiter:
    """Fixed-window limiter per client key, exposing RateLimit-* headers.

    Login attempts use their own, much smaller budget.
    """

    def __init__(self, limit: int, window_s: int = 60, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window = window_s
        self.max_keys = max_keys
        self._hits: OrderedDict[str, tuple[int, int]] = OrderedDict()

    def hit(self, key: str, now: float | None = None) -> tuple[bool, dict[str, str]]:
        now = time.time() if now is None else now
        window_start = int(now // self.window) * self.window
        start, count = self._hits.get(key, (window_start, 0))
        if start != window_start:
            start, count = window_start, 0
        count += 1
        self._hits[key] = (start, count)
        self._hits.move_to_end(key)
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)
        reset = max(1, math.ceil(start + self.window - now))
        headers = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.limit - count)),
            "RateLimit-Reset": str(reset),
        }
        if count > self.limit:
            headers["Retry-After"] = str(reset)
            return False, headers
        return True, headers


def client_key(request: Request) -> str:
    """Caddy sits in front; only its X-Forwarded-For is trusted when the peer is loopback/private."""
    peer = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    try:
        trusted = ipaddress.ip_address(peer).is_private
    except ValueError:
        trusted = False
    if fwd and trusted:
        return fwd.split(",")[-1].strip()
    return peer
