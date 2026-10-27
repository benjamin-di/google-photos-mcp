"""Application account bindings over shared optional OIDC browser authentication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from signin_window.oidc import BrowserAuth, OIDCConfig
from signin_window.oidc import BrowserStore as SessionStore
from signin_window.state import atomic_private_write
from starlette.responses import Response

from .accounts import valid_id
from .config import ConfigError


@dataclass(frozen=True)
class ConnectionsConfig:
    issuer: str
    client_id: str
    public_url: str
    bindings: Mapping[str, str]
    required_group: str
    state_dir: Path
    allow_loopback_http: bool = False
    account_claim: str | None = None

    def __post_init__(self):
        for value in (self.issuer, self.public_url):
            parts = urlsplit(value)
            local = self.allow_loopback_http and parts.hostname in ("127.0.0.1", "localhost")
            if (parts.scheme != "https" and not (local and parts.scheme == "http")) or (
                not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            ):
                raise ConfigError("Connections URLs must be fixed HTTPS addresses")
        if not self.client_id or not self.required_group or (not self.bindings and not self.account_claim):
            raise ConfigError("Connections requires an OIDC client, allowed group and immutable subject bindings")
        if any(not isinstance(sub, str) or not sub or not valid_id(account) for sub, account in self.bindings.items()):
            raise ConfigError("Connections bindings must map immutable OIDC subjects to configured account ids")
        if self.account_claim and (
            self.bindings or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", self.account_claim)
        ):
            raise ConfigError("Choose an explicit subject map or one administrator-controlled signed account claim")
        if len(set(self.bindings.values())) != len(self.bindings):
            raise ConfigError("A Photos account may have only one connections owner")

    @classmethod
    def from_env(cls, public_url: str | None, state_dir: Path, env=None):
        env = (
            {
                name: os.environ[name]
                for name in (
                    "PHOTOS_CONNECTIONS_PUBLIC_URL",
                    "PHOTOS_CONNECTIONS_BINDINGS_FILE",
                    "PHOTOS_CONNECTIONS_ISSUER",
                    "PHOTOS_CONNECTIONS_CLIENT_ID",
                    "PHOTOS_CONNECTIONS_GROUP",
                    "PHOTOS_CONNECTIONS_ACCOUNT_CLAIM",
                )
                if name in os.environ
            }
            if env is None
            else env
        )
        try:
            bindings = (
                json.loads(Path(env["PHOTOS_CONNECTIONS_BINDINGS_FILE"]).read_text())
                if env.get("PHOTOS_CONNECTIONS_BINDINGS_FILE")
                else {}
            )
            if not isinstance(bindings, dict):
                raise ValueError("bindings")
            return cls(
                env["PHOTOS_CONNECTIONS_ISSUER"].rstrip("/"),
                env["PHOTOS_CONNECTIONS_CLIENT_ID"],
                (env.get("PHOTOS_CONNECTIONS_PUBLIC_URL") or public_url or "").rstrip("/"),
                bindings,
                env["PHOTOS_CONNECTIONS_GROUP"],
                state_dir,
                account_claim=env.get("PHOTOS_CONNECTIONS_ACCOUNT_CLAIM") or None,
            )
        except (KeyError, ValueError, OSError):
            raise ConfigError("Configure Photos connections OIDC and its private subject/account bindings") from None


class BrowserStore(SessionStore):
    """Retain existing durable account controls independently of browser configuration."""

    def __init__(self, folder: Path, *, clock=time.time, track_location=True, require_existing=None):
        marker = folder / "controls-initialized"
        location = folder.parent / (folder.name + "-controls-initialized")
        markers = (marker, location) if track_location else (marker,)
        required = (
            os.environ.get("CONNECTIONS_REQUIRE_CONTROLS", "0") != "0" if require_existing is None else require_existing
        )
        if required and not (folder / "browser.sqlite3").is_file():
            raise ConfigError("The durable connection controls are missing or invalid")
        if any(m.is_symlink() or (m.exists() and not (folder / "browser.sqlite3").is_file()) for m in markers):
            raise ConfigError("The durable connection controls are missing or invalid")
        try:
            super().__init__(folder, clock=clock)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        with self.db() as db:
            if any(item.exists() for item in markers):
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"controls", "owners"} <= tables:
                    raise ConfigError("The durable connection tables are missing")
            db.execute("CREATE TABLE IF NOT EXISTS controls (account TEXT PRIMARY KEY, epoch TEXT, suspended INT)")
            db.execute("CREATE TABLE IF NOT EXISTS owners (account TEXT PRIMARY KEY, issuer TEXT, subject TEXT)")
            for row in db.execute("SELECT account FROM owners"):
                self.mark_account(row[0])
        for item in markers:
            if not item.exists():
                atomic_private_write(item, b"1\n")

    def account_marker(self, account):
        key = hashlib.sha256(account.encode()).hexdigest()[:24]
        return self.path.parent / ("account-" + key + "-initialized")

    def adopted(self, account):
        marker = self.account_marker(account)
        return marker.exists() or marker.is_symlink()

    def mark_account(self, account):
        if not self.adopted(account):
            atomic_private_write(self.account_marker(account), b"1\n")

    def owner(self, account):
        with self.db() as db:
            owner = db.execute("SELECT issuer,subject FROM owners WHERE account=?", (account,)).fetchone()
        if owner is None and self.adopted(account):
            raise ConfigError("The durable account owner is missing")
        return owner

    def claim_owner(self, account, issuer, subject):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.adopted(account) and not db.execute("SELECT 1 FROM owners WHERE account=?", (account,)).fetchone():
                raise ConfigError("The durable account owner is missing")
            if (
                db.execute("SELECT 1 FROM owners WHERE account=?", (account,)).fetchone()
                and not db.execute("SELECT 1 FROM controls WHERE account=?", (account,)).fetchone()
            ):
                raise ConfigError("The durable account control is missing")
            db.execute("INSERT OR IGNORE INTO controls VALUES (?,?,?)", (account, "initial", 0))
            db.execute("INSERT OR IGNORE INTO owners VALUES (?,?,?)", (account, issuer, subject))
            owner = db.execute("SELECT issuer,subject FROM owners WHERE account=?", (account,)).fetchone()
            self.mark_account(account)
        return owner == (issuer, subject)

    def control(self, account):
        with self.db() as db:
            row = db.execute("SELECT epoch,suspended FROM controls WHERE account=?", (account,)).fetchone()
            if row is None and (
                self.adopted(account) or db.execute("SELECT 1 FROM owners WHERE account=?", (account,)).fetchone()
            ):
                raise ConfigError("The durable account control is missing")
            if self.adopted(account) and not db.execute("SELECT 1 FROM owners WHERE account=?", (account,)).fetchone():
                raise ConfigError("The durable account owner is missing")
        return row or ("initial", 0)

    def set_control(self, account, suspended):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO controls VALUES (?,?,?)", (account, secrets.token_urlsafe(18), suspended)
            )


class BrowserLogin(BrowserAuth):
    def __init__(self, config: ConnectionsConfig, *, clock=time.time, transport=None):
        self.bindings_config = config
        controls = BrowserStore(config.state_dir / "connections", clock=clock)
        super().__init__(
            OIDCConfig(
                issuer=config.issuer,
                client_id=config.client_id,
                public_url=config.public_url,
                state_dir=config.state_dir / "connections",
                cookie_name="__Host-photos-connections",
                required_group=config.required_group,
                claim_names=(config.account_claim,) if config.account_claim else (),
                allow_loopback_http=config.allow_loopback_http,
            ),
            clock=clock,
            transport=transport,
        )
        self.store = controls

    def mapped(self, data):
        config = self.bindings_config
        sub = data.get("sub")
        account = config.bindings.get(sub) if config.bindings else data.get("claims", {}).get(config.account_claim)
        if not valid_id(account):
            return None
        owner = self.store.owner(account)
        if owner is not None and owner != (data.get("issuer"), sub):
            return None
        return {
            "issuer": data.get("issuer"),
            "sub": sub,
            "name": data.get("name") or "Your account",
            "account": account,
        }

    def identity(self, request):
        data = super().identity(request)
        return self.mapped(data) if data else None

    async def callback(self, request):
        response = await super().callback(request)
        if response.status_code == 303 and self.mapped(request.session) is None:
            request.session.clear()
            return Response("Sign-in could not be verified. Return to connections and try again.", status_code=403)
        return response


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Initialize reviewed connection controls once")
    parser.add_argument("--initialize", action="store_true", required=True)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    BrowserStore(args.folder, require_existing=False)
