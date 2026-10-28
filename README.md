# Home Energy

A self-hosted, mobile-first web app for Tibber customers. It shows prices, live power,
consumption and connected devices. It uses **only Tibber's official, documented APIs**:

| API | Used for | Auth |
| --- | --- | --- |
| [Tibber GraphQL API](https://developer.tibber.com/api/reference.md) (`api.tibber.com/v1-beta/gql`) | homes, prices, consumption/production, live `liveMeasurement` | your personal access token |
| [Tibber Data API](https://data-api.tibber.com/docs/) (`data-api.tibber.com/v1`) | devices, device history, live-events stream | OAuth 2.0 Authorization Code + PKCE (S256) |

The app never calls undocumented endpoints, never imitates the Tibber app, and never reuses
tokens from it. You enter your credentials at runtime, in the app.

## Quick start (Docker / TrueNAS)

```bash
cp .env.example .env
```

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put that value in `TOKEN_ENC_KEY`. Then create the sign-in password hash and paste the printed
line into `.env`. Keep the single quotes, because the hash contains `$` signs.

```bash
python -m app.hash_password
```

```bash
docker compose up -d --build
```

Put Caddy in front (see `Caddyfile.example`). Caddy terminates TLS and adds basic auth as a
second gate. Compose publishes on `127.0.0.1:8080` by default; set `PUBLISH_ADDR`/`PUBLISH_PORT`
to change it, and `DATA_DIR` for the SQLite volume (e.g. a TrueNAS dataset path).

Local development:

```bash
python3.12 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
```

```bash
set -a && . ./.env && set +a && python -m app
```

## 1. Register the OAuth client (Data API)

1. Sign in at <https://data-api.tibber.com/clients/manage> with your Tibber account.
2. Create a client:
   * **Redirect URI**: `https://<your host>/oauth/callback`. It must match `OAUTH_REDIRECT_URI`
     **exactly** (scheme, host, path, no trailing slash). For local development,
     `http://localhost:8080/oauth/callback` is accepted by Tibber.
   * **Scopes**: tick the categories you want to use (see below).
3. Copy the client id and the secret into `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET`. The secret
   is shown only once.

Tibber requires the client secret for the token exchange even when PKCE is used ("public flows
without a secret are not supported"). That is why the app runs the whole flow on the server,
and why `OAUTH_CLIENT_SECRET` is needed in addition to the variables in the original brief.
Tokens are encrypted with `TOKEN_ENC_KEY` and never reach the browser. Refresh tokens rotate:
the newest one is always stored.

Then open **Settings → Connect Tibber**.

## 2. Personal access token (GraphQL API)

Create one at <https://developer.tibber.com/settings/access-token> and paste it on
**Settings**. The server checks it against the API, stores it encrypted, and never returns it.
**Remove token** deletes it.

## 3. Which scopes to choose

Always requested: `openid profile email offline_access data-api-user-read data-api-homes-read`.
Then choose the device categories you want (names from the [Scopes page](https://data-api.tibber.com/docs/scopes/)):

| Scope | Gives |
| --- | --- |
| `data-api-vehicles-read` | electric vehicles (shown under every home) |
| `data-api-chargers-read` | EV chargers |
| `data-api-thermostats-read` | thermostats, heat pumps, water and space heaters |
| `data-api-energy-systems-read` | home batteries and hybrid energy systems |
| `data-api-inverters-read` | legacy (solar) inverters |
| `data-api-meters-read` | Pulse/Watty live stream through the Data API (only needed without a personal token) |

A device appears only if its category scope was granted. Adding scopes later means pressing
**Reconnect** (Tibber shows a consent screen for new scopes only).

## 4. What each screen shows

| Screen | Content | Source |
| --- | --- | --- |
| **Home** | Home picker, current price and level, today/tomorrow price chart (15-minute), live power, energy and cost so far today, one tile per device with its first capability | GraphQL `priceInfo`, `liveMeasurement` (or hourly `consumption` when no Pulse), Data API devices |
| **Live** | Power, average/min/max today, consumption, production, cost and reward today, per-phase voltage and current when the meter reports them, 10-minute power chart | GraphQL `liveMeasurement`, or Data API `live-events` |
| **Prices** | Current price, today and tomorrow at 15-minute or hourly resolution with price levels, min/max/average, cheapest-window finder ("cheapest 2 h tonight"). Tomorrow's prices are polled for after ~13:00 CET, not spun on | GraphQL `priceInfo` |
| **Analysis** | Consumption or production per day/week/month/year with cost, compared with the previous period, plus *price performance*: the average price you paid (Σcost / ΣkWh over hourly data) against the plain average hourly price. Negative means you used more power in cheap hours | GraphQL `consumption` / `production` |
| **Devices** | Every device's state (capabilities with units and allowed values), attributes, last seen, and history charts with cursor paging | Data API devices and history |
| **Settings** | Connect/disconnect the Data API, choose scopes, paste/remove the personal token, sign out, and the "not available" list | — |

What the official APIs don't support, and how the app handles it:

* **No price contract**: `currentSubscription` is null, and the app shows "No price data for
  this account".
* **No device category**: the Data API does not say whether a device is a car, charger,
  battery or thermostat. Every device gets the same generic view. Labels come from Tibber's own
  capability descriptions and units, so state of charge, range and charging state show up where
  the vehicle integration reports them. Unknown capabilities and attributes still render.
* **Live stream**: GraphQL is used when a personal token is stored and
  `realTimeConsumptionEnabled` is true. Otherwise the app uses the Data API `live-events`
  stream, which allows only one connection per home. Either way the server keeps one upstream
  connection per home and shares it with every open browser. It reconnects with
  exponential backoff and full jitter, waiting at least 5 minutes after an `evicted` error.
* **Privacy**: Wi-Fi SSID/BSSID/IP and device external ids (often a QR code) are never sent to
  the browser. Upstream home and device ids are swapped for opaque ids on the server.

## 5. Not available through the official APIs

| Item | Why |
| --- | --- |
| Battery mode (the app's SetBatteryMode), charger and thermostat changes, away mode | The official APIs are read-only for devices. |
| Checklist, offer cards, articles, store link, messages, invites | App-only content; no public API. |
| Power-up catalogue, instalment summary | App-only content; no public API. |
| Notification categories, weather widget, feature flags | App-only content; no public API. |
| Push notifications | The official `sendPushNotification` only sends to the Tibber app; we don't build our own push on it. |
| Comparison with other households | That data is not public. |
| Revoking tokens at Tibber | No revocation endpoint is documented. **Disconnect** deletes the stored tokens; to revoke fully, delete the client at data-api.tibber.com/clients/manage. |

## Refresh policy

| Data | Refresh |
| --- | --- |
| Prices | Cached until the next hour, never past local midnight. While tomorrow is missing: at ~13:00 CET, then every 15 minutes |
| Device state | Every 5 minutes or when a screen opens, revalidated with `If-None-Match` when Tibber sends an ETag |
| Consumption/production | Cached for an hour; closed periods are kept in SQLite for good and never fetched again |
| Homes | Hourly |
| Live data | One upstream stream per home, fanned out to browsers over SSE |

All caching is on the server and shared between browser sessions. If Tibber does not answer,
the last good data is served and the UI shows "data stale since …".

## API

`openapi.yaml` (OpenAPI 3.1) is the contract and is served at `/openapi.yaml`. The UI uses
only this API, so another client (PWA, native) can be added later without backend changes.
Main routes:

```
GET  /v1/homes
GET  /v1/homes/{id}/prices?resolution=quarter_hourly|hourly
GET  /v1/homes/{id}/prices/cheapest-window?duration=&from=&to=
GET  /v1/homes/{id}/energy?type=consumption|production&resolution=&from=&to=
GET  /v1/homes/{id}/devices
GET  /v1/homes/{id}/devices/{deviceId}
GET  /v1/homes/{id}/devices/{deviceId}/history?cursor=  (or resolution + since|until)
GET  /v1/homes/{id}/live            (text/event-stream)
GET  /livez   GET /readyz
```

plus `/v1/session` (sign in/out), `/v1/settings…` and `/oauth/callback`.

## Security (OWASP API Top 10, 2023)

* **Authentication**: single-user password (argon2id) and an HttpOnly, Secure, SameSite=Strict
  session cookie, with Caddy basic auth in front. Sign-in attempts are limited to 10 a minute.
* **Object-level authorisation**: a home or device id exists only after this server listed it
  from Tibber. Anything else is 404. Upstream ids are never accepted or returned.
* **Input validation**: every body and query parameter is validated. Unknown fields and unknown
  or repeated query parameters are rejected with 422. Bodies are JSON only, at most 16 KB.
* **Errors**: RFC 9457 `application/problem+json`, with no upstream details.
* **Rate limiting**: per client, with `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`
  (and `Retry-After` on 429).
* **Same-origin**: no CORS headers. Cross-origin writes are refused using the `Origin` and
  `Sec-Fetch-Site` headers. A strict CSP is set (no inline script or style).
* **Logging**: access logs record route templates only (no ids, no query strings), and a
  redaction filter removes bearer tokens, JWTs, OAuth parameters, emails, UUIDs and 18-digit
  meter ids.
* **Upstream calls**: timeouts everywhere. Retries happen only on 429/500/502/503 and transport
  errors, with full jitter, honouring `Retry-After`. The User-Agent is honest (`USER_AGENT`).

## Operations

* `/livez` and `/readyz` probes, and a Docker `HEALTHCHECK`.
* The container runs as uid 10001 with a read-only root filesystem and all capabilities
  dropped. Data lives in the `/data` volume.
* Graceful shutdown: on SIGTERM the server stops accepting connections, drains for up to 10
  seconds, closes the live streams and closes the database.
* OpenTelemetry tracing with W3C `traceparent`. Spans are exported when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Trace ids appear in every log line.

## Configuration (environment only)

| Variable | Meaning |
| --- | --- |
| `TOKEN_ENC_KEY` | Fernet key that encrypts upstream tokens at rest (required) |
| `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI` | Data API OAuth client |
| `APP_PASSWORD_HASH` | argon2 hash of the sign-in password (required) |
| `TZ` | Fallback time zone for homes the GraphQL API doesn't describe |
| `DB_PATH` | SQLite file |
| `BIND` | `host:port` to listen on |
| `USER_AGENT` | Sent to Tibber, default `home-energy/0.1` |
| `PUBLIC_ORIGIN` | Optional; defaults to the origin of `OAUTH_REDIRECT_URI` |
| `RATE_LIMIT_PER_MINUTE` | Optional, default 120 |
| `FORWARDED_ALLOW_IPS` | Proxies trusted for `X-Forwarded-*` |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | Optional tracing export |

## Tests and CI

```bash
pytest -q
```

The tests use a hand-written fake of the Tibber endpoints (`tests/fake_tibber.py`): no live
tokens and no real data. They cover:

* price levels and price performance
* DST days (23 and 25 hours, and 92/100 quarter-hour slots)
* GraphQL and Data API cursor paging
* token refresh with rotation, single-flight refresh, and refresh on 401
* live reconnect backoff, the eviction rule and stream sharing
* the security controls above
* every GraphQL operation is validated against the introspected `schema.graphql`
* every route is checked against `openapi.yaml`

CI (`.github/workflows/ci.yml`) runs:

* ruff and the tests
* Spectral lint of `openapi.yaml`, and an oasdiff breaking-change diff against the base branch
* pip-audit (CVE scan) and gitleaks (secret scan of the full history)
* a Docker build

`schema.graphql` is the live GraphQL schema, introspected from `api.tibber.com/v1-beta/gql`.
