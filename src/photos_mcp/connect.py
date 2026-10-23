"""Sign-in links: a person connects their own Google account from their assistant (README "Signing in with a link").

``photos_connect`` makes a link for the CALLER's account only: ``PHOTOS_PUBLIC_URL/connect/<id>``, an unguessable id
that works for a short time and one successful sign-in. The page shows Google's own sign-in page, running in that
account's browser profile in the login service (``photos_login``) and viewed through noVNC, both relayed by this
server (signin-window's relay). Once the person is signed in, the login service writes the account's snapshot itself
(and keeps it fresh); this server only binds the account to the Google address the sign-in proved (``_bind``).
Nothing here shows, returns or reads a session, and nothing here is behind the server's bearer: the id is the
credential.

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
from signin_window.relay import Link, LinkLimited, Relay
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
        "Sign in on Google's own page below, as you usually do: with your password and the code or prompt Google asks "
        "for (passkeys do not work in this window). Pasting works too. The page runs in a browser on the server your "
        "assistant uses, which stays signed in to your Google account; your assistant can then search and look at "
        "your photos, add photos, organise albums and, only after you say yes, move photos to the trash (Google keeps "
        "them 60 days) or delete an album. You can sign it out any time in your Google Account (Security, Your "
        "devices)."
    ),
    intro_html=(
        "This link connects Google Photos for <b>{label}</b> on this assistant. Continue only if that is you: whoever "
        "signs in here, their Google account is the one the assistant will use."
    ),
    button="Continue to Google's sign-in",
    secure_host=r"(^|\.)google\.com$",
    host_label="🔒 accounts.google.com",
    starting="Opening Google…",
).with_outcomes(
    connected=(
        "<p class=ok>✓</p><h1>Connected</h1><p>Your Google Photos ({email}) is connected. You can close this page and "
        "go back to your assistant.</p>"
    ),
    wrong_account=(
        "<h1>That's a different Google account</h1><p>This assistant is already connected to another Google account. "
        "Sign in with that one, or ask the person who set it up to disconnect it first.</p>"
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
    expired="<h1>This link has expired</h1><p>Ask your assistant to connect Google Photos again for a new link.</p>",
    failed="<h1>Something went wrong</h1><p>Nothing was connected.</p>",
)


class ConnectFlow:
    def __init__(self, settings: Settings, client: LoginClient, *, clock=time.time) -> None:
        assert settings.public_url
        self.settings, self.client = settings, client
        # The relay runs one hook at a time: the binding checks and the write go together.
        self.relay = Relay(client, settings.public_url.rstrip("/") + "/connect", text=TEXT, clock=clock,
                           before_start=self._before_start, on_ended=self._ended)  # fmt: skip

    def create(self, account: accounts.Account | None, *, signed_in: bool) -> dict[str, Any]:
        """A sign-in link for ``account`` (the caller's): replaces any open link of that account."""
        if account is None or not account.linked:
            raise PhotosError("connect_unavailable", log_detail="the default account signs in through the operator")
        try:
            link = self.relay.create(account.id)
        except LinkLimited:
            raise PhotosError("connect_limited", log_detail="link rate") from None
        log.info("connect: a sign-in link for account %s", account.id)
        return {
            "url": link.url,
            "expires_at": link.expires_at,
            "account_id": account.id,
            "signed_in": signed_in,
            "next_step": (
                "Give the person this link and nothing else; it is for them only. It opens Google's own sign-in page "
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
        link.params.update(account=account.id, email=account.email,
                           taken=accounts.taken_emails(self.settings, account.id))  # fmt: skip
        return None

    def _ended(self, link: Link, window: dict[str, Any]) -> str:
        state = str(window.get("state") or "")
        if state != "connected":
            return state if state in TEXT.outcomes else "failed"
        return self._bind(link, str(window.get("email") or ""))

    def _bind(self, link: Link, email: str) -> str:
        if not accounts.bind_email(self.settings, link.key, email):
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
