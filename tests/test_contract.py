"""The code agrees with the two contracts: Tibber's saved GraphQL schema and our openapi.yaml."""

from pathlib import Path

import yaml
from fastapi.routing import APIRoute
from graphql import build_schema, parse, validate

from app.upstream.gql import ALL_OPERATIONS
from app.upstream.oauth import BASELINE_SCOPES, CATEGORY_SCOPES

ROOT = Path(__file__).resolve().parent.parent


def test_every_graphql_operation_is_valid_against_the_introspected_schema():
    schema = build_schema((ROOT / "schema.graphql").read_text())
    for op in ALL_OPERATIONS:
        errors = validate(schema, parse(op))
        assert not errors, (op.split("{")[0], errors)


def test_routes_and_openapi_paths_match(client):
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text())
    documented = {
        (m.upper(), p) for p, ops in spec["paths"].items() for m in ops if m in ("get", "post", "put", "delete")
    }
    implemented = {(m, r.path) for r in _api_routes(client.app.routes) if r.path != "/openapi.yaml" for m in r.methods}
    assert implemented == documented


def test_openapi_scope_enum_matches_code():
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text())
    enum = spec["components"]["schemas"]["OAuthStartRequest"]["properties"]["scopes"]["items"]["enum"]
    assert set(enum) == set(CATEGORY_SCOPES)
    assert "data-api-homes-read" in BASELINE_SCOPES and "offline_access" in BASELINE_SCOPES


def test_openapi_is_served(client):
    r = client.get("/openapi.yaml")
    assert r.status_code == 200 and r.text.startswith("openapi: 3.1")


def _api_routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _api_routes(r.original_router.routes)
