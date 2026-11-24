"""An authenticated Photos connection screen using the application's existing accounts and login service."""

from __future__ import annotations

import asyncio
import html
import re
import sqlite3
from dataclasses import replace
from urllib.parse import parse_qs, quote, urlsplit

from signin_window.connections import disconnect_confirmation, timestamp
from signin_window.connections import page as connection_page
from signin_window.page import PageText
from signin_window.relay import AttemptContext, Link
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import accounts
from .connect import TEXT, ConnectFlow
from .connection_controls import ConnectionAdmission, publication_pending, recovery_required
from .connections_auth import BrowserLogin, ConnectionsConfig
from .errors import PhotosError
from .session import read_snapshot
from .state import StateStore
from .ui import renderer_options


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
            settings,
            client,
            authorize=self.authorize,
            revision=self.revision,
            text=text,
            public_url=config.public_url,
            controls=self.auth.store,
        )
        self._pending_close: dict[str, list[Link]] = {}
        self._known_links: dict[str, dict[str, Link]] = {}
        self._draining_accounts: set[str] = set()

    @staticmethod
    def _link_still_open(link: Link) -> bool:
        with link.lock:
            return link.window_id is not None or link.closing or link.state in ("starting", "waiting", "saving")

    @staticmethod
    def _is_already_completed_idle(link: Link) -> bool:
        if not link.lock.acquire(blocking=False):
            return False
        try:
            return link.window_id is None and not link.closing and link.state == "connected"
        finally:
            link.lock.release()

    def _cancel_and_check(self, link: Link) -> tuple[bool, bool]:
        self.flow.relay.cancel(link)
        with link.lock:
            still_open = link.window_id is not None or link.closing or link.state in ("starting", "waiting", "saving")
            completed = link.state == "connected"
            return still_open, completed

    def _snapshot_links(self, account_id: str) -> tuple[list[Link], set[str], set[str]]:
        seen: dict[str, Link] = {}
        known = self._known_links.get(account_id)
        if known:
            for link_id, link in list(known.items()):
                if link.key == account_id:
                    seen[link_id] = link
        for link in self.flow.relay.links(account_id):
            if link.key == account_id:
                seen[link.id] = link
        with self.flow.relay._lock:
            for link in self.flow.relay._retired.values():
                if link.key == account_id:
                    seen[link.id] = link
            active_relay_ids = {link_id for link_id, link in self.flow.relay._links.items() if link.key == account_id}
            live_ids = set(self.flow.relay._links) | set(self.flow.relay._retired)
        already_completed: set[str] = set()
        for link_id, link in list(seen.items()):
            if link.lock.acquire(blocking=False):
                try:
                    still_open = (
                        link.window_id is not None or link.closing or link.state in ("starting", "waiting", "saving")
                    )
                    if not still_open and link.state == "connected":
                        already_completed.add(link_id)
                finally:
                    link.lock.release()
                if known and link_id not in live_ids and not still_open:
                    known.pop(link_id, None)
        return list(seen.values()), already_completed, active_relay_ids

    def _account_links(self, account_id: str) -> list[Link]:
        links, _, _ = self._snapshot_links(account_id)
        return links

    def revision(self, account):
        epoch, _ = self.auth.store.control(account.id)
        return f"{account.created_at}:{epoch}"

    def account_allowed(self, account_id):
        return not self.auth.store.control(account_id)[1] and not publication_pending(self.auth.store, account_id)

    async def authorize(self, request, link: Link, operation):
        person = self.auth.identity(request)
        if not person or not link.context or person["account"] != link.context.owner:
            return False
        if link.closed or person["account"] in self._draining_accounts:
            return False
        try:
            account = accounts.load(self.settings, person["account"])
        except PhotosError:
            return False
        return (
            self.auth.store.control(account.id)[1] != 2
            and not publication_pending(self.auth.store, account.id)
            and link.context.target == self.revision(account)
        )

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
                + self.form("start", "Start new sign-in", revision, replace=status["url"].rsplit("/", 1)[-1])
                if state == "connecting"
                else "<p>You can close this page while the connection is saved.</p>"
            )
        elif state == "disconnecting":
            controls = self.form("disconnect", "Finish disconnecting", revision)
        elif state == "recovery_required":
            controls = "<p>Ask your administrator to repair this saved connection.</p>"
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
        if recovery_required(self.auth.store, account.id):
            return {"state": "recovery_required", "label": "Local connection recovery required"}
        if suspended == 2:
            return {
                "state": "disconnecting",
                "label": "Access stopped — finish disconnecting",
                "managed": not account.linked,
            }
        if publication_pending(self.auth.store, account.id):
            return {"state": "saving", "label": "Finishing your saved connection", "url": self.path}
        pending = (
            None
            if account.id in self._draining_accounts
            else next(
                (
                    link
                    for link in self.flow.relay.links(account.id)
                    if not link.closed
                    and link.state in ("ready", "starting", "waiting", "saving")
                    and link.context
                    and link.context.target == self.revision(account)
                ),
                None,
            )
        )
        if pending:
            saving = pending.state == "saving"
            layout = getattr(pending, "layout", "")
            view_note = " (mobile view)" if layout == "phone" else " (desktop view)" if layout == "desktop" else ""
            return {
                "state": "saving" if saving else "connecting",
                "label": "Saving connection" if saving else f"Sign-in in progress{view_note}",
                "url": pending.url,
                "layout": layout,
            }
        if suspended:
            return {"state": "disconnected", "label": "Disconnected", "managed": not account.linked}
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
            fields = parse_qs(raw.decode("ascii"), keep_blank_values=True, strict_parsing=True)
            revision = fields.get("revision")
            replace_values = fields.get("replace")
            if not set(fields) <= {"revision", "replace"} or (
                replace_values is not None and (len(replace_values) != 1 or not replace_values[0])
            ):
                return Response(status_code=400)
            request.state.replace_id = replace_values[0] if replace_values else None
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
        except sqlite3.OperationalError:
            return self.notice("Connection operations are still finishing. Open the current connection and retry.", 409)

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
        if recovery_required(self.auth.store, account.id):
            return JSONResponse({"error": "recovery_required"}, status_code=503)
        if self.auth.store.control(account.id)[1] == 2:
            return self.notice("Confirm disconnect before starting another sign-in.", 409)
        if publication_pending(self.auth.store, account.id):
            return self.notice("The saved connection is being recovered. Try again shortly.", 409)
        if not self.auth.store.claim_owner(account.id, person["issuer"], person["sub"]):
            return Response(status_code=403)
        still_open = []
        pending_completed = False
        for held in self._pending_close.get(account.id, ()):
            open_now, done_now = await asyncio.to_thread(self._cancel_and_check, held)
            if open_now:
                still_open.append(held)
            if done_now:
                pending_completed = True
        if still_open:
            self._pending_close[account.id] = still_open
            return self.notice(
                "The previous sign-in window is still closing. Wait a moment before starting a new sign-in.", 409
            )
        self._pending_close.pop(account.id, None)
        if pending_completed:
            return self.notice("Your previous sign-in just completed. Return to Connections to check its status.", 409)
        replace_id = getattr(request.state, "replace_id", None)
        signed_in = self.status(person)["state"] == "connected"
        deployment_ticket = None
        slot_reserved = False
        created_link = None
        committed = False
        draining_entered = False
        try:
            if replace_id is not None:
                with self.flow.relay._lock:
                    pre_target = self.flow.relay._links.get(replace_id)
                if (
                    pre_target is None
                    or pre_target.key != account.id
                    or pre_target.closed
                    or pre_target.state not in ("ready", "starting", "waiting")
                ):
                    return self.notice(
                        "This sign-in attempt is no longer active. Return to Connections to check its status.", 409
                    )
            deployment = getattr(self.flow.relay, "deployment", None)
            if deployment is not None:
                try:
                    deployment_ticket = deployment.ticket()
                except Exception:
                    return self.notice("This service is updating. Try again in a moment.", 503)
            if not self.flow.relay.reserve_slot(account.id):
                return self.notice("A new sign-in could not be opened. Try again later.", 429)
            slot_reserved = True
            self._draining_accounts.add(account.id)
            draining_entered = True
            previous, already_completed, active_relay_ids = await asyncio.to_thread(self._snapshot_links, account.id)
            if replace_id is not None:
                target_link = next((old for old in previous if old.id == replace_id), None)
                if (
                    target_link is None
                    or target_link.id not in active_relay_ids
                    or target_link.closed
                    or target_link.id in already_completed
                    or target_link.state not in ("ready", "starting", "waiting")
                ):
                    return self.notice(
                        "This sign-in attempt is no longer active. Return to Connections to check its status.", 409
                    )
                already_completed.discard(replace_id)
            if any(old.state == "saving" for old in previous):
                return self.notice("A previous sign-in is still finishing. Wait a moment before trying again.", 409)
            for old in previous:
                old.closed = True
            completed = False
            for old in previous:
                open_now, done_now = await asyncio.to_thread(self._cancel_and_check, old)
                if open_now:
                    still_open.append(old)
                if done_now and old.id not in already_completed:
                    completed = True
            if still_open:
                self._pending_close[account.id] = still_open
                return self.notice(
                    "The previous sign-in window is still closing. Wait a moment before starting a new sign-in.", 409
                )
            if completed:
                return self.notice(
                    "Your previous sign-in just completed. Return to Connections to check its status.", 409
                )
            slot_reserved = False
            try:
                created = self.flow.create(account, signed_in=signed_in, reserved=True, ticket=deployment_ticket)
            except PhotosError as error:
                return self.notice(
                    "A new sign-in could not be opened. Try again later.",
                    429 if error.code == "connect_limited" else 409,
                )
            deployment_ticket = None
            created_link = next((x for x in self.flow.relay.links(account.id) if x.url == created["url"]), None)
            if created_link is not None:
                created_link.context = AttemptContext(owner=account.id, target="")
                self._known_links.setdefault(account.id, {})[created_link.id] = created_link
            # Starting verification grants no authority to use retained credentials.
            self.auth.store.set_control(account.id, 1)
            if created_link is not None:
                created_link.context = AttemptContext(owner=account.id, target=self.revision(account))
            committed = True
        finally:
            if slot_reserved:
                self.flow.relay.release_slot()
            if deployment_ticket is not None:
                deployment_ticket.close()
            if created_link is not None and not committed:
                created_link.closed = True
                if account.id in self._known_links:
                    self._known_links[account.id].pop(created_link.id, None)
                await asyncio.to_thread(self.flow.relay.cancel, created_link)
            if draining_entered:
                self._draining_accounts.discard(account.id)
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
        still_open = []
        seen_ids: set[str] = set()
        account_links = await asyncio.to_thread(self._account_links, account.id)
        for link in (*self._pending_close.get(account.id, ()), *account_links):
            if link.id in seen_ids:
                continue
            seen_ids.add(link.id)
            open_now, _ = await asyncio.to_thread(self._cancel_and_check, link)
            if open_now:
                still_open.append(link)
        if still_open:
            self._pending_close[account.id] = still_open
        else:
            self._pending_close.pop(account.id, None)
        try:
            await asyncio.to_thread(self.drain, account.id)
            await asyncio.to_thread(self.client.forget, account.id)
        except Exception:
            if recovery_required(self.auth.store, account.id):
                return JSONResponse({"error": "recovery_required"}, status_code=503)
            return self.notice("Disconnect could not be confirmed. Open the current connection to retry.", 503)
        open_ids = {item.id for item in still_open}
        for item in still_open:
            item.closed = True
        settled_links = []
        if account.id in self._known_links:
            for link_id in list(self._known_links[account.id]):
                if link_id not in open_ids:
                    removed = self._known_links[account.id].pop(link_id, None)
                    if removed is not None:
                        settled_links.append(removed)
            if not self._known_links[account.id]:
                self._known_links.pop(account.id, None)
        with self.flow.relay._lock:
            for active_id, active_link in list(self.flow.relay._links.items()):
                if active_link.key == account.id and active_id not in open_ids:
                    active_link.closed = True
                    self.flow.relay._links.pop(active_id, None)
                    settled_links.append(active_link)
            for retired_id, retired_link in list(self.flow.relay._retired.items()):
                if retired_link.key == account.id and retired_id not in open_ids:
                    retired_link.closed = True
                    self.flow.relay._retired.pop(retired_id, None)
                    settled_links.append(retired_link)
        settle_deployment = getattr(self.flow.relay, "_deployment_done", None)
        if callable(settle_deployment):
            for item in settled_links:
                settle_deployment(item)
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

    def form(self, action, label, revision, *, replace=None):
        kind = (
            "danger"
            if action == "disconnect"
            else "quiet"
            if action == "logout"
            else "secondary"
            if label in ("Reconnect", "Start new sign-in")
            else "primary"
        )
        extra = f'<input type=hidden name=replace value="{html.escape(replace, quote=True)}">' if replace else ""
        form = (
            f'<form method=post action="{self.path}{action}">'
            f'<input type=hidden name=revision value="{html.escape(revision, quote=True)}">{extra}'
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
        return connection_page(
            title,
            body,
            intro=intro,
            refresh=refresh,
            login_url=self.path + "login",
            **renderer_options(connection_page),
        )

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
