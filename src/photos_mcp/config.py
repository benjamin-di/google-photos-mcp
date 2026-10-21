"""Settings from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Bad or missing configuration; raised at startup only."""


@dataclass(frozen=True)
class Settings:
    expected_email: str
    session_dir: Path = Path("/session")
    state_dir: Path = Path("/state")
    daily_rpc_budget: int = 3000
    daily_thumb_budget: int = 2000
    cache_max_bytes: int = 2 * 1024**3
    budget_tz: tzinfo = field(default=UTC)

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

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
            cache_max_bytes=_int(env, "PHOTOS_CACHE_MAX_BYTES", 2 * 1024**3, 0, 1024**4),
            budget_tz=_zone(env, "PHOTOS_BUDGET_TZ"),
        )


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
