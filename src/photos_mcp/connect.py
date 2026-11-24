"""Sign-in links: a person connects their own Google account from their assistant (README "Signing in with a link").

``photos_connect`` makes a link for the CALLER's account only: ``PHOTOS_PUBLIC_URL/connect/<id>``, an unguessable id
that works for a short time and one successful sign-in. The page shows Google's own sign-in page, running in that
account's browser profile in the login service (``photos_login``) and viewed through noVNC, both relayed by this
server (signin-window's relay). Once the person is signed in, the login service writes the account's snapshot itself
(and keeps it fresh); this server only binds the account to the Google address the sign-in proved (``_bind``).
Nothing here shows, returns or reads a session. Photos mounts these routes behind its browser-owner
policy; the attempt id alone grants no access.

  GET  /connect/{id}               the page
  POST /connect/{id}/start         open a sign-in window (the layout follows the person's screen)
  GET  /connect/{id}/state         progress; binds the account's Google address once the login service is done
  POST /connect/{id}/cancel        close the window, unless the sign-in is already being saved
  GET  /connect/{id}/novnc/...     the viewer's files (noVNC, from the login service, plus the paste add-on)
  WS   /connect/{id}/vnc           the live view of Google's page
"""

from __future__ import annotations

import collections
import logging
import secrets
import threading
import time
from dataclasses import replace
from typing import Any

from signin_window.page import PageText
from signin_window.relay import AttemptContext, Link, LinkLimited, Relay
from starlette.applications import Starlette

from . import accounts
from .config import Settings
from .connection_controls import ConnectionSuspended, PublicationGuard
from .errors import PhotosError
from .login_client import LoginClient, LoginUnavailable
from .ui import renderer_options

log = logging.getLogger(__name__)

TEXT = PageText(
    service="Google",
    title="Connect your Google Photos",
    notice="Use your password or verification code — device passkeys aren't available in this remote window.",
    help_html=(
        "Sign in to Google in the remote browser below, using your password and any verification Google requests. "
        "On a phone or tablet, tap Paste, Keyboard, or Zoom in the toolbar above; on a desktop, paste with Ctrl+V or "
        "Cmd+V. This application keeps the signed-in session; assistant permissions are managed separately. You can "
        "also sign out this device in your Google Account's Security settings."
    ),
    intro_html=(
        "Connect Google Photos for <b>{label}</b>. Sign in to the Google account you want this application to use."
    ),
    button="Continue to Google's sign-in",
    secure_host=r"(^|\.)google\.com$",
    host_label="🔒 accounts.google.com",
    starting="Opening Google…",
).with_outcomes(
    connected=(
        "<p class=ok>✓</p><h1>Connection saved</h1><p>Your Google Photos sign-in ({email}) is saved. "
        "You can close this page and return to your assistant.</p>"
    ),
    wrong_account=(
        "<h1>That is a different Google account</h1>"
        "<p>Sign in to the Google account already linked to this connection. "
        "Disconnecting does not change which Google account is linked.</p>"
    ),
    in_use=(
        "<h1>That Google account is someone else's here</h1><p>It is already connected for another person on this "
        "server. Sign in with your own Google account.</p>"
    ),
    empty_library=(
        "<h1>Your library looks empty</h1><p>There was no photo to open, so nothing was connected. Add a photo to "
        "Google Photos and try again.</p>"
    ),
    recovery_required=(
        "<h1>Connection recovery required</h1><p>Ask your administrator to repair local publication state.</p>"
    ),
    busy="<h1>The server's browser is busy</h1><p>Please try again in a few minutes.</p>",
    expired="<h1>This link has expired</h1><p>Return to Connections to start a new Google Photos sign-in.</p>",
    failed="<h1>Something went wrong</h1><p>Nothing was connected.</p>",
)


class ReservedRelay(Relay):
    """Relay with binding pre-teardown slot reservation so 429 never cancels a live window."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()
        self._reserved_slots = 0

    def reserve_slot(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            made = self._made.setdefault(key, collections.deque())
            while made and now - made[0] > 3600:
                made.popleft()
            if len(made) >= self.links_per_hour:
                return False
            self._expire(now)
            if len(self._retired) + self._reserved_slots >= self.max_links:
                return False
            self._reserved_slots += 1
            return True

    def release_slot(self) -> None:
        with self._lock:
            if self._reserved_slots > 0:
                self._reserved_slots -= 1

    def create(
        self,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        label: str | None = None,
        context: AttemptContext | None = None,
        reserved: bool = False,
    ) -> Link:
        if self._authorize is not None and (context is None or not context.owner):
            raise ValueError("authenticated links require a host-assigned owner")
        to_retire: list[Link] = []
        with self._lock:
            if reserved and self._reserved_slots > 0:
                self._reserved_slots -= 1
            now = self._clock()
            made = self._made.setdefault(key, collections.deque())
            while made and now - made[0] > 3600:
                made.popleft()
            if len(made) >= self.links_per_hour:
                raise LinkLimited(key)
            self._expire(now)
            if not reserved and len(self._retired) + self._reserved_slots >= self.max_links:
                raise LinkLimited(key)
            made.append(now)
            for old in [item for item in self._links.values() if item.key == key]:
                self._links.pop(old.id, None)
                old.closed = True
                old.view_token = None
                if old.window_id is not None:
                    self._track_retired_locked(old)
                    if old.state != "saving":
                        to_retire.append(old)
            while len(self._links) >= self.max_links:
                oldest = self._links.pop(min(self._links.values(), key=lambda item: item.created).id)
                oldest.closed = True
                oldest.view_token = None
                if oldest.window_id is not None:
                    self._track_retired_locked(oldest)
                    if oldest.state != "saving":
                        to_retire.append(oldest)
            link_id = secrets.token_urlsafe(24)
            link = Link(
                link_id,
                key,
                now,
                now + self.link_ttl,
                f"{self.public_url}/{link_id}",
                dict(params or {}),
                key if label is None else label,
            )
            link.context = context
            self._links[link.id] = link
        for old in to_retire:
            threading.Thread(target=self._retire, args=(old,), daemon=True).start()
        return link


class ConnectFlow:
    def __init__(
        self,
        settings: Settings,
        client: LoginClient,
        *,
        clock=time.time,
        authorize=None,
        revision=None,
        text=None,
        public_url=None,
        deployment=None,
        controls=None,
    ) -> None:
        assert settings.public_url
        self.settings, self.client, self.controls = settings, client, controls
        self.revision = revision or (lambda account: str(account.created_at))
        # The relay runs one hook at a time: the binding checks and the write go together.
        if deployment is not None:
            import functools

            from .deployment import DeploymentRelay

            relay = functools.partial(DeploymentRelay, deployment=deployment)
        else:
            relay = ReservedRelay
        text = text or TEXT
        options = renderer_options(PageText)
        if options:
            text = replace(text, **options)
        self.relay = relay(client, (public_url or settings.public_url).rstrip("/") + "/connect", text=text,
                           clock=clock, authorize=authorize,
                           before_start=self._before_start, on_ended=self._ended)  # fmt: skip

    def create(
        self,
        account: accounts.Account | None,
        *,
        signed_in: bool,
        reserved: bool = False,
        ticket: Any = None,
    ) -> dict[str, Any]:
        """A sign-in link for ``account`` (the caller's): replaces any open link of that account."""
        if account is None:
            if reserved and hasattr(self.relay, "release_slot"):
                self.relay.release_slot()
            raise PhotosError("connect_unavailable", log_detail="account missing")
        try:
            target = self.revision(account)
        except BaseException:
            if reserved and hasattr(self.relay, "release_slot"):
                self.relay.release_slot()
            raise
        try:
            extra = {"ticket": ticket} if ticket is not None else {}
            link = self.relay.create(
                account.id,
                context=AttemptContext(owner=account.id, target=target),
                reserved=reserved,
                **extra,
            )
        except LinkLimited:
            raise PhotosError("connect_limited", log_detail="link rate") from None
        log.info("connect: a sign-in link for account %s", account.id)
        return {
            "url": link.url,
            "expires_at": link.expires_at,
            "account_id": account.id,
            "signed_in": signed_in,
            "next_step": (
                "Give the person this link and nothing else; they sign in with their own identity "
                "before connecting Google Photos. "
                "It opens Google's own sign-in page "
                f"and works once, for {self.relay.link_ttl // 60} minutes; a newer link replaces it. After they say "
                "they are done, photos_status shows the session state ok."
            ),
        }

    def _before_start(self, link: Link) -> str | None:
        """Each window gets the account as it is now: the address it may sign in as, and those others hold."""
        try:
            account = accounts.load(self.settings, link.key)
        except PhotosError:
            return "failed"  # the account was removed meanwhile
        if link.context and link.context.target != self.revision(account):
            return "failed"
        link.params.update(account=account.id, email=account.email,
                           taken=accounts.taken_emails(self.settings, account.id))  # fmt: skip
        if account.id == self.settings.default_account and link.context:
            link.params["epoch"] = link.context.target.split(":", 1)[1]
        return None

    def _ended(self, link: Link, window: dict[str, Any]) -> str:
        try:
            account = accounts.load(self.settings, link.key)
            if link.context and link.context.target != self.revision(account):
                return "failed"
        except PhotosError:
            return "failed"
        state = str(window.get("state") or "")
        if state != "connected":
            return state if state in TEXT.outcomes else "failed"
        return self._bind(link, str(window.get("email") or ""))

    def _bind(self, link: Link, email: str) -> str:
        if self.controls is None:
            result = self._bind_verified(link, email)
            return self._forget_refused(link) if result == "in_use" else result
        if not link.context:
            return "failed"
        guard = PublicationGuard(self.controls, link.key)
        epoch = link.context.target.split(":", 1)[1]
        # Native promotion opens admission itself, with its snapshot receipt.
        # A relay result alone cannot activate retained native credentials.
        boundary = guard.status(epoch) if link.key == self.settings.default_account else guard.activate(epoch)
        result = "failed"
        try:
            with boundary:
                result = self._bind_verified(link, email)
                if result != "connected":
                    raise ConnectionSuspended()
                return result
        except ConnectionSuspended:
            return self._forget_refused(link) if result == "in_use" else "failed"

    def _bind_verified(self, link: Link, email: str) -> str:
        bound = (
            email.strip().lower() == self.settings.expected_email
            if link.key == self.settings.default_account
            else accounts.bind_email(self.settings, link.key, email)
        )
        if not bound:
            return "in_use"
        link.show(email=accounts.mask_email(email.strip().lower()))
        log.info("connect: account %s signed in", link.key)
        return "connected"

    def _forget_refused(self, link):
        # Forget can wait for a keeper holding the browser lock and waiting to
        # record its result in SQLite. Never call it inside a control transaction.
        log.warning("connect: account %s could not be bound to the address it signed in as", link.key)
        try:
            self.client.forget(link.key)
        except LoginUnavailable:
            pass
        return "in_use"

    def app(self) -> Starlette:
        return self.relay.app()
