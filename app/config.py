"""Configuration, read from the environment only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_USER_AGENT = "home-energy/0.1"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    token_enc_key: str
    oauth_client_id: str
    oauth_client_secret: str
    oauth_redirect_uri: str
    tz: str
    db_path: str
    bind: str
    user_agent: str
    app_password_hash: str
    public_origin: str
    rate_limit_per_minute: int
    data_api_base: str = "https://data-api.tibber.com"
    gql_url: str = "https://api.tibber.com/v1-beta/gql"
    oauth_authorize_url: str = "https://thewall.tibber.com/connect/authorize"
    oauth_token_url: str = "https://thewall.tibber.com/connect/token"

    @property
    def oauth_configured(self) -> bool:
        return bool(self.oauth_client_id and self.oauth_client_secret and self.oauth_redirect_uri)

    @property
    def bind_host(self) -> str:
        host, _, _ = self.bind.rpartition(":")
        return host.strip("[]") or "127.0.0.1"

    @property
    def bind_port(self) -> int:
        return int(self.bind.rpartition(":")[2])


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    redirect = env.get("OAUTH_REDIRECT_URI", "")
    settings = Settings(
        token_enc_key=env.get("TOKEN_ENC_KEY", ""),
        oauth_client_id=env.get("OAUTH_CLIENT_ID", ""),
        oauth_client_secret=env.get("OAUTH_CLIENT_SECRET", ""),
        oauth_redirect_uri=redirect,
        tz=env.get("TZ", "Europe/Stockholm"),
        db_path=env.get("DB_PATH", "./data/home-energy.sqlite3"),
        bind=env.get("BIND", "127.0.0.1:8080"),
        user_agent=env.get("USER_AGENT", DEFAULT_USER_AGENT),
        app_password_hash=env.get("APP_PASSWORD_HASH", ""),
        public_origin=env.get("PUBLIC_ORIGIN", "") or _origin_of(redirect),
        rate_limit_per_minute=int(env.get("RATE_LIMIT_PER_MINUTE", "120")),
    )
    validate(settings)
    return settings


def validate(s: Settings) -> None:
    from cryptography.fernet import Fernet

    if not s.token_enc_key:
        raise ConfigError("TOKEN_ENC_KEY is required")
    try:
        Fernet(s.token_enc_key.encode())
    except Exception as exc:  # noqa: BLE001 - never echo the key
        raise ConfigError("TOKEN_ENC_KEY must be a Fernet key (32 url-safe base64 bytes)") from exc
    if not s.app_password_hash.startswith("$argon2"):
        raise ConfigError("APP_PASSWORD_HASH must be an argon2 hash (see README)")
    if s.oauth_redirect_uri and urlsplit(s.oauth_redirect_uri).scheme not in ("https", "http"):
        raise ConfigError("OAUTH_REDIRECT_URI must be an absolute http(s) URL")
    try:
        if not 0 < s.bind_port < 65536:
            raise ValueError
    except ValueError as exc:
        raise ConfigError("BIND must be host:port") from exc
    if s.rate_limit_per_minute < 1:
        raise ConfigError("RATE_LIMIT_PER_MINUTE must be positive")
