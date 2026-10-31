"""An authenticated Photos connection screen using the application's existing accounts and login service."""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import replace
from urllib.parse import parse_qs, quote, urlsplit

from signin_window.connections import disconnect_confirmation, timestamp
from signin_window.connections import page as connection_page
from signin_window.page import PageText
from signin_window.relay import Link
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import accounts
from .connect import TEXT, ConnectFlow
from .connection_controls import ConnectionAdmission, publication_pending
from .connections_auth import BrowserLogin, ConnectionsConfig
from .errors import PhotosError
from .session import read_snapshot
from .state import StateStore


class Connections:
    def __init__(
        self, settings, client, config: ConnectionsConfig, *, clock=None, oidc_transport=None, flow_factory=ConnectFlow
    ):
        self.settings, self.client, self.config = settings, client, config
        self.drain = lambda account: None
        self.prefix = urlsplit(config.public_url).path.rstrip("/")
        self.path = self.prefix + "/connections/"
        self.auth = BrowserLogin(config, **({"clock": clock} if clock else {}), transport=oidc_transport)
        # Every link is bound to the signed-in owner who just pressed Connect (authorize refuses any other), so the
        # window opens at once: the intro only matters for a link handed to someone.
        text = replace(
            TEXT,
            reconnect=True,
            auto_start=True,
            return_url=self.path,
            intro_html="Connect Google Photos for <b>{label}</b>. Your application keeps the verified connection.",
        )
        text = text.with_outcomes(failed=PageText().outcomes["failed"])
        self.flow = flow_factory(
            settings, client, authorize=self.authorize, revision=self.revision, text=text, public_url=config.public_url
        )

    def revision(self, account):
        epoch, _ = self.auth.store.control(account.id)
        return f"{account.created_at}:{epoch}"

    def account_allowed(self, account_id):
        return not self.auth.store.control(account_id)[1] and not publication_pending(self.auth.store, account_id)

    async def authorize(self, request, link: Link, operation):
        person = self.auth.identity(request)
        if not person or not link.context or person["account"] != link.context.owner:
            return False
        try:
            account = accounts.load(self.settings, person["account"])
        except PhotosError:
            return False
        return self.account_allowed(account.id) and link.context.target == self.revision(account)

    def origin_ok(self, request):
        return self.auth.origin_ok(request)

    async def index(self, request):
        person = self.auth.identity(request)
        if not person:  # an unmapped person is refused at the callback, so this cannot loop
            return RedirectResponse(self.path + "login", status_code=303)
        revision = self.auth.store.control(person["account"])[0]
        status = self.status(person)
        state = status["state"]
        action = "Reconnect" if state in ("connected", "check_needed") else "Connect"
        controls = self.form("start", action, revision)
        if state in ("connecting", "saving"):
            controls = (
                f'<a class=button href="{html.escape(status["url"], quote=True)}">Continue sign-in</a>'
                if state == "connecting"
                else "<p>You can close this page while the connection is saved.</p>"
            )
        elif state == "disconnecting":
            controls = self.form("disconnect", "Finish disconnecting", revision)
        elif state == "setup_required":
            controls = "<p>Ask your administrator to finish setting up this account.</p>"
        elif state != "disconnected":
            controls += self.form("disconnect", "Disconnect Google Photos", revision)
        if status.get("managed"):
            controls += "<p class=muted>Your saved browser profile is kept.</p>"
        email = html.escape(
            status.get("account")
            or (
                "Your Google account will appear after sign-in"
                if state in ("connecting", "saving")
                else "No Google account connected"
            )
        )
        body = (
            f"<article><h2>Google Photos</h2><p id=connection-state role=status>{html.escape(status['label'])}</p>"
            f"<p>{email}</p>"
            + (timestamp(status["verified_at"]) if status.get("verified_at") else "")
            + f"<p>{html.escape(status.get('detail', ''))}</p>{controls}</article>"
            "<p class=muted>Connecting Google Photos does not give an assistant permission to use it. "
            "Assistant permissions are managed separately.</p>"
            + self.form("logout", "Sign out of Connections", revision)
        )
        return HTMLResponse(
            self.page(
                "Google Photos connection",
                "Signed in to Connections as " + person["name"],
                body,
                refresh=state in ("connecting", "saving"),
            ),
        )

    def status(self, person):
        try:
            account = accounts.load(self.settings, person["account"])
        except PhotosError:
            return {"state": "setup_required", "label": "Account setup required"}
        _, suspended = self.auth.store.control(account.id)
        if suspended:
            return {
                "state": "disconnecting" if suspended == 2 else "disconnected",
                "label": "Access stopped — finish disconnecting" if suspended == 2 else "Disconnected",
                "managed": not account.linked,
            }
        if publication_pending(self.auth.store, account.id):
            return {"state": "saving", "label": "Finishing your saved connection", "url": self.path}
        pending = next(
            (
                link
                for link in self.flow.relay.links(account.id)
                if link.state in ("ready", "starting", "waiting", "saving")
                and link.context
                and link.context.target == self.revision(account)
            ),
            None,
        )
        if pending:
            return {
                "state": "saving" if pending.state == "saving" else "connecting",
                "label": "Saving connection" if pending.state == "saving" else "Sign-in in progress",
                "url": pending.url,
            }
        accounts.bind_from_keeper(self.settings, account)
        account = accounts.load(self.settings, account.id)
        try:
            snapshot = read_snapshot(account.session_dir)
        except PhotosError:
            return {"state": "not_connected", "label": "Sign-in required"}
        keeper = accounts.keeper_status(account) or {}
        if keeper.get("generation") != snapshot.generation:
            keeper = {}
        if not account.email or snapshot.email.lower() != account.email.lower():
            return {"state": "not_connected", "label": "We could not confirm the Google account"}
        if keeper.get("state") == "signed_out":
            return {"state": "not_connected", "label": "Sign-in required"}
        clock = self.auth.store.clock()
        stale = clock - snapshot.verified_at > 3 * 3600 or snapshot.verified_at - clock > 300
        state = StateStore(account.state_dir, rpc_budget=account.rpc_budget, thumb_budget=account.thumb_budget)
        rejected = state.dead_code(snapshot.generation)
        return {
            "state": "check_needed" if stale or rejected or keeper.get("state") == "error" else "connected",
            "label": "Connection needs attention"
            if stale or rejected or keeper.get("state") == "error"
            else "Connection saved",
            "account": accounts.mask_email(account.email),
            "verified_at": snapshot.describe()["verified_at"],
            "detail": "Google Photos access is checked when used.",
        }

    async def api_status(self, request):
        person = self.auth.identity(request)
        return (
            JSONResponse(self.status(person))
            if person
            else JSONResponse({"state": "sign_in_required"}, status_code=401)
        )

    async def mutate(self, request, action):
        person = self.auth.identity(request)
        if not person or not self.origin_ok(request):
            return Response(status_code=403)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 256:
                return Response(status_code=400)
            raw.extend(chunk)
        try:
            revision = parse_qs(raw.decode("ascii"), strict_parsing=True).get("revision")
        except (ValueError, UnicodeError):
            return Response(status_code=400)
        try:
            with ConnectionAdmission(self.auth.store).lock(
                person["account"], exclusive=True, timeout=0, lifecycle=True
            ):
                if revision != [self.auth.store.control(person["account"])[0]]:
                    return self.notice("This connection changed. Open the current connection before trying again.", 409)
                return await action(request)
        except TimeoutError:
            return self.notice("Another connection change is finishing. Retry shortly.", 409)

    async def start(self, request):
        return await self.mutate(request, self._start)

    async def _start(self, request):
        person = self.auth.identity(request)
        if not person or not self.origin_ok(request):
            return Response(status_code=403)
        try:
            account = accounts.load(self.settings, person["account"])
        except PhotosError:
            return self.notice("Account setup is required.", 409)
        if self.auth.store.control(account.id)[1] == 2:
            return self.notice("Confirm disconnect before starting another sign-in.", 409)
        if publication_pending(self.auth.store, account.id):
            return self.notice("The saved connection is being recovered. Try again shortly.", 409)
        if not self.auth.store.claim_owner(account.id, person["issuer"], person["sub"]):
            return Response(status_code=403)
        self.auth.store.set_control(account.id, 0)
        try:
            created = self.flow.create(account, signed_in=self.status(person)["state"] == "connected")
        except PhotosError as error:
            return self.notice(
                "A new sign-in could not be opened. Try again later.",
                429 if error.code == "connect_limited" else 409,
            )
        return RedirectResponse(created["url"], status_code=303)

    async def disconnect(self, request):
        return await self.mutate(request, self._disconnect)

    async def _disconnect(self, request):
        person = self.auth.identity(request)
        if not person or not self.origin_ok(request):
            return Response(status_code=403)
        account = accounts.load(self.settings, person["account"])
        if not self.auth.store.claim_owner(account.id, person["issuer"], person["sub"]):
            return Response(status_code=403)
        # Revoke publication and new MCP calls before asking the profile owner to drain and delete.
        self.auth.store.set_control(account.id, 2)
        for link in self.flow.relay.links(account.id):
            await asyncio.to_thread(self.flow.relay.cancel, link)
        try:
            await asyncio.to_thread(self.drain, account.id)
            await asyncio.to_thread(self.client.forget, account.id)
        except Exception:
            return self.notice("Disconnect could not be confirmed. Open the current connection to retry.", 503)
        self.auth.store.set_control(account.id, 1)
        return RedirectResponse(self.path, status_code=303)

    async def logout(self, request):
        return await self.auth.logout(request)

    def notice(self, message, status):
        return HTMLResponse(
            self.page(
                "Google Photos connection",
                message,
                f'<a class=button href="{html.escape(self.path, quote=True)}">Back to Connections</a>',
            ),
            status_code=status,
        )

    def form(self, action, label, revision):
        kind = (
            "danger"
            if action == "disconnect"
            else "quiet"
            if action == "logout"
            else "secondary"
            if label == "Reconnect"
            else "primary"
        )
        form = (
            f'<form method=post action="{self.path}{action}">'
            f'<input type=hidden name=revision value="{html.escape(revision, quote=True)}">'
            f'<button class="{kind}">{html.escape(label)}</button></form>'
        )
        if action == "disconnect" and label.startswith("Disconnect"):
            return disconnect_confirmation(
                form,
                "Google Photos",
                "This stops this application from using your Google Photos connection. Your photos are not deleted.",
            )
        return form

    def page(self, title, intro, body, *, refresh=False):
        return connection_page(title, body, intro=intro, refresh=refresh, login_url=self.path + "login")

    def app(self):
        portal = Starlette(
            routes=[
                Route("/", self.index),
                Route("/status", self.api_status),
                Route("/login", self.auth.login),
                Route("/callback", self.auth.callback),
                Route("/start", self.start, methods=["POST"]),
                Route("/disconnect", self.disconnect, methods=["POST"]),
                Route("/logout", self.logout, methods=["POST"]),
            ]
        )
        return self.auth.middleware(portal)

    def viewer_app(self):
        app = self.flow.app()

        async def viewer(scope, receive, send):
            if scope["type"] == "http" and scope["method"] == "GET":
                request = Request(scope)
                pattern = r"(?:" + re.escape(self.prefix) + r")?/connect/([A-Za-z0-9_-]{32,64})"
                match = re.fullmatch(pattern, scope["path"])
                if match and not self.auth.identity(request):
                    destination = self.prefix + "/connect/" + match.group(1)
                    redirect = RedirectResponse(
                        self.path + "login?next=" + quote(destination, safe=""), status_code=303
                    )
                    return await redirect(scope, receive, send)
            await app(scope, receive, send)

        # Keep the relay's lifespan visible to Starlette's mount host and explicit tests.
        viewer.router = app.router
        return viewer
