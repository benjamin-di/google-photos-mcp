"""Where the login service keeps each account: its browser profile (``PHOTOS_LOGIN_PROFILES/<id>``, only here) and
its snapshot (``PHOTOS_LOGIN_SESSIONS/<id>``, which photos-mcp reads as ``PHOTOS_ACCOUNT_SESSIONS_DIR/<id>``), with
``keeper.json`` beside the snapshot: what the browser last saw.

keeper.json: ``{"state": "signed_in" | "signed_out" | "error", "email", "checked_at", "generation", "error"}``.
``generation`` is the newest snapshot generation written for the account, kept even when the snapshot is withdrawn,
so the next one counts on from it (photos-mcp never re-uses a generation Google rejected).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from photos_mcp.accounts import KEEPER_FILE, valid_id

SIGNED_IN, SIGNED_OUT, ERROR = "signed_in", "signed_out", "error"


@dataclass(frozen=True)
class Paths:
    profiles: Path
    sessions: Path

    def profile(self, account: str) -> Path:
        if not valid_id(account):
            raise ValueError("bad account id")
        return self.profiles / account

    def session(self, account: str) -> Path:
        if not valid_id(account):
            raise ValueError("bad account id")
        return self.sessions / account

    def keeper(self, account: str) -> dict[str, Any] | None:
        try:
            data = json.loads((self.session(account) / KEEPER_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def generation(self, account: str) -> int:
        data = self.keeper(account) or {}
        value = data.get("generation")
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    def record(
        self,
        account: str,
        state: str,
        *,
        email: str | None = None,
        generation: int | None = None,
        error: str | None = None,
        clock=time.time,
    ) -> None:
        """Write keeper.json (atomically, 0600); fields not given keep their previous value."""
        previous = self.keeper(account) or {}
        data = {
            "state": state,
            "email": email if email is not None else previous.get("email"),
            "checked_at": int(clock()),
            "generation": max(generation or 0, self.generation(account)),
            "error": error,
        }
        folder = self.session(account)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{KEEPER_FILE}.", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, folder / KEEPER_FILE)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def kept(self) -> list[str]:
        """The accounts the keeper refreshes: a profile and a keeper.json with an address that is not signed out."""
        if not self.sessions.is_dir():
            return []
        out = []
        for folder in sorted(self.sessions.iterdir()):
            if not valid_id(folder.name) or not self.profile(folder.name).is_dir():
                continue
            data = self.keeper(folder.name) or {}
            if data.get("state") in (SIGNED_IN, ERROR) and isinstance(data.get("email"), str):
                out.append(folder.name)
        return out

    def wipe_profile(self, account: str) -> None:
        shutil.rmtree(self.profile(account), ignore_errors=True)

    def forget(self, account: str) -> None:
        """Everything kept for an account: its browser profile (its Google sign-in) and its snapshot."""
        self.wipe_profile(account)
        shutil.rmtree(self.session(account), ignore_errors=True)
