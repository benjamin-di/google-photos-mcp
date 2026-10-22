"""Settings from environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Bad or missing configuration; raised at startup only."""


# Path-safe by construction: lower-case letters, digits and dashes, never "." or "/" or empty.
ACCOUNT_ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}\Z")


@dataclass(frozen=True)
class Settings:
    expected_email: str
    session_dir: Path = Path("/session")
    state_dir: Path = Path("/state")
    daily_rpc_budget: int = 3000
    daily_thumb_budget: int = 2000
    daily_write_budget: int = 500  # album changes per account and day (each account's own)
    cache_max_bytes: int = 2 * 1024**3
    budget_tz: tzinfo = field(default=UTC)
    # Accounts (README "Accounts"): the default one is the snapshot in session_dir; further ones live in
    # state_dir/accounts/<id> with their snapshots in account_sessions_dir/<id> (written by the login service).
    default_account: str = "default"
    trusted_account_header: str | None = None
    account_sessions_dir: Path = Path("/sessions")
    # Sign-in links (photos_connect): the address people reach this server at, and the login service beside it.
    public_url: str | None = None
    login_url: str | None = None
    login_token: str | None = field(default=None, repr=False)

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def connect_enabled(self) -> bool:
        """Self-service sign-in links (``photos_connect``): a public address and a login service."""
        return bool(self.public_url and self.login_url and self.login_token)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        email = (env.get("GOOGLE_PHOTOS_EXPECTED_EMAIL") or "").strip().lower()
        if "@" not in email:
            raise ConfigError("GOOGLE_PHOTOS_EXPECTED_EMAIL must name the Google account to serve")
        return cls(
            expected_email=email,
            session_dir=Path((env.get("PHOTOS_SESSION_DIR") or "").strip() or "/session"),
            state_dir=Path((env.get("PHOTOS_STATE_DIR") or "").strip() or "/state"),
            daily_rpc_budget=_int(env, "PHOTOS_DAILY_RPC_BUDGET", 3000, 0, 100_000),
            daily_thumb_budget=_int(env, "PHOTOS_DAILY_THUMB_BUDGET", 2000, 0, 100_000),
            daily_write_budget=_int(env, "PHOTOS_DAILY_WRITE_BUDGET", 500, 0, 100_000),
            cache_max_bytes=_int(env, "PHOTOS_CACHE_MAX_BYTES", 2 * 1024**3, 0, 1024**4),
            budget_tz=_zone(env, "PHOTOS_BUDGET_TZ"),
            default_account=_account(env),
            trusted_account_header=_header(env),
            account_sessions_dir=Path((env.get("PHOTOS_ACCOUNT_SESSIONS_DIR") or "").strip() or "/sessions"),
            public_url=_public_url(env),
            login_url=_login_url(env),
            login_token=_login_token(env),
        )


def _account(env: Mapping[str, str]) -> str:
    raw = (env.get("PHOTOS_DEFAULT_ACCOUNT") or "").strip() or "default"
    if not ACCOUNT_ID.match(raw):
        raise ConfigError("PHOTOS_DEFAULT_ACCOUNT must be 1-32 lower-case letters, digits or dashes")
    return raw


def _header(env: Mapping[str, str]) -> str | None:
    raw = (env.get("PHOTOS_TRUSTED_ACCOUNT_HEADER") or "").strip()
    if not raw:
        return None
    if not HEADER_NAME.match(raw):
        raise ConfigError("PHOTOS_TRUSTED_ACCOUNT_HEADER must be an HTTP header name such as X-Account")
    return raw


def _login_url(env: Mapping[str, str]) -> str | None:
    """The login service runs beside this server: plain http on a private network, host and port only."""
    raw = (env.get("PHOTOS_LOGIN_URL") or "").strip().rstrip("/")
    if not raw:
        return None
    if not re.fullmatch(r"http://[a-z0-9][a-z0-9.-]{0,62}:[0-9]{2,5}", raw):
        raise ConfigError("PHOTOS_LOGIN_URL must look like http://photos-mcp-login:8080")
    return raw


def _login_token(env: Mapping[str, str]) -> str | None:
    raw = (env.get("PHOTOS_LOGIN_TOKEN") or "").strip()
    if (env.get("PHOTOS_LOGIN_URL") or "").strip() and len(raw) < 32:
        raise ConfigError("PHOTOS_LOGIN_URL needs PHOTOS_LOGIN_TOKEN (32 or more characters)")
    return raw or None


def _public_url(env: Mapping[str, str]) -> str | None:
    """The address people reach this server at (the parent of its /mcp endpoint), e.g. https://example.com/photos.
    HTTPS, or plain http on localhost for trying it out; no query or fragment."""
    raw = (env.get("PHOTOS_PUBLIC_URL") or "").strip().rstrip("/")
    if not raw:
        return None
    if not re.fullmatch(
        r"(https://[a-z0-9][a-z0-9.-]{0,252}(:[0-9]{2,5})?|http://(localhost|127\.0\.0\.1)(:[0-9]{2,5})?)"
        r"(/[A-Za-z0-9._~-]+){0,8}",
        raw,
    ):
        raise ConfigError("PHOTOS_PUBLIC_URL must look like https://example.com/photos (no query or fragment)")
    return raw


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _zone(env: Mapping[str, str], name: str) -> tzinfo:
    raw = (env.get(name) or "").strip()
    if not raw or raw.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{name} must be an IANA time zone name such as Europe/Paris") from None
