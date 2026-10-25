"""Browser steps for the Google identity target: warm, sign in, export.

Every step answers with a closed signal from :mod:`.outcomes`; the run
policy (budget, holds, cooldowns, publication) lives in :mod:`.keeper`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from signin_window.automation.auth import AuthBrowserSession, SecretFiller
from signin_window.automation.auth import AuthFlowFailure as AcquisitionFailure
from signin_window.automation.auth import AuthOutcome as MintOutcome
from signin_window.automation.auth import SecretDeliveryError as LoginInfrastructureError
from signin_window.automation.browser import BrowserSession as PatchrightMintContext
from signin_window.automation.challenges import ChallengeMode, ChallengeVendor
from signin_window.automation.google import GoogleIdentityDriver
from signin_window.automation.resolver import ChallengeResolver
from signin_window.automation.secret_filler import configured_credential_filler

from .config import IdentityTargetSpec
from .outcomes import (
    AccountMismatch,
    BlockReason,
    HoldReason,
    ProviderBlocked,
    SessionDead,
    SignInHold,
    VerifyFailed,
    VerifyFailure,
)
from .page import has_signed_in_data, parse_wiz, same_account
from .runtime import PatchrightRuntime
from .snapshot import (
    HEADER_KINDS,
    ExportCookie,
    HeaderTemplate,
    SnapshotError,
    filter_header_template,
    select_export_cookies,
    validate_header_template,
)

_NAVIGATION_SECONDS = 45.0
_SIGNED_IN_WAIT_SECONDS = 20.0
_TILE_WAIT_SECONDS = 20.0
_IMAGE_WAIT_SECONDS = 20.0
_COOKIE_SETTLE_SECONDS = 2.0
_INTERSTITIAL_SECONDS = 60.0
_INTERSTITIAL_MAX_CLICKS = 4
_LANDING_SECONDS = 30.0
_SESSION_COOKIES = frozenset({"SID", "__Secure-1PSID"})
_RPC_PATH_SUFFIX = "/data/batchexecute"
_PUBLIC_PAGE_HOSTS = frozenset({"google.com", "www.google.com"})
#: CDP resource types of the exported request kinds.
_DOCUMENT_TYPE = "Document"

_HOLD_BY_MINT_OUTCOME = {
    MintOutcome.CREDENTIAL_REJECTED: HoldReason.CREDENTIAL_REJECTED,
    MintOutcome.SECOND_FACTOR_REQUIRED: HoldReason.SECOND_FACTOR_UNSUPPORTED,
    MintOutcome.ACCOUNT_ALERT: HoldReason.ACCOUNT_ALERT,
    MintOutcome.CHALLENGE_BLOCKED: HoldReason.CHALLENGE,
    MintOutcome.TIMEOUT: HoldReason.TIMEOUT,
    MintOutcome.UNKNOWN_FLOW: HoldReason.UNKNOWN_FLOW,
    MintOutcome.INFRASTRUCTURE_ERROR: HoldReason.SIGN_IN_INFRASTRUCTURE,
}


def _host_path(url: str) -> tuple[str, str]:
    parts = urlsplit(url or "")
    return (parts.hostname or "").lower(), parts.path or "/"


class _WireHeaderRecorder:
    """Request header values exactly as Chrome sent them, via CDP.

    Playwright's ``request.headers_array()`` can return the renderer's
    provisional headers (alphabetical, without ``sec-fetch-*``) instead of
    the network stack's. ``Network.requestWillBeSentExtraInfo`` carries the
    headers actually sent (Chrome reports them sorted by name, so the
    consumer applies Chrome's wire order); ``Network.requestWillBeSent`` (same
    ``requestId``) gives the url, method and type to classify them.

    Raw headers are reduced to the allowlist inside the event handler, so
    cookies and tokens are never retained. Redirect hops share a
    ``requestId``; hops are paired in order, and a request whose counts
    disagree is ambiguous and skipped.
    """

    def __init__(self, page: Any) -> None:
        self._page = page
        self._session: Any | None = None
        self._sent: dict[str, list[tuple[str, str, str]]] = {}
        self._wire: dict[str, list[HeaderTemplate]] = {}

    def open(self) -> None:
        """Attach and enable Network; a failure just leaves nothing captured."""

        try:
            self._session = self._page.context.new_cdp_session(self._page)
            self._session.on("Network.requestWillBeSent", self._on_sent)
            self._session.on("Network.requestWillBeSentExtraInfo", self._on_wire)
            self._session.send("Network.enable")
            # Cache hits carry no ExtraInfo; thumbnails are usually cached in
            # the persistent profile, so force real fetches for this warm-up.
            self._session.send("Network.setCacheDisabled", {"cacheDisabled": True})
        except Exception:
            self.close()

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            with suppress(Exception):
                session.detach()

    def _on_sent(self, params: Any) -> None:
        with suppress(Exception):
            request = params["request"]
            self._sent.setdefault(str(params["requestId"]), []).append(
                (str(request["url"]), str(request["method"]), str(params["type"]))
            )

    def _on_wire(self, params: Any) -> None:
        with suppress(Exception):
            raw = params["headers"]
            # Superset of every kind's allowlist; nothing else is kept.
            kept = filter_header_template("rpc", ((str(name), str(value)) for name, value in raw.items()))
            self._wire.setdefault(str(params["requestId"]), []).append(kept)

    def requests(self) -> list[tuple[str, str, str, HeaderTemplate]]:
        """``(url, method, type, headers)`` per hop, in request order."""

        paired: list[tuple[str, str, str, HeaderTemplate]] = []
        for request_id, hops in self._sent.items():
            wire = self._wire.get(request_id, [])
            if len(wire) != len(hops):
                continue
            paired.extend((url, method, kind, headers) for (url, method, kind), headers in zip(hops, wire, strict=True))
        return paired


class GoogleTargetBrowser:
    """One persistent Google profile driven for warm-up, sign-in and export."""

    def __init__(
        self,
        browser: PatchrightMintContext,
        spec: IdentityTargetSpec,
        *,
        expected_email: str,
        driver: GoogleIdentityDriver,
        challenge_resolver: ChallengeResolver,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._browser = browser
        self._spec = spec
        self._expected_email = expected_email
        self._driver = driver
        self._resolver = challenge_resolver
        self._monotonic = monotonic
        self._headers: dict[str, HeaderTemplate] = {}

    # -- warm -----------------------------------------------------------

    def warm(self) -> str:
        """Load the library, prove the account, and load one CDN image.

        Returns the verified account email. Raises ``SessionDead`` only on
        positive signed-out evidence (a sign-in page or Google's public
        product page), ``ProviderBlocked`` for pushback, ``AccountMismatch`` for
        another account and ``VerifyFailed`` for everything else, including any
        HTTP error status other than 429 (even on a sign-in host), an error
        page, a slow page or a page without signed-in data.
        """

        seen_images: list[int] = []
        self._headers = {}

        def on_response(response: Any) -> None:
            with suppress(Exception):
                host, _path = _host_path(str(response.url))
                content_type = str(response.headers.get("content-type", ""))
                if host == self._spec.image_host and response.status == 200 and content_type.startswith("image/"):
                    seen_images.append(response.status)

        page = self._browser.page
        wire = _WireHeaderRecorder(page)
        wire.open()
        page.on("response", on_response)
        try:
            navigation = self._browser.navigate_observed(self._spec.warm_url, timeout_seconds=_NAVIGATION_SECONDS)
            status = navigation.status
            if status == 429:
                raise ProviderBlocked(BlockReason.RATE_LIMITED)
            self._settle()
            if _host_path(self._browser.current_url)[1].startswith("/sorry"):
                raise ProviderBlocked(BlockReason.SORRY_PAGE)
            # An error status proves nothing about the session, wherever it was
            # served: a 5xx on a sign-in hop is an outage, not a signed-out
            # profile, and must never spend a credential sign-in.
            if isinstance(status, int) and status >= 400:
                raise VerifyFailed(VerifyFailure.PAGE_INVALID)
            self._require_library_host()
            html = self._signed_in_html()
            wiz = parse_wiz(html) or {}
            email = wiz.get("oPEP7c")
            if not email:
                raise VerifyFailed(VerifyFailure.PAGE_INVALID)
            if not same_account(email, self._expected_email):
                raise AccountMismatch()
            self._open_first_tile(seen_images)
            self._headers = self._capture_headers(wire)
            return email
        finally:
            wire.close()
            with suppress(Exception):
                page.remove_listener("response", on_response)

    # -- request headers ------------------------------------------------

    def _request_kind(self, url: str, method: str, resource_type: str) -> str | None:
        """Which exported request kind a request on the wire is, if any."""

        method = method.upper()
        host, path = _host_path(url)
        if host == self._spec.image_host and method == "GET":
            return "image"
        if host != self._spec.warm_host:
            return None
        if method == "POST" and path.endswith(_RPC_PATH_SUFFIX):
            return "rpc"
        if method == "GET" and resource_type == _DOCUMENT_TYPE:
            return "page"
        return None

    def _capture_headers(self, wire: _WireHeaderRecorder) -> dict[str, HeaderTemplate]:
        """The first valid wire header template per kind, or ``headers_missing``.

        The recorder only ever holds allowlisted headers; a request whose
        on-the-wire headers never arrived (no ``requestWillBeSentExtraInfo``)
        is not a candidate, so renderer-side provisional headers can never
        be exported.
        """

        user_agent = self.user_agent()
        captured: dict[str, HeaderTemplate] = {}
        for url, method, resource_type, headers in wire.requests():
            kind = self._request_kind(url, method, resource_type)
            if kind is None or kind in captured:
                continue
            template = filter_header_template(kind, headers)
            try:
                validate_header_template(kind, template, user_agent=user_agent)
            except SnapshotError:
                continue
            captured[kind] = template
        if set(captured) != set(HEADER_KINDS):
            raise VerifyFailed(VerifyFailure.HEADERS_MISSING)
        return {kind: captured[kind] for kind in HEADER_KINDS}

    def request_headers(self) -> dict[str, HeaderTemplate]:
        """Header templates captured by the last successful :meth:`warm`."""

        if set(self._headers) != set(HEADER_KINDS):
            raise VerifyFailed(VerifyFailure.HEADERS_MISSING)
        return dict(self._headers)

    def _settle(self) -> None:
        with suppress(Exception):
            self._browser.page.wait_for_load_state("domcontentloaded", timeout=_NAVIGATION_SECONDS * 1000)

    def _require_library_host(self) -> None:
        if _host_path(self._browser.current_url)[1].startswith("/sorry"):
            raise ProviderBlocked(BlockReason.SORRY_PAGE)
        if self._on_interstitial():
            self.skip_interstitials()
        self._classify_location()

    def _classify_location(self) -> None:
        """Pass on the library host; otherwise raise the one closed signal.

        Liveness has one authority: only a sign-in page or Google's public
        product page is evidence of a signed-out profile. Anything else off
        the library (an error document, an unexpected host, an interstitial
        that did not go away) is a page that proved nothing.
        """

        host, path = _host_path(self._browser.current_url)
        if path.startswith("/sorry"):
            raise ProviderBlocked(BlockReason.SORRY_PAGE)
        if host == self._spec.warm_host:
            return
        if self._signed_out_location(host, path):
            raise SessionDead()
        raise VerifyFailed(VerifyFailure.PAGE_INVALID)

    def _signed_out_location(self, host: str, path: str) -> bool:
        if host in self._spec.identity_hosts:
            # Judge the URL already read: a second read could see another page.
            return not self._is_interstitial(host, path)
        # Google's public product page, where the signed-out app redirects.
        # Other Google hosts (consent, myaccount, ...) prove nothing.
        return host in _PUBLIC_PAGE_HOSTS and path.startswith("/photos/about")

    def _signed_in_html(self) -> str:
        deadline = self._monotonic() + _SIGNED_IN_WAIT_SECONDS
        while True:
            html = self._content()
            if html is not None and has_signed_in_data(html):
                return html
            self._classify_location()
            if self._monotonic() >= deadline:
                # Still on the library host without signed-in data: a slow or
                # error page, not evidence of a signed-out session.
                raise VerifyFailed(VerifyFailure.PAGE_INVALID)
            self._browser.wait(0.5)

    def _content(self) -> str | None:
        try:
            content = self._browser.page.content()
        except Exception:
            return None
        return content if isinstance(content, str) else None

    def _open_first_tile(self, seen_images: list[int]) -> None:
        selectors = self._spec.library_tile_selectors
        if not self._browser.wait_for_visible(selectors, timeout_seconds=_TILE_WAIT_SECONDS):
            raise VerifyFailed(VerifyFailure.LIBRARY_EMPTY)
        # Grid thumbnails may already have loaded; the proof is an image
        # response caused by opening the item, which mints the CDN cookies.
        before = len(seen_images)
        self._browser.click(selectors)
        deadline = self._monotonic() + _IMAGE_WAIT_SECONDS
        while len(seen_images) <= before:
            if self._monotonic() >= deadline:
                raise VerifyFailed(VerifyFailure.IMAGE_RESPONSE_MISSING)
            self._browser.wait(0.5)
        self._browser.wait(_COOKIE_SETTLE_SECONDS)

    # -- interstitials --------------------------------------------------

    def _on_interstitial(self) -> bool:
        return self._is_interstitial(*_host_path(self._browser.current_url))

    def _is_interstitial(self, host: str, path: str) -> bool:
        return host in self._spec.identity_hosts and any(
            path.startswith(prefix) for prefix in self._spec.interstitial_path_prefixes
        )

    def skip_interstitials(self) -> None:
        """Dismiss post-login account prompts (at most four clicks)."""

        clicks = 0
        deadline = self._monotonic() + _INTERSTITIAL_SECONDS
        while self._on_interstitial() and self._monotonic() < deadline:
            if clicks < _INTERSTITIAL_MAX_CLICKS and self._browser.click_optional(
                self._spec.interstitial_skip_selectors
            ):
                clicks += 1
                self._browser.wait(2.5)
                continue
            if clicks >= _INTERSTITIAL_MAX_CLICKS:
                return
            self._browser.wait(1.0)

    # -- sign-in --------------------------------------------------------

    def sign_in(self) -> None:
        """Drive one credential sign-in; any non-success is a closed signal."""

        browser = self._browser
        browser.navigate_committed("about:blank", timeout_seconds=15)
        entered = False

        def resolve(identity_browser: AuthBrowserSession) -> None:
            # The driver prepares passkey suppression (virtual authenticator
            # and WebAuthn-disabling init script) before its loop; entering
            # the sign-in URL here, on the first turn, makes that preparation
            # precede the first sign-in document, as in the live spike.
            nonlocal entered
            if not entered:
                entered = True
                identity_browser.navigate_committed(self._spec.login_url, timeout_seconds=_NAVIGATION_SECONDS)
                identity_browser.wait(1.5)
                return
            _host, path = _host_path(identity_browser.current_url)
            if path.startswith("/sorry"):
                raise ProviderBlocked(BlockReason.SORRY_PAGE)
            resolution = self._resolver.resolve(identity_browser, ChallengeMode.DETECT_ONLY)
            if not resolution.allows_continuation:
                if resolution.detection.vendor is ChallengeVendor.RECAPTCHA:
                    raise ProviderBlocked(BlockReason.CAPTCHA)
                raise SignInHold(HoldReason.CHALLENGE)

        try:
            result = self._driver.drive(
                browser,
                callback_complete=self._identity_done,
                resolve_challenge=resolve,
            )
        except AcquisitionFailure as failure:
            raise SignInHold(_HOLD_BY_MINT_OUTCOME.get(failure.outcome, HoldReason.UNKNOWN_FLOW)) from failure
        except LoginInfrastructureError as error:
            raise SignInHold(HoldReason.SIGN_IN_INFRASTRUCTURE) from error
        if not result.callback_complete:
            raise SignInHold(HoldReason.SIGN_IN_UNCONFIRMED)
        self.skip_interstitials()
        self._await_landing()

    def _identity_done(self) -> bool:
        """The driver's completion predicate: signed-in library or interstitial."""

        if self._on_interstitial():
            return True
        host, _path = _host_path(self._browser.current_url)
        if host != self._spec.warm_host:
            return False
        html = self._content()
        return html is not None and has_signed_in_data(html)

    def _await_landing(self) -> None:
        deadline = self._monotonic() + _LANDING_SECONDS
        while self._monotonic() < deadline:
            host, path = _host_path(self._browser.current_url)
            if path.startswith("/sorry"):
                raise ProviderBlocked(BlockReason.SORRY_PAGE)
            if host == self._spec.warm_host:
                html = self._content()
                if html is not None and has_signed_in_data(html):
                    return
            self._browser.wait(0.5)
        raise SignInHold(HoldReason.SIGN_IN_UNCONFIRMED)

    # -- export ---------------------------------------------------------

    def export_cookies(self) -> list[ExportCookie]:
        cookies = select_export_cookies(
            self._browser.cookies(),
            hosts=self._spec.export_hosts,
            names=self._spec.export_names,
        )
        if not any(cookie.name in _SESSION_COOKIES for cookie in cookies) or not any(
            cookie.domain == self._spec.image_host for cookie in cookies
        ):
            raise VerifyFailed(VerifyFailure.EXPORT_INCOMPLETE)
        return cookies

    def user_agent(self) -> str:
        return self._browser.user_agent()


@contextmanager
def open_google_browser(
    spec: IdentityTargetSpec,
    *,
    profile: Path,
    expected_email: str,
    runtime: PatchrightRuntime | None = None,
    secrets: SecretFiller | None = None,
) -> Iterator[GoogleTargetBrowser]:
    selected_runtime = runtime or PatchrightRuntime()
    try:
        with selected_runtime.open(
            profile,
            require_virtual_display=True,
            launch_config=spec.launch_config(),
        ) as browser:
            yield GoogleTargetBrowser(
                browser,
                spec,
                expected_email=expected_email,
                driver=GoogleIdentityDriver(
                    spec.identity_config(),
                    secrets=secrets
                    or configured_credential_filler(os.environ.get("PHOTOS_MAINTENANCE_CREDENTIAL_READER", "")),
                ),
                challenge_resolver=ChallengeResolver({}),
            )
    finally:
        selected_runtime.close()


__all__ = ["GoogleTargetBrowser", "open_google_browser"]
