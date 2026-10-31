"""The keeper: every hour, each signed-in account's browser profile opens Google Photos once and writes a fresh
snapshot, so Google's rotating cookies stay current (README "Signing in with a link").

Only the browser decides that a session ended (as for the CLI exporter), and only on evidence seen twice in a row,
10 minutes apart: Google's sign-in page, its signed-out page, or a request to prove it is them again. Then the
snapshot is withdrawn (photos-mcp answers not_signed_in, and the person gets a new link). Another Google account in
the profile withdraws it at once. Anything else (an account notice, a slow page, a network error) keeps the
snapshot and tries again after 10 minutes. The keeper never signs in: it holds no password and types nothing.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from signin_window.sessions import refresh_due

from photos_mcp.session_export import ExportError, unpublish

from . import window
from .profiles import ERROR, SIGNED_IN, SIGNED_OUT, Paths

log = logging.getLogger("photos-login")

EVERY = 3600  # seconds between refreshes of a signed-in account
RETRY = 600  # after a failed refresh
REQUEST_GAP = 300  # an early refresh (photos-mcp saw Google end a generation) at most this often
POLL = 60
LOCK_WAIT = 5  # a refresh waits this long for a sign-in window's browser, then tries next round
DISPLAY = ":80"
SIGNED_OUT_CODES = frozenset({"signed_out", "challenge"})  # withdrawn when seen on two refreshes in a row
ENDED_AT_ONCE = frozenset({"account_mismatch"})  # another Google account: the profile no longer serves this one

# (profile, session_dir, env, email, min_generation) -> export result
Refresher = Callable[..., dict[str, Any]]


class Keeper:
    def __init__(
        self,
        paths: Paths,
        browser_lock: threading.Lock,
        *,
        every: int = EVERY,
        retry: int = RETRY,
        xvfb: str = "Xvfb",
        refresher: Refresher | None = None,
        clock: Callable[[], float] = time.time,
        deployment=None,
    ) -> None:
        self.deployment = deployment
        self.paths, self.browser_lock = paths, browser_lock
        self.every, self.retry = every, retry
        self.xvfb = xvfb
        self._refresher = refresher or self._refresh
        self._clock = clock
        self._requested: set[str] = set()
        self._lock = threading.Lock()

    def request(self, account: str) -> bool:
        """Refresh this account soon (not before REQUEST_GAP since its last look). False when it is not kept."""
        if account not in self.paths.kept():
            return False
        with self._lock:
            self._requested.add(account)
        return True

    def due(self, account: str) -> bool:
        data = self.paths.keeper(account) or {}
        checked = data.get("checked_at")
        if not isinstance(checked, int | float):
            return True
        with self._lock:
            requested = account in self._requested
        return refresh_due(
            checked,
            now=self._clock(),
            interval=self.retry if data.get("state") == ERROR else self.every,
            requested=requested,
            request_gap=REQUEST_GAP,
        )

    def round(self) -> None:
        for account in self.paths.kept():
            if self.due(account):
                self.refresh_one(account)

    def refresh_one(self, account: str) -> str | None:
        from photos_mcp.deployment import Busy

        if self.deployment is None:
            return self._refresh_one(account)
        try:
            with self.deployment.work():
                return self._refresh_one(account)
        except Busy:
            return None

    def _refresh_one(self, account: str) -> str | None:
        """One refresh; returns the keeper state written, or None when the browser was busy (next round)."""
        data = self.paths.keeper(account) or {}
        email = data.get("email")
        if not isinstance(email, str):
            return None
        if not self.browser_lock.acquire(timeout=LOCK_WAIT):
            return None
        try:
            with self._lock:
                self._requested.discard(account)
            result = self._refresher(
                profile=self.paths.profile(account),
                session_dir=self.paths.session(account),
                email=email,
                min_generation=self.paths.generation(account),
            )
        except ExportError as exc:
            seen_before = data.get("state") == ERROR and data.get("error") in SIGNED_OUT_CODES
            if exc.code in ENDED_AT_ONCE or (exc.code in SIGNED_OUT_CODES and seen_before):
                unpublish(self.paths.session(account))
                self.paths.record(account, SIGNED_OUT, error=exc.code, clock=self._clock)
                log.warning("keeper: account %s is signed out (%s); its snapshot is withdrawn", account, exc.code)
                return SIGNED_OUT
            self.paths.record(account, ERROR, error=exc.code, clock=self._clock)
            log.warning("keeper: account %s not refreshed (%s)", account, exc.code)
            return ERROR
        except Exception as exc:  # the loop must survive anything
            self.paths.record(account, ERROR, error="failed", clock=self._clock)
            log.error("keeper: account %s crashed (%s)", account, type(exc).__name__)
            return ERROR
        finally:
            self.browser_lock.release()
        self.paths.record(account, SIGNED_IN, generation=int(result["generation"]), clock=self._clock)
        log.info("keeper: account %s refreshed (generation %s)", account, result["generation"])
        return SIGNED_IN

    def _refresh(self, *, profile, session_dir, email, min_generation) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        width, height = window.LAYOUTS["desktop"]
        display = subprocess.Popen(
            [self.xvfb, DISPLAY, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.8)
            if display.poll() is not None:
                raise ExportError("the virtual display did not start", "failed")
            return window.refresh(
                profile=profile,
                session_dir=session_dir,
                env={**os.environ, "DISPLAY": DISPLAY},
                email=email,
                min_generation=min_generation,
            )
        finally:
            display.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                display.wait(timeout=5)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.round()
            except Exception as exc:  # listing the accounts failed; try again next round
                log.error("keeper round crashed: %s", type(exc).__name__)
            stop.wait(POLL)
