"""Sign-in windows (signin-window's service): one virtual display, one remote-desktop server on loopback and the
account's own browser profile, in which ``SignIn`` (the runner) shows Google's sign-in page.

States: ``starting`` (checking whether the profile is still signed in) -> ``waiting`` (Google's sign-in page is up;
the person is signing in; the only state with a live view) -> ``saving`` (signed in; the snapshot is being written) ->
``connected``; or ``wrong_account`` / ``in_use`` (signed in as a Google account this account may not use: the
profile is wiped), ``empty_library``, ``busy`` (the browser stayed in use), ``failed`` (also when the profile showed
something nobody may watch, such as a signed-in account's notice), ``expired`` or ``cancelled``. A cancel once the
window is ``saving`` changes nothing: the sign-in is finished and kept. Ended windows stay readable for a while so
photos-mcp sees how they ended.

Only one browser runs at a time (the service's lock, shared with the keeper): a window waits for a refresh to
finish. Each window has its own view token, which only the caller that started it gets.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from signin_window.service import Window

from photos_mcp.session_export import ExportError, unpublish

from . import window
from .profiles import ERROR, SIGNED_IN, SIGNED_OUT, Paths

log = logging.getLogger("photos-login")


class SignIn:
    """The runner: Google's sign-in in the account's own profile, on the window's display. A plain function (the
    browser library is synchronous), so it runs in a thread and stops when ``window.cancelled`` is set."""

    def __init__(self, paths: Paths, *, require_policy: bool = True,
                 sign_in: Callable[..., dict[str, Any]] = window.sign_in) -> None:  # fmt: skip
        # Without the managed Chromium policy no window opens (window.py): it keeps the window from reaching the
        # profiles and snapshots of every other account here. Only tests turn this off.
        self.paths, self.require_policy, self._sign_in = paths, require_policy, sign_in

    def __call__(self, login: Window) -> None:
        account = str(login.params["account"])
        login.report(account=account)
        kept_before = (self.paths.keeper(account) or {}).get("state") in (SIGNED_IN, ERROR)

        def on_state(state: str, host: str) -> None:
            if host:
                login.at(host)
            if state == "waiting":
                login.waiting()
            elif state == "saving":
                login.saving()

        try:
            result = self._sign_in(
                profile=self.paths.profile(account),
                session_dir=self.paths.session(account),
                layout=login.layout,
                env=login.env(),
                email=login.params.get("email"),
                taken=list(login.params.get("taken") or []),
                min_generation=max(self.paths.generation(account), 0),
                cancel=login.cancelled,
                deadline=login.deadline,
                on_state=on_state,
                require_policy=self.require_policy,
            )
            email = str(result["email"])
            self.paths.record(account, SIGNED_IN, email=email, generation=int(result["generation"]))
            login.report(email=email)
            login.finish("connected")
        except window.Refused as refused:
            # The profile now holds a Google account this account may not use: it goes, all of it. (Its own account
            # was not signed in there any more, or the window would have found it at once: a kept snapshot of it is
            # withdrawn too, so photos-mcp says not_signed_in.)
            self.paths.wipe_profile(account)
            if kept_before:
                unpublish(self.paths.session(account))
                self.paths.record(account, SIGNED_OUT, error=refused.code)
            login.finish(refused.code)
        except window.NotViewable as exc:
            self._tidy(account, kept_before)
            login.finish("failed", exc.code)
        except window.Cancelled:
            self._tidy(account, kept_before)  # the service says cancelled or expired
        except window.WindowClosed:
            self._tidy(account, kept_before)
            login.finish("cancelled")
        except ExportError as exc:
            self._tidy(account, kept_before)
            login.finish("empty_library" if exc.code == "empty_library" else "failed", exc.code)
        except Exception as exc:
            self._tidy(account, kept_before)
            login.finish("failed", type(exc).__name__)

    def _tidy(self, account: str, kept_before: bool) -> None:
        """A window that did not connect an account nobody keeps leaves no browser profile behind (it may hold a
        half-finished or unchecked sign-in). A kept account's profile stays: it may still be signed in."""
        if not kept_before:
            self.paths.wipe_profile(account)
