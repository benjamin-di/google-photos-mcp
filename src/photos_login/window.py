"""One account's browser profile, opened two ways: as a sign-in window a person uses through a sign-in link, and as
the keeper's hourly refresh nobody watches.

The sign-in window shows Google's own sign-in page and automates nothing on it: the person does every step, Google's
checks included (``tests/test_login_service.py`` pins that). The login service keeps every further account's profile
and snapshot, so the window must not become a way to reach them, or anything but Google's sign-in:

* Chromium runs under the managed policy ``chromium-policy.json`` (installed by the image, checked before a window
  opens): no ``file://``, ``chrome://``, ``view-source:``, ``data:`` or DevTools, no file dialogs, downloads,
  printing or saved passwords.
* Top-level pages are limited to Google's sign-in hosts (``allowed_document``).
* The live view is offered only while the profile is shown signed out on Google's sign-in page (state ``waiting``):
  a profile that is still signed in is checked, and later exported, with nobody watching.

Once Google Photos shows a signed-in account, the window checks it is one this account may use, then writes the
snapshot from the same browser (``photos_mcp.session_export.export_from_context``: it loads the library and opens
one photo, as the CLI exporter does) and closes. The profile stays: it is the account's Google sign-in, refreshed by
the keeper.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from photos_mcp.session_export import export_from_context, page_email, settled_url, signed_out_page

APP_URL = "https://photos.google.com/"
APP_HOST = "photos.google.com"
SIGN_IN_URL = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fphotos.google.com%2F"
LAYOUTS = {"desktop": (1280, 860), "phone": (430, 900)}
# Passkeys cannot work in a remote window (no device near the server): with WebAuthn off, Google offers the
# password and the other ways instead. The same browser identity serves the sign-in and every refresh.
LAUNCH_ARGS = (
    "--no-first-run",
    "--disable-features=Translate,WebAuthentication,WebAuthenticationPasskeysUI,WebAuthCable,WebAuthnConditionalUI",
)
# Where Chromium (Chrome for Testing, as patchright installs it, and distribution builds) reads managed policies.
POLICY_DIRS = (Path("/etc/opt/chrome_for_testing/policies/managed"), Path("/etc/chromium/policies/managed"))
POLICY_NAME = "photos-mcp-login.json"
POLICY = json.loads(resources.files("photos_login").joinpath("chromium-policy.json").read_text(encoding="utf-8"))
# The pages a sign-in may pass through: Google's sign-in, its consent and account-notice pages, the hops that set
# the same sign-in on youtube.com and Google's country domains, the signed-out landing page, and Google Photos.
_SIGN_IN_HOSTS = frozenset(
    {"accounts.google.com", "accounts.youtube.com", "consent.google.com", "gds.google.com", APP_HOST}
)
_COUNTRY_ACCOUNTS = re.compile(r"accounts\.google\.(?:com?\.)?[a-z]{2}\Z")
WATCH_EVERY_MS = 1500
SIGNED_IN_WAIT_S = 20  # how long Google Photos may take to show a profile that is still signed in


class Cancelled(Exception):
    """The sign-in was cancelled or ran out of time."""


class WindowClosed(Exception):
    """The person closed the window before signing in."""


class Refused(Exception):
    """Signed in as a Google account this account may not use: ``wrong_account`` or ``in_use``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotViewable(Exception):
    """The profile shows something nobody may watch through a link: an account notice of a signed-in account
    (``interstitial``) or no clear page (``unexpected_page``). The window ends without a live view."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PolicyMissing(Exception):
    """The managed Chromium policy is not installed: no window opens without it."""


def policy_installed() -> bool:
    """Whether a managed policy file with at least our URL blocklist is installed where Chromium reads it."""
    for folder in POLICY_DIRS:
        try:
            installed = json.loads((folder / POLICY_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(installed, dict) and set(POLICY["URLBlocklist"]) <= set(installed.get("URLBlocklist") or ()):
            return True
    return False


def allowed_document(url: str) -> bool:
    """Whether the window may show this page at top level (subresources and frames load freely; local schemes are
    blocked by the managed policy)."""
    if url == "about:blank":
        return True
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
        return False
    if host in ("google.com", "www.google.com"):
        return (parts.path or "").startswith("/photos/about")
    return host in _SIGN_IN_HOSTS or bool(_COUNTRY_ACCOUNTS.fullmatch(host))


def _playwright() -> Any:
    from patchright.sync_api import sync_playwright

    return sync_playwright()


def launch(playwright: Any, profile: Path, layout: str, env: Mapping[str, str]) -> Any:
    """The account's own persistent profile, headed on the display in ``env`` (DISPLAY)."""
    width, height = LAYOUTS[layout]
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        str(profile),
        headless=False,
        no_viewport=True,
        env=dict(env),
        args=[f"--window-size={width},{height}", "--window-position=0,0", *LAUNCH_ARGS],
    )


def account_shown(pages: list[Any]) -> str | None:
    """The Google address a Google Photos page shows as signed in, if one does."""
    for page in pages:
        try:
            if (urlsplit(page.url).hostname or "") == APP_HOST and (email := page_email(page.content())):
                return email.strip().lower()
        except Exception:  # navigating, or closed meanwhile
            continue
    return None


def _starting(page: Any) -> str | None:
    """With nobody watching: the address a still signed-in profile shows, or None once Google positively shows the
    profile signed out (then its sign-in page is up). Anything else is NotViewable: never shown to the link's
    holder."""
    page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + SIGNED_IN_WAIT_S
    while True:
        url = settled_url(page)
        reading = signed_out_page(url)
        if reading == "signed_out":
            if (urlsplit(url).hostname or "") != "accounts.google.com":
                page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=60_000)
            return None
        if reading == "challenge":
            return None  # Google asks the person to prove it is them: that is a sign-in step they do themselves
        if reading == "interstitial":
            raise NotViewable("interstitial")  # a signed-in account's notice: not for the link's holder
        if (urlsplit(url).hostname or "") == APP_HOST and (signed := account_shown([page])):
            return signed
        if time.monotonic() > deadline:
            raise NotViewable("unexpected_page")
        page.wait_for_timeout(1000)


def sign_in(
    *,
    profile: Path,
    session_dir: Path,
    layout: str,
    env: Mapping[str, str],
    email: str | None,
    taken: list[str],
    min_generation: int,
    cancel: threading.Event,
    deadline: float,
    on_state: Callable[[str, str], None],
    require_policy: bool = True,
) -> dict[str, Any]:
    """Show Google's sign-in in ``profile`` until the person is signed in, then write the snapshot.

    ``email``: the only address the account may sign in as (None: any but ``taken``). Raises PolicyMissing, Refused,
    NotViewable, Cancelled or WindowClosed; returns export_from_context's result."""
    if require_policy and not policy_installed():
        raise PolicyMissing()
    with _playwright() as playwright:
        context = launch(playwright, profile, layout, env)
        try:

            def guard(route: Any) -> None:
                request = route.request
                top = request.is_navigation_request() and request.frame.parent_frame is None
                if top and not allowed_document(request.url):
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", guard)
            page = context.pages[0] if context.pages else context.new_page()
            signed = _starting(page)
            # From here on, until a signed-in account shows, the window shows Google's sign-in (state "waiting", the
            # only one with a live view).
            while not signed:
                if cancel.is_set() or time.monotonic() > deadline:
                    raise Cancelled()
                pages = [p for p in context.pages if not p.is_closed()]
                if not pages:
                    raise WindowClosed()
                on_state("waiting", urlsplit(pages[0].url).hostname or "")
                signed = account_shown(pages)
                if not signed:
                    pages[0].wait_for_timeout(WATCH_EVERY_MS)
            on_state("saving", APP_HOST)  # the live view ends here
            if signed in taken:
                raise Refused("in_use")
            if email and signed != email.strip().lower():
                raise Refused("wrong_account")
            context.unroute("**/*")  # the export loads only Google Photos; its headers must be the browser's own
            return export_from_context(context, session_dir, expected_email=signed, min_generation=min_generation)
        finally:
            try:
                context.close()
            except Exception:
                pass


def refresh(
    *, profile: Path, session_dir: Path, env: Mapping[str, str], email: str, min_generation: int
) -> dict[str, Any]:
    """The keeper's round for one account: open its profile, write a fresh snapshot (export_from_context raises
    ExportError with its code when the profile is signed out or Google shows something else)."""
    with _playwright() as playwright:
        context = launch(playwright, profile, "desktop", env)
        try:
            return export_from_context(context, session_dir, expected_email=email, min_generation=min_generation)
        finally:
            try:
                context.close()
            except Exception:
                pass
