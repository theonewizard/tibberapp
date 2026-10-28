"""Tibber GraphQL API client (prices, energy, live). Queries are validated against schema.graphql in tests."""

from __future__ import annotations

from typing import Any

from ..crypto import SecretBox
from ..db import Database
from .http import NotConnected, UpstreamAuthError, UpstreamError, UpstreamHTTP

SECRET_NAME = "gql.pat"

HOMES_QUERY = """
query Homes {
  viewer {
    homes {
      id
      timeZone
      appNickname
      features { realTimeConsumptionEnabled }
    }
  }
}
"""

PRICES_QUERY = """
query Prices($homeId: ID!, $resolution: PriceInfoResolution) {
  viewer {
    home(id: $homeId) {
      timeZone
      currentSubscription {
        priceInfo(resolution: $resolution) {
          current { total energy tax startsAt currency level }
          today { total energy tax startsAt currency level }
          tomorrow { total energy tax startsAt currency level }
        }
      }
    }
  }
}
"""

CONSUMPTION_QUERY = """
query Consumption($homeId: ID!, $resolution: EnergyResolution!, $first: Int, $after: String) {
  viewer {
    home(id: $homeId) {
      consumption(resolution: $resolution, first: $first, after: $after) {
        pageInfo { endCursor hasNextPage }
        nodes { from to consumption cost unitPrice currency }
      }
    }
  }
}
"""

PRODUCTION_QUERY = """
query Production($homeId: ID!, $resolution: EnergyResolution!, $first: Int, $after: String) {
  viewer {
    home(id: $homeId) {
      production(resolution: $resolution, first: $first, after: $after) {
        pageInfo { endCursor hasNextPage }
        nodes { from to production profit unitPrice currency }
      }
    }
  }
}
"""

WS_URL_QUERY = """
query WsUrl { viewer { websocketSubscriptionUrl } }
"""

LIVE_SUBSCRIPTION = """
subscription Live($homeId: ID!) {
  liveMeasurement(homeId: $homeId) {
    timestamp power powerProduction minPower averagePower maxPower
    minPowerProduction maxPowerProduction
    accumulatedConsumption accumulatedProduction
    accumulatedConsumptionLastHour accumulatedProductionLastHour
    accumulatedCost accumulatedReward currency
    lastMeterConsumption lastMeterProduction
    voltagePhase1 voltagePhase2 voltagePhase3
    currentL1 currentL2 currentL3
    powerFactor signalStrength
  }
}
"""

ALL_OPERATIONS = [HOMES_QUERY, PRICES_QUERY, CONSUMPTION_QUERY, PRODUCTION_QUERY, WS_URL_QUERY, LIVE_SUBSCRIPTION]

# Per-resolution `first` caps, from the schema descriptions.
PAGE_LIMITS = {"HOURLY": 744, "DAILY": 31, "WEEKLY": 52, "MONTHLY": 12, "ANNUAL": 1}


class GraphQLError(UpstreamError):
    pass


class GraphQL:
    def __init__(self, url: str, http: UpstreamHTTP, db: Database, box: SecretBox) -> None:
        self.url = url
        self.http = http
        self.db = db
        self.box = box

    # --- personal access token, encrypted at rest ---
    def token(self) -> str | None:
        raw = self.db.get_secret(SECRET_NAME)
        return self.box.open(raw) if raw else None

    def set_token(self, token: str) -> None:
        self.db.put_secret(SECRET_NAME, self.box.seal(token))

    def delete_token(self) -> None:
        self.db.delete_secret(SECRET_NAME)
        self.db.cache_clear("gql:")

    async def query(self, query: str, variables: dict[str, Any] | None = None, token: str | None = None) -> dict:
        token = token or self.token()
        if not token:
            raise NotConnected("graphql")
        resp = await self.http.request(
            "POST",
            self.url,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if resp.status_code in (401, 403):
            raise UpstreamAuthError("GraphQL API refused the token", resp.status_code)
        if resp.status_code != 200:
            raise UpstreamError("GraphQL API error", resp.status_code)
        body = resp.json()
        errors = body.get("errors") or []
        if errors:
            codes = {str((e.get("extensions") or {}).get("code", "")) for e in errors}
            if codes & {"UNAUTHENTICATED", "unauthenticated"}:
                raise UpstreamAuthError("GraphQL API refused the token", 401)
            if body.get("data") is None:
                raise GraphQLError("GraphQL error: " + (",".join(sorted(c for c in codes if c)) or "unknown"))
        return body.get("data") or {}
