"""FastAPI application: wiring, middleware, error mapping, static UI."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import api, public
from .config import Settings, load_settings
from .crypto import SecretBox
from .db import Database
from .logging_setup import setup_logging
from .security import RateLimiter, Sessions, client_key, problem
from .services.core import BadRequest, DeviceNotFound, HomeNotFound, Service
from .telemetry import setup_telemetry
from .upstream.dataapi import DataAPI, NotFound
from .upstream.gql import GraphQL
from .upstream.http import NotConnected, UpstreamAuthError, UpstreamError, UpstreamHTTP
from .upstream.live import LiveHub, LiveSources
from .upstream.oauth import OAuthClient

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
OPENAPI_FILE = ROOT / "openapi.yaml"
MAX_BODY_BYTES = 16 * 1024
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
CONNECTION_NAMES = {"graphql": "Tibber personal access token", "data-api": "Tibber Data API (OAuth)"}


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None, live_source=None
) -> FastAPI:
    settings = settings or load_settings()
    log = setup_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        box = SecretBox(settings.token_enc_key)
        http = UpstreamHTTP(settings.user_agent, transport=transport)
        oauth = OAuthClient(settings, db, box, http)
        gql = GraphQL(settings.gql_url, http, db, box)
        data = DataAPI(settings.data_api_base, http, oauth)
        st = app.state
        st.settings, st.db, st.oauth, st.gql, st.data, st.log = settings, db, oauth, gql, data, log
        st.service = Service(settings, db, gql, data)
        st.sessions = Sessions(db)
        st.live = LiveHub(live_source or LiveSources(gql, data, db, settings.user_agent))
        log.info("started")
        try:
            yield
        finally:
            log.info("shutting down")
            await st.live.aclose()
            await http.aclose()
            db.close()

    app = FastAPI(title="Home Energy", lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.login_limiter = RateLimiter(10)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        start = time.perf_counter()
        path = request.url.path
        request.state.client_key = client_key(request)
        rl_headers: dict[str, str] = {}
        if path not in ("/livez", "/readyz"):
            ok, rl_headers = app.state.limiter.hit(request.state.client_key)
            if not ok:
                return _finish(problem(429, "rate limit exceeded", headers=rl_headers), request, start, log)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Same-origin only: no CORS headers are ever sent, and cross-site writes are refused.
            origin = request.headers.get("origin")
            expected = {settings.public_origin} if settings.public_origin else set()
            expected.add(f"{request.url.scheme}://{request.headers.get('host', '')}")
            if origin is not None and origin not in expected:
                return _finish(problem(403, "cross-origin request refused"), request, start, log)
            if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
                return _finish(problem(403, "cross-site request refused"), request, start, log)
            length = request.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
                return _finish(problem(413), request, start, log)
            if length not in (None, "0") and not request.headers.get("content-type", "").startswith("application/json"):
                return _finish(problem(415, "send application/json"), request, start, log)
        response = await call_next(request)
        response.headers.update(rl_headers)
        return _finish(response, request, start, log)

    # ---------------------------------------------------------------- errors → problem+json
    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) and exc.status_code < 500 else None
        if exc.status_code == 404 and detail == "Not Found":
            detail = None
        return problem(exc.status_code, detail, headers=getattr(exc, "headers", None))

    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def starlette_exc(request: Request, exc: StarletteHTTPException):
        return problem(exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exc(request: Request, exc: RequestValidationError):
        errors = [
            {"location": ".".join(str(p) for p in e.get("loc", ())), "message": str(e.get("msg", "invalid"))}
            for e in exc.errors()[:20]
        ]
        return problem(422, "request does not match the API schema", errors=errors)

    @app.exception_handler(NotConnected)
    async def not_connected(request: Request, exc: NotConnected):
        return problem(409, f"Connect {CONNECTION_NAMES.get(exc.what, 'Tibber')} in Settings")

    @app.exception_handler(UpstreamAuthError)
    async def upstream_auth(request: Request, exc: UpstreamAuthError):
        return problem(409, "Tibber rejected the stored credentials or scopes; reconnect in Settings")

    @app.exception_handler(HomeNotFound)
    async def home_nf(request: Request, exc: HomeNotFound):
        return problem(404, "unknown home")

    @app.exception_handler(DeviceNotFound)
    async def device_nf(request: Request, exc: DeviceNotFound):
        return problem(404, "unknown device")

    @app.exception_handler(NotFound)
    async def upstream_nf(request: Request, exc: NotFound):
        return problem(404)

    @app.exception_handler(BadRequest)
    async def bad_request(request: Request, exc: BadRequest):
        return problem(422, str(exc))

    @app.exception_handler(UpstreamError)
    async def upstream_err(request: Request, exc: UpstreamError):
        if exc.status == 400:
            return problem(422, "Tibber rejected the request parameters")
        log.warning("upstream error: %s (status %s)", exc, exc.status)
        return problem(502)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.error("unhandled %s on %s", type(exc).__name__, _route_template(request))
        return problem(500)

    app.include_router(public)
    app.include_router(api)

    @app.get("/openapi.yaml", include_in_schema=False)
    async def openapi_yaml():
        return FileResponse(OPENAPI_FILE, media_type="application/yaml")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    setup_telemetry(app)
    return app


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or ("/static" if not request.url.path.startswith("/v1") else "unmatched")


def _finish(response, request: Request, start: float, log: logging.Logger):
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    path = request.url.path
    if path.startswith(("/v1", "/oauth")):
        h.setdefault("Cache-Control", "no-store")
    # Access log: route template only (no ids, no query strings, never tokens or codes).
    log.info(
        "%s %s %s %.0fms",
        request.method,
        _route_template(request),
        response.status_code,
        (time.perf_counter() - start) * 1000,
    )
    return response


def run() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.bind_port,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        timeout_graceful_shutdown=10,
        log_config=None,
    )
