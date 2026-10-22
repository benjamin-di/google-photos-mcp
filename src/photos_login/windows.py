"""Sign-in windows: one virtual display, one remote-desktop server on loopback and the account's own browser profile.

States: ``starting`` (checking whether the profile is still signed in) -> ``waiting`` (Google's sign-in page is up;
the person is signing in; the only state with a live view) -> ``saving`` (signed in; the snapshot is being written) ->
``connected``; or ``wrong_account`` / ``in_use`` (signed in as a Google account this account may not use: the
profile is wiped), ``empty_library``, ``busy`` (the browser stayed in use), ``failed`` (also when the profile showed
something nobody may watch, such as a signed-in account's notice), ``expired`` or ``cancelled``. A cancel once the
window is ``saving`` changes nothing: the sign-in is finished and kept. Ended windows stay readable for
``ENDED_TTL`` so photos-mcp sees how they ended.

Only one browser runs at a time (``browser_lock``, shared with the keeper): a window waits for a refresh to finish.
Each window has its own view token, which only the caller that started it gets.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from photos_mcp.session_export import ExportError, unpublish

from . import window
from .profiles import ERROR, SIGNED_IN, SIGNED_OUT, Paths

log = logging.getLogger("photos-login")

ENDED_TTL = 5 * 60
LOCK_WAIT = 90  # seconds a window waits for the keeper's refresh to finish
ACTIVE = ("starting", "waiting", "saving")
VIEWABLE = "waiting"  # the only state the live view works in: Google's sign-in page is shown


class Busy(Exception):
    """Every window slot is in use."""


@dataclass
class Login:
    id: str
    account: str
    layout: str
    slot: int
    created: float
    deadline: float
    state: str = "starting"
    host: str = ""
    email: str | None = None
    error: str | None = None
    ended_at: float | None = None
    procs: list[subprocess.Popen] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    view_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    @property
    def display(self) -> str:
        return f":{90 + self.slot}"

    @property
    def vnc_port(self) -> int:
        return 5990 + self.slot

    def public(self) -> dict[str, Any]:
        out = {"id": self.id, "account": self.account, "state": self.state, "host": self.host, "layout": self.layout}
        if self.state == "connected":
            out["email"] = self.email
        return out

    def view_ok(self, token: str) -> bool:
        return secrets.compare_digest(token or "", self.view_token)


# (login, email, taken, min_generation, on_state) -> export result
Runner = Callable[..., dict[str, Any]]


class Windows:
    def __init__(
        self,
        paths: Paths,
        browser_lock: threading.Lock,
        *,
        slots: int = 1,
        minutes: int = 20,
        xvfb: str = "Xvfb",
        x11vnc: str = "x11vnc",
        runner: Runner | None = None,
        clock: Callable[[], float] = time.monotonic,
        lock_wait: float = LOCK_WAIT,
        require_policy: bool = True,
    ) -> None:
        # Without the managed Chromium policy no window opens (window.py): it keeps the window from reaching the
        # profiles and snapshots of every other account here. Only tests turn this off.
        self.require_policy = require_policy
        self.paths, self.browser_lock = paths, browser_lock
        self.slots, self.limit = slots, minutes * 60
        self.xvfb, self.x11vnc = xvfb, x11vnc
        self._runner = runner or self._sign_in
        self._clock = clock
        self._lock_wait = lock_wait
        self._logins: dict[str, Login] = {}
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------------------------------------------

    def start(self, account: str, layout: str, *, email: str | None, taken: list[str]) -> Login:
        if layout not in window.LAYOUTS:
            raise ValueError("layout")
        self.paths.profile(account)  # validates the id
        if self.require_policy and not window.policy_installed():
            raise window.PolicyMissing()
        with self._lock:
            self.reap()
            for other in self._logins.values():
                if other.account == account and other.state in ACTIVE:
                    other.cancel.set()  # one window per account: the newer one wins
            used = {login.slot for login in self._logins.values() if login.state in ACTIVE}
            free = [slot for slot in range(self.slots) if slot not in used]
            if not free:
                raise Busy()
            now = self._clock()
            login = Login(secrets.token_urlsafe(18), account, layout, free[0], now, now + self.limit)
            self._logins[login.id] = login
        try:
            self._start_display(login)
        except Exception:
            self._end(login, "failed", "display")
            raise
        threading.Thread(target=self._run, args=(login, email, list(taken)), daemon=True).start()
        return login

    def get(self, login_id: str) -> Login | None:
        with self._lock:
            self.reap()
            return self._logins.get(login_id)

    def close(self, login_id: str) -> None:
        """Cancel a window, unless it is already saving a finished sign-in."""
        login = self.get(login_id)
        if login is not None and login.state in ("starting", "waiting"):
            login.cancel.set()

    def close_account(self, account: str) -> None:
        """Cancel every window of an account (it is being removed), saving ones too."""
        with self._lock:
            for login in self._logins.values():
                if login.account == account and login.state in ACTIVE:
                    login.cancel.set()

    def reap(self) -> None:
        """Windows past their time are told to stop; ended ones are forgotten after ENDED_TTL (hold ``_lock``)."""
        now = self._clock()
        for login in list(self._logins.values()):
            if login.state in ACTIVE and now > login.deadline:
                login.cancel.set()
            elif login.ended_at is not None and now - login.ended_at > ENDED_TTL:
                self._logins.pop(login.id, None)

    # -- the window -----------------------------------------------------------------------------------------------

    def _run(self, login: Login, email: str | None, taken: list[str]) -> None:
        if not self.browser_lock.acquire(timeout=self._lock_wait):
            self._end(login, "busy")
            return
        kept_before = (self.paths.keeper(login.account) or {}).get("state") in (SIGNED_IN, ERROR)
        try:
            if login.cancel.is_set():
                raise window.Cancelled()

            def on_state(state: str, host: str) -> None:
                login.state, login.host = state, host[:100]

            result = self._runner(login, email, taken, max(self.paths.generation(login.account), 0), on_state)
            login.email = str(result["email"])
            self.paths.record(login.account, SIGNED_IN, email=login.email, generation=int(result["generation"]))
            self._end(login, "connected")
        except window.Refused as refused:
            # The profile now holds a Google account this account may not use: it goes, all of it. (Its own account
            # was not signed in there any more, or the window would have found it at once: a kept snapshot of it is
            # withdrawn too, so photos-mcp says not_signed_in.)
            self.paths.wipe_profile(login.account)
            if kept_before:
                unpublish(self.paths.session(login.account))
                self.paths.record(login.account, SIGNED_OUT, error=refused.code)
            self._end(login, refused.code)
        except window.NotViewable as exc:
            self._tidy(login, kept_before)
            self._end(login, "failed", exc.code)
        except (window.Cancelled, window.WindowClosed):
            self._tidy(login, kept_before)
            self._end(login, "expired" if self._clock() > login.deadline else "cancelled")
        except ExportError as exc:
            self._tidy(login, kept_before)
            self._end(login, "empty_library" if exc.code == "empty_library" else "failed", exc.code)
        except Exception as exc:
            self._tidy(login, kept_before)
            self._end(login, "failed", type(exc).__name__)
        finally:
            self.browser_lock.release()

    def _sign_in(self, login: Login, email, taken, min_generation, on_state) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        import os

        return window.sign_in(
            profile=self.paths.profile(login.account),
            session_dir=self.paths.session(login.account),
            layout=login.layout,
            env={**os.environ, "DISPLAY": login.display},
            email=email,
            taken=taken,
            min_generation=min_generation,
            cancel=login.cancel,
            deadline=login.deadline,
            on_state=on_state,
            require_policy=self.require_policy,
        )

    def _tidy(self, login: Login, kept_before: bool) -> None:
        """A window that did not connect an account nobody keeps leaves no browser profile behind (it may hold a
        half-finished or unchecked sign-in). A kept account's profile stays: it may still be signed in."""
        if not kept_before:
            self.paths.wipe_profile(login.account)

    def _end(self, login: Login, state: str, error: str | None = None) -> None:
        login.state, login.error, login.ended_at = state, error, self._clock()
        self._stop_display(login)
        detail = f" ({error})" if error else ""
        log.info("window %s for account %s ended: %s%s", login.id[:6], login.account, state, detail)

    # -- the display and its remote-desktop view ------------------------------------------------------------------

    def _start_display(self, login: Login) -> None:
        width, height = window.LAYOUTS[login.layout]
        login.procs.append(
            subprocess.Popen(
                [self.xvfb, login.display, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        time.sleep(0.8)
        # Loopback only: the service's WebSocket bridge is the one way in, behind its bearer and the view token.
        login.procs.append(
            subprocess.Popen(
                [self.x11vnc, "-display", login.display, "-localhost", "-rfbport", str(login.vnc_port), "-nopw",
                 "-forever", "-shared", "-quiet", "-noxdamage"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )  # fmt: skip
        time.sleep(0.8)
        if any(proc.poll() is not None for proc in login.procs):
            raise RuntimeError("Xvfb or x11vnc exited at start")

    def _stop_display(self, login: Login) -> None:
        for proc in reversed(login.procs):
            proc.terminate()
        for proc in login.procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        login.procs.clear()
