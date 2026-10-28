"""REST routes. Contract: openapi.yaml (tests check that every route here is in it)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..security import (
    COOKIE_NAME,
    clear_session_cookie,
    require_session,
    set_session_cookie,
    verify_password,
)
from ..services.timeutil import parse_ts
from ..upstream.gql import HOMES_QUERY
from ..upstream.http import UpstreamAuthError
from ..upstream.oauth import BASELINE_SCOPES, CATEGORY_SCOPES

ID_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"
HomeId = Annotated[str, Path(pattern=ID_PATTERN)]
DeviceId = Annotated[str, Path(pattern=ID_PATTERN)]
Timestamp = Annotated[str, Query(max_length=40, pattern=r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(Z|[+-]\d{2}:\d{2})$")]
CategoryScope = Literal[
    "data-api-vehicles-read",
    "data-api-chargers-read",
    "data-api-thermostats-read",
    "data-api-energy-systems-read",
    "data-api-inverters-read",
    "data-api-meters-read",
]


def reject_unknown_query(request: Request) -> None:
    """Unknown query parameters are an error, not silently ignored."""
    route = request.scope.get("route")
    allowed = {p.alias for p in route.dependant.query_params} if route is not None else set()
    for dep in getattr(route.dependant, "dependencies", []) if route is not None else []:
        allowed |= {p.alias for p in dep.query_params}
    unknown = sorted(set(request.query_params.keys()) - allowed)
    if unknown:
        raise HTTPException(status_code=422, detail="unknown query parameter: " + ", ".join(unknown[:5]))
    if any(len(request.query_params.getlist(k)) > 1 for k in request.query_params.keys()):
        raise HTTPException(status_code=422, detail="repeated query parameter")


def ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_ts(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="timestamp must be RFC 3339 with an offset") from None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(Strict):
    password: str = Field(min_length=1, max_length=1024)


class PersonalTokenRequest(Strict):
    token: str = Field(min_length=20, max_length=512, pattern=r"^[A-Za-z0-9_.-]+$")


class OAuthStartRequest(Strict):
    scopes: list[CategoryScope] = Field(max_length=16)


public = APIRouter(dependencies=[Depends(reject_unknown_query)])
api = APIRouter(dependencies=[Depends(reject_unknown_query), Depends(require_session)])


# ------------------------------------------------------------------ ops


@public.get("/livez")
async def livez():
    return {"status": "ok"}


@public.get("/readyz")
async def readyz(request: Request):
    try:
        ok = request.app.state.db.ping()
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        raise HTTPException(status_code=503, detail="database not reachable")
    return {"status": "ok"}


# ------------------------------------------------------------------ session


@public.get("/v1/session")
async def get_session(request: Request):
    return {"authenticated": request.app.state.sessions.valid(request.cookies.get(COOKIE_NAME))}


@public.post("/v1/session", status_code=204)
async def create_session(body: LoginRequest, request: Request, response: Response):
    ok, headers = request.app.state.login_limiter.hit(request.state.client_key)
    if not ok:
        raise HTTPException(status_code=429, detail="too many sign-in attempts", headers=headers)
    stored = request.app.state.settings.app_password_hash
    if not await asyncio.to_thread(verify_password, stored, body.password):
        raise HTTPException(status_code=401, detail="wrong password")
    set_session_cookie(response, request.app.state.sessions.create())
    response.status_code = 204
    return response


@api.delete("/v1/session", status_code=204)
async def delete_session(request: Request, response: Response):
    request.app.state.sessions.delete(request.cookies.get(COOKIE_NAME))
    clear_session_cookie(response)
    response.status_code = 204
    return response


# ------------------------------------------------------------------ settings


@api.get("/v1/settings")
async def get_settings(request: Request):
    st = request.app.state
    granted = set(st.oauth.granted_scopes())
    scopes = [{"name": s, "label": s, "required": True, "granted": s in granted} for s in BASELINE_SCOPES]
    scopes += [
        {"name": s, "label": label, "required": False, "granted": s in granted} for s, label in CATEGORY_SCOPES.items()
    ]
    return {
        "dataApiConnected": st.oauth.connected(),
        "oauthConfigured": st.settings.oauth_configured,
        "personalTokenSet": st.gql.token() is not None,
        "scopes": scopes,
    }


@api.put("/v1/settings/personal-token", status_code=204)
async def put_personal_token(body: PersonalTokenRequest, request: Request):
    gql = request.app.state.gql
    try:
        await gql.query(HOMES_QUERY, token=body.token)
    except UpstreamAuthError:
        raise HTTPException(status_code=422, detail="Tibber rejected this token") from None
    gql.set_token(body.token)
    request.app.state.db.cache_clear("gql:")
    return Response(status_code=204)


@api.delete("/v1/settings/personal-token", status_code=204)
async def delete_personal_token(request: Request):
    request.app.state.gql.delete_token()
    return Response(status_code=204)


@api.post("/v1/settings/oauth")
async def start_oauth(body: OAuthStartRequest, request: Request):
    st = request.app.state
    if not st.settings.oauth_configured:
        raise HTTPException(
            status_code=409, detail="OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET and OAUTH_REDIRECT_URI must be set"
        )
    return {"authorizeUrl": st.oauth.authorize_url(list(body.scopes))}


@api.delete("/v1/settings/oauth", status_code=204)
async def disconnect_oauth(request: Request):
    request.app.state.oauth.disconnect()
    return Response(status_code=204)


CALLBACK_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="0;url=/#/settings?oauth={result}">
<title>Home Energy</title></head><body><p><a href="/#/settings?oauth={result}">Continue</a></p></body></html>"""


@public.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    request: Request,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state: Annotated[str | None, Query(max_length=256)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
    error_description: Annotated[str | None, Query(max_length=1024)] = None,
    scope: Annotated[str | None, Query(max_length=2048)] = None,
    session_state: Annotated[str | None, Query(max_length=1024)] = None,
    iss: Annotated[str | None, Query(max_length=1024)] = None,
):
    # Authenticated by the single-use `state` minted for a signed-in session. The session cookie is
    # SameSite=Strict and therefore absent on this cross-site redirect; the page below re-navigates
    # same-site so the cookie is sent again.
    result = "error"
    if code and state and not error:
        try:
            await request.app.state.oauth.finish(state, code)
            request.app.state.db.cache_clear("data:")
            result = "ok"
        except Exception:  # noqa: BLE001 - logged without details, shown generically
            request.app.state.log.warning("OAuth callback failed")
    return HTMLResponse(CALLBACK_PAGE.format(result=result), headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ homes / prices / energy


@api.get("/v1/homes")
async def list_homes(request: Request):
    homes, meta = await request.app.state.service.homes()
    return {"homes": homes, "meta": meta}


@api.get("/v1/homes/{id}/prices")
async def get_prices(request: Request, id: HomeId, resolution: Literal["quarter_hourly", "hourly"] = "quarter_hourly"):
    return await request.app.state.service.prices(id, resolution)


@api.get("/v1/homes/{id}/prices/cheapest-window")
async def get_cheapest_window(
    request: Request,
    id: HomeId,
    duration: Annotated[int, Query(ge=15, le=1440, multiple_of=15)],
    from_: Annotated[Timestamp | None, Query(alias="from")] = None,
    to: Timestamp | None = None,
):
    return await request.app.state.service.cheapest(id, duration, ts(from_), ts(to))


@api.get("/v1/homes/{id}/energy")
async def get_energy(
    request: Request,
    id: HomeId,
    type: Literal["consumption", "production"],
    resolution: Literal["hourly", "daily", "weekly", "monthly", "annual"],
    from_: Annotated[Timestamp, Query(alias="from")],
    to: Timestamp,
):
    return await request.app.state.service.energy(id, type, resolution, ts(from_), ts(to))


# ------------------------------------------------------------------ devices


@api.get("/v1/homes/{id}/devices")
async def list_devices(request: Request, id: HomeId):
    return await request.app.state.service.devices(id)


@api.get("/v1/homes/{id}/devices/{deviceId}")
async def get_device(request: Request, id: HomeId, deviceId: DeviceId):
    return await request.app.state.service.device(id, deviceId)


@api.get("/v1/homes/{id}/devices/{deviceId}/history")
async def get_device_history(
    request: Request,
    id: HomeId,
    deviceId: DeviceId,
    cursor: Annotated[str | None, Query(pattern=ID_PATTERN)] = None,
    resolution: Literal["quarterHour", "hour", "day", "month"] | None = None,
    since: Timestamp | None = None,
    until: Timestamp | None = None,
):
    return await request.app.state.service.history(id, deviceId, cursor, resolution, ts(since), ts(until))


# ------------------------------------------------------------------ live

PING_INTERVAL_S = 20.0


@api.get("/v1/homes/{id}/live")
async def stream_live(request: Request, id: HomeId):
    home = request.app.state.service.home(id)
    hub = request.app.state.live

    async def events():
        q: asyncio.Queue = asyncio.Queue()

        async def pump():
            async for item in hub.subscribe(home):
                await q.put(item)

        task = asyncio.create_task(pump())
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=PING_INTERVAL_S)
                except TimeoutError:
                    yield "event: ping\ndata: \n\n"
                    continue
                yield f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
                if event == "status" and data.get("state") == "unavailable":
                    break
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
