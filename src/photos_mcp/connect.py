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

import logging
import time
from typing import Any

from signin_window.page import PageText
from signin_window.relay import AttemptContext, Link, LinkLimited, Relay
from starlette.applications import Starlette

from . import accounts
from .config import Settings
from .errors import PhotosError
from .login_client import LoginClient, LoginUnavailable

log = logging.getLogger(__name__)

TEXT = PageText(
    service="Google",
    title="Connect your Google Photos",
    help_html=(
        "Sign in to Google in the remote browser below, using your password and any verification Google requests. "
        "Device passkeys do not work here. Paste with Ctrl+V or Cmd+V. This application keeps the signed-in session; "
        "assistant permissions are managed separately. You can also sign out this device in your Google Account's "
        "Security settings."
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
    busy="<h1>The server's browser is busy</h1><p>Please try again in a few minutes.</p>",
    expired="<h1>This link has expired</h1><p>Return to Connections to start a new Google Photos sign-in.</p>",
    failed="<h1>Something went wrong</h1><p>Nothing was connected.</p>",
)


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
    ) -> None:
        assert settings.public_url
        self.settings, self.client = settings, client
        self.revision = revision or (lambda account: str(account.created_at))
        # The relay runs one hook at a time: the binding checks and the write go together.
        if deployment is not None:
            import functools

            from .deployment import DeploymentRelay

            relay = functools.partial(DeploymentRelay, deployment=deployment)
        else:
            relay = Relay
        self.relay = relay(client, (public_url or settings.public_url).rstrip("/") + "/connect", text=text or TEXT,
                           clock=clock, authorize=authorize,
                           before_start=self._before_start, on_ended=self._ended)  # fmt: skip

    def create(self, account: accounts.Account | None, *, signed_in: bool) -> dict[str, Any]:
        """A sign-in link for ``account`` (the caller's): replaces any open link of that account."""
        if account is None:
            raise PhotosError("connect_unavailable", log_detail="account missing")
        try:
            link = self.relay.create(
                account.id, context=AttemptContext(owner=account.id, target=self.revision(account))
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
        bound = (
            email.strip().lower() == self.settings.expected_email
            if link.key == self.settings.default_account
            else accounts.bind_email(self.settings, link.key, email)
        )
        if not bound:
            # The login service checked the same rules before it wrote the snapshot; this is a race with another
            # account binding the same address. That snapshot must not stay: the account starts over.
            log.warning("connect: account %s could not be bound to the address it signed in as", link.key)
            try:
                self.client.forget(link.key)
            except LoginUnavailable:
                pass
            return "in_use"
        link.show(email=accounts.mask_email(email.strip().lower()))
        log.info("connect: account %s signed in", link.key)
        return "connected"

    def app(self) -> Starlette:
        return self.relay.app()
