"""Sign-in links: a person connects their own Google account from their assistant (README "Signing in with a link").

``photos_connect`` makes a link for the CALLER's account only: ``PHOTOS_PUBLIC_URL/connect/<id>``, an unguessable id
that works for ``LINK_TTL`` and one successful sign-in. The page shows Google's own sign-in page, running in that
account's browser profile in the login service (``photos_login``) and viewed through noVNC, both relayed by this
server. Once the person is signed in, the login service writes the account's snapshot itself (and keeps it fresh);
this server only binds the account to the Google address the sign-in proved. Nothing here shows, returns or reads a
session, and nothing here is behind the server's bearer: the id is the credential.

  GET  /connect/{id}               the page
  POST /connect/{id}/start         open a sign-in window (the layout follows the person's screen)
  GET  /connect/{id}/state         progress; binds the account's Google address once the login service is done
  POST /connect/{id}/cancel        close the window (the page sends it when it is closed)
  GET  /connect/{id}/novnc/...     the viewer's files (noVNC, from the login service, plus paste.js)
  WS   /connect/{id}/vnc           the live view of Google's page
"""

from __future__ import annotations

import collections
import html
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import anyio.to_thread
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from . import accounts
from .config import Settings
from .errors import PhotosError
from .login_client import LoginBusy, LoginClient, LoginUnavailable

log = logging.getLogger(__name__)

LINK_TTL = 20 * 60  # from the moment the link is made, whatever happens on the page
MAX_LINKS = 50  # open links on the server; the oldest goes first
LINKS_PER_HOUR = 6  # per account
MAX_STARTS = 4  # sign-in windows per link (a reload or "try again" opens a new one)
MISS_WINDOW, MISSES_PER_ADDRESS, MISSES_TOTAL = 10 * 60, 20, 200  # requests for ids that do not exist
LINK_ID = re.compile(r"[A-Za-z0-9_-]{32,64}\Z")
ACTIVE = ("starting", "waiting", "saving")
FINAL = ("connected",)
# What the login service may report at the end of a window, and what the page then shows.
ENDINGS = frozenset({"connected", "wrong_account", "in_use", "empty_library", "failed", "expired", "cancelled"})
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
PAGE_HEADERS = {
    **HEADERS,
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'none'"
    ),
}
PASTE_TAG = b'<script type="module" src="paste.js"></script>'


@dataclass
class Link:
    id: str
    account: str
    created: float
    state: str = "ready"  # ready, starting, waiting, saving, then one of ENDINGS or busy
    login_id: str | None = None
    view_token: str | None = field(default=None, repr=False)
    host: str = ""
    shown_email: str | None = None  # masked, once connected
    starts: int = 0
    closed: bool = False  # replaced by a newer link or expired: it opens nothing any more
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public(self) -> dict[str, Any]:
        return {"state": self.state, "host": self.host, "email": self.shown_email}


class Misses:
    """Requests for link ids that do not exist, per address and in total: guessing is slowed long before it matters
    (an id has 192 random bits)."""

    def __init__(self) -> None:
        self.by_address: dict[str, collections.deque] = {}
        self.total: collections.deque = collections.deque()

    def allowed(self, address: str, now: float) -> bool:
        if len(self.by_address) > 10_000:
            self.by_address.clear()
        for queue in (self.total, self.by_address.setdefault(address, collections.deque())):
            while queue and now - queue[0] > MISS_WINDOW:
                queue.popleft()
        return len(self.total) < MISSES_TOTAL and len(self.by_address[address]) < MISSES_PER_ADDRESS

    def record(self, address: str, now: float) -> None:
        self.total.append(now)
        self.by_address.setdefault(address, collections.deque()).append(now)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


class ConnectFlow:
    def __init__(self, settings: Settings, client: LoginClient, *, clock=time.time) -> None:
        assert settings.public_url
        self.settings, self.client, self.public_url = settings, client, settings.public_url.rstrip("/")
        self._clock = clock
        self._links: dict[str, Link] = {}
        self._made: dict[str, collections.deque] = {}
        self._misses = Misses()
        self._lock = threading.Lock()
        self._binding = threading.Lock()  # one address bound at a time: the checks and the write go together

    # -- links --------------------------------------------------------------------------------------------------

    def create(self, account: accounts.Account | None, *, signed_in: bool) -> dict[str, Any]:
        """A sign-in link for ``account`` (the caller's): replaces any open link of that account."""
        if account is None or not account.linked:
            raise PhotosError("connect_unavailable", log_detail="the default account signs in through the operator")
        now = self._clock()
        with self._lock:
            made = self._made.setdefault(account.id, collections.deque())
            while made and now - made[0] > 3600:
                made.popleft()
            if len(made) >= LINKS_PER_HOUR:
                raise PhotosError("connect_limited", log_detail="link rate")
            made.append(now)
            self._expire(now)
            replaced = [link for link in self._links.values() if link.account == account.id]
            for link in replaced:
                self._links.pop(link.id, None)
                link.closed = True
            while len(self._links) >= MAX_LINKS:
                oldest = self._links.pop(min(self._links.values(), key=lambda link: link.created).id)
                oldest.closed = True
                replaced.append(oldest)
            link = Link(secrets.token_urlsafe(24), account.id, now)
            self._links[link.id] = link
        for old in replaced:
            threading.Thread(target=self._retire, args=(old,), daemon=True).start()
        log.info("connect: a sign-in link for account %s", account.id)
        return {
            "url": f"{self.public_url}/connect/{link.id}",
            "expires_at": _iso(now + LINK_TTL),
            "account_id": account.id,
            "signed_in": signed_in,
            "next_step": (
                "Give the person this link and nothing else; it is for them only. It opens Google's own sign-in page "
                f"and works once, for {LINK_TTL // 60} minutes; a newer link replaces it. After they say they are "
                "done, photos_status shows the session state ok."
            ),
        }

    def _expire(self, now: float) -> None:
        for link in [x for x in self._links.values() if now - x.created > LINK_TTL]:
            self._links.pop(link.id, None)
            link.closed = True
            threading.Thread(target=self._retire, args=(link,), daemon=True).start()

    def find(self, link_id: str, address: str) -> Link | str:
        """The open link with this id, else "expired", or "limited" once this address asked for too many ids that do
        not exist. A real link always works: misses never lock anyone out of their own link."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            link = self._links.get(link_id) if LINK_ID.match(link_id or "") else None
            if link is not None:
                return link
            allowed = self._misses.allowed(address, now)
            self._misses.record(address, now)
            return "expired" if allowed else "limited"

    def _retire(self, link: Link) -> None:
        """Close a replaced or expired link's window, once any start or poll on it has finished."""
        with link.lock:
            self._close(link)

    def _close(self, link: Link) -> None:
        """Close the link's window, if one is open (hold ``link.lock``)."""
        if link.login_id:
            self.client.close(link.login_id)
        link.login_id, link.view_token = None, None
        if link.state in ACTIVE:
            link.state = "cancelled"

    # -- the sign-in --------------------------------------------------------------------------------------------

    def usable(self, link: Link) -> bool:
        return link.state not in FINAL and (link.starts < MAX_STARTS or link.state in ACTIVE)

    def start(self, link: Link, layout: str) -> None:
        with link.lock:
            if link.closed or link.state in ACTIVE or link.state in FINAL or link.starts >= MAX_STARTS:
                return
            try:
                account = accounts.load(self.settings, link.account)
            except PhotosError:
                link.state = "failed"  # the account was removed meanwhile
                return
            link.starts += 1
            try:
                login = self.client.start(
                    account.id,
                    layout if layout in ("phone", "desktop") else "desktop",
                    email=account.email,
                    taken=accounts.taken_emails(self.settings, account.id),
                )
            except LoginBusy:
                link.state = "busy"
                return
            except LoginUnavailable:
                link.state = "failed"
                return
            link.login_id, link.view_token, link.state, link.host = login["id"], login["view_token"], "starting", ""
            log.info("connect: account %s opened a sign-in window", link.account)

    def advance(self, link: Link) -> None:
        """Follow the sign-in; once the login service has written the snapshot, bind the account's address."""
        if not link.lock.acquire(blocking=False):
            return  # another poll is already on it
        try:
            if link.state not in ACTIVE or link.login_id is None:
                return
            try:
                login = self.client.state(link.login_id)
            except LoginUnavailable:
                link.state = "failed"
                return
            link.host = str(login.get("host") or "")[:100]
            state = login.get("state")
            if state in ACTIVE:
                link.state = state
                return
            link.login_id, link.view_token = None, None
            if state != "connected":
                link.state = state if state in ENDINGS else "failed"
                return
            with self._binding:
                link.state = self._bind(link, str(login.get("email") or ""))
        finally:
            link.lock.release()

    def _bind(self, link: Link, email: str) -> str:
        if not accounts.bind_email(self.settings, link.account, email):
            # The login service checked the same rules before it wrote the snapshot; this is a race with another
            # account binding the same address. That snapshot must not stay: the account starts over.
            log.warning("connect: account %s could not be bound to the address it signed in as", link.account)
            try:
                self.client.forget(link.account)
            except LoginUnavailable:
                pass
            return "in_use"
        link.shown_email = accounts.mask_email(email.strip().lower())
        log.info("connect: account %s signed in", link.account)
        return "connected"

    def cancel(self, link: Link) -> None:
        """The page was closed: close its window, unless the sign-in is already being saved (it then finishes, and
        the account is bound from what the login service recorded)."""
        with link.lock:
            if link.state in ("starting", "waiting"):
                self._close(link)

    # -- HTTP ---------------------------------------------------------------------------------------------------

    def app(self) -> Starlette:
        def address(request: Request | WebSocket) -> str:
            # The reverse proxy in front must set X-Forwarded-For (Caddy does); the last entry is the one it added
            # (earlier ones are whatever the client sent). Without one, every client shares one bucket.
            return (request.headers.get("x-forwarded-for") or "unknown").split(",")[-1].strip()[:64]

        async def lookup(request: Request) -> Link | Response:
            link = self.find(request.path_params["id"], address(request))
            if isinstance(link, Link):
                return link
            return JSONResponse({"state": link}, status_code=429 if link == "limited" else 404, headers=HEADERS)

        async def body(request: Request) -> dict[str, Any]:
            try:
                data = await request.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        def reply(link: Link) -> JSONResponse:
            return JSONResponse(link.public(), headers=HEADERS)

        async def page(request: Request) -> Response:
            link = self.find(request.path_params["id"], address(request))
            if not isinstance(link, Link) or not self.usable(link):
                return HTMLResponse(_page(None), status_code=429 if link == "limited" else 404, headers=PAGE_HEADERS)
            return HTMLResponse(_page(link), headers=PAGE_HEADERS)

        async def start(request: Request) -> Response:
            link = await lookup(request)
            if isinstance(link, Response):
                return link
            layout = str((await body(request)).get("layout"))
            await anyio.to_thread.run_sync(self.start, link, layout)
            return reply(link)

        async def state(request: Request) -> Response:
            link = await lookup(request)
            if isinstance(link, Response):
                return link
            await anyio.to_thread.run_sync(self.advance, link)
            return reply(link)

        async def cancel(request: Request) -> Response:
            link = await lookup(request)
            if not isinstance(link, Response):
                await anyio.to_thread.run_sync(self.cancel, link)
            return Response(status_code=204, headers=HEADERS)

        async def novnc(request: Request) -> Response:
            link = await lookup(request)
            if isinstance(link, Response):
                return Response(status_code=404, headers=HEADERS)
            path = request.path_params["path"]
            if path == "paste.js":
                return Response(PASTE_JS, media_type="text/javascript", headers=HEADERS)
            status, content, content_type = await anyio.to_thread.run_sync(self.client.static, path)
            if path == "vnc.html" and status == 200:
                content = _with_paste(content)
            headers = {**HEADERS, "X-Frame-Options": "SAMEORIGIN"}
            return Response(content, status_code=status, media_type=content_type or None, headers=headers)

        async def vnc(ws: WebSocket) -> None:
            # Only while Google's sign-in page is shown (the login service closes the view when that ends): the
            # window may otherwise show a profile that is still signed in.
            link = self.find(ws.path_params["id"], address(ws))
            if not isinstance(link, Link) or link.state != "waiting" or not link.login_id or not link.view_token:
                await ws.close(code=4404)
                return
            await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
            try:
                await self.client.bridge(link.login_id, link.view_token, ws)
            except LoginUnavailable:
                pass

        return Starlette(
            routes=[
                Route("/{id}", page, methods=["GET"]),
                Route("/{id}/start", start, methods=["POST"]),
                Route("/{id}/state", state, methods=["GET"]),
                Route("/{id}/cancel", cancel, methods=["POST"]),
                Route("/{id}/novnc/{path:path}", novnc, methods=["GET"]),
                WebSocketRoute("/{id}/vnc", vnc),
            ]
        )


PASTE_JS = resources.files("photos_mcp").joinpath("paste.js").read_bytes()


def _with_paste(page: bytes) -> bytes:
    """noVNC's own vnc.html, unchanged but for the paste add-on (paste.js) loaded beside its UI."""
    if b"</head>" not in page:
        return page
    return page.replace(b"</head>", PASTE_TAG + b"\n</head>", 1)


def _page(link: Link | None) -> str:
    if link is None:
        body = """<main class=msg><h1>This link has expired</h1>
<p>Sign-in links work once, for a short time. Ask your assistant to connect Google Photos again for a new one.</p>
</main>"""
        return PAGE.replace("{{BODY}}", body).replace("{{SCRIPT}}", "")
    body = BODY.replace("{{ACCOUNT}}", html.escape(link.account))
    return PAGE.replace("{{BODY}}", body).replace("{{SCRIPT}}", SCRIPT)


BODY = """<header><h1>Connect your Google Photos</h1>
<p>Sign in on Google's own page below, as you usually do: with your password and the code or prompt Google asks for
(passkeys do not work in this window). Pasting works too. The page runs in a browser on the server your assistant
uses, which stays signed in to your Google account; your assistant can then search and look at your photos, add
photos and organise albums for you, and never deletes anything. You can sign it out any time in your Google Account
(Security, Your devices).</p>
<div class=bar hidden><span id=host>🔒 accounts.google.com</span><span id=status></span></div></header>
<main id=intro class=msg><p>This link connects Google Photos for <b>{{ACCOUNT}}</b> on this assistant. Continue only
if that is you: whoever signs in here, their Google account is the one the assistant will use.</p>
<button id=go>Continue to Google's sign-in</button></main>
<div id=view></div>
<main id=done class=msg hidden></main>"""

PAGE = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=referrer content=no-referrer>
<title>Connect Google Photos</title>
<style>
:root{color-scheme:light;font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:#141414}
html,body{margin:0;height:100%;background:#f4f4f4}
body{display:flex;flex-direction:column}
header{padding:12px 16px 8px;background:#fff;border-bottom:1px solid #ddd}
h1{font-size:18px;margin:0 0 4px}
header p{margin:0 0 8px;font-size:13px;color:#4b4b4b;max-width:62em}
.bar{display:flex;justify-content:space-between;gap:12px;font-size:13px}
#host{color:#1a7f37;font-weight:600}#status{color:#4b4b4b;text-align:right}
#view{flex:1;min-height:0;display:flex}
#view iframe{flex:1;border:0;width:100%;height:100%;background:#fff}
.msg{margin:auto;padding:28px 20px;text-align:center;max-width:26em;width:100%;box-sizing:border-box}
.msg h1{font-size:22px}.ok{font-size:44px;color:#1a7f37;margin:0}
button{font:inherit;font-weight:600;margin-top:14px;width:100%;padding:12px 18px;border-radius:8px;
 border:1px solid #141414;background:#141414;color:#fff}
[hidden]{display:none!important}
@media (max-width:600px){header p{font-size:12px}}
</style>
{{BODY}}
{{SCRIPT}}
</html>"""

SCRIPT = """<script>
const base = location.pathname.replace(/\\/+$/, "");
const $ = id => document.getElementById(id);
const esc = t => String(t ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const post = (path, data) => fetch(`${base}/${path}`, {method: "POST", headers: {"content-type": "application/json"},
                                                      body: JSON.stringify(data || {})}).then(r => r.json());
const TEXT = {starting: "Opening Google…", waiting: "Sign in as you usually do. This page notices when you're in.",
              saving: "Signed in. Opening your library once to finish…"};
const AGAIN = `<button onclick="location.reload()">Try again</button>`;
const STOP = {
  connected: s => `<p class=ok>✓</p><h1>Connected</h1><p>Your Google Photos (${esc(s.email)}) is connected.
    You can close this page and go back to your assistant.</p>`,
  wrong_account: () => `<h1>That's a different Google account</h1><p>This assistant is already connected to another
    Google account. Sign in with that one, or ask the person who set it up to disconnect it first.</p>${AGAIN}`,
  in_use: () => `<h1>That Google account is someone else's here</h1><p>It is already connected for another person on
    this server. Sign in with your own Google account.</p>${AGAIN}`,
  empty_library: () => `<h1>Your library looks empty</h1><p>There was no photo to open, so nothing was connected.
    Add a photo to Google Photos and try again.</p>${AGAIN}`,
  busy: () => `<h1>The server's browser is busy</h1><p>Please try again in a few minutes.</p>${AGAIN}`,
  expired: () => `<h1>This link has expired</h1><p>Ask your assistant to connect Google Photos again for a new
    link.</p>`,
  limited: () => `<h1>Too many tries</h1><p>Please wait ten minutes and try again.</p>`,
  failed: () => `<h1>Something went wrong</h1><p>Nothing was connected.</p>${AGAIN}`,
  cancelled: () => `<h1>Cancelled</h1>${AGAIN}`,
};
function finish(html) {
  $("view").hidden = true;
  $("intro").hidden = true;
  document.querySelector("header .bar").hidden = true;
  const done = $("done"); done.innerHTML = html; done.hidden = false;
}
let viewer = false, over = false;
function show(s) {
  if (STOP[s.state]) {
    over = true;
    finish(STOP[s.state](s));
    return true;
  }
  if (s.host) $("host").textContent = (/(^|\\.)google\\.com$/.test(s.host) ? "🔒 " : "") + s.host;
  $("status").textContent = TEXT[s.state] || "";
  if (s.state === "saving") $("view").replaceChildren();  // the live view ends with Google's sign-in page
  if (s.state === "waiting" && !viewer) {
    viewer = true;
    const frame = document.createElement("iframe");
    const q = new URLSearchParams({autoconnect: 1, resize: "scale", reconnect: 1, reconnect_delay: 1500,
                                   path: "../vnc", show_dot: 1});  // relative to the viewer's own address
    frame.src = `${base}/novnc/vnc.html?${q}`;
    frame.title = "Google sign-in";
    frame.allow = "clipboard-read";
    $("view").appendChild(frame);
  }
  return false;
}
async function poll() {
  try {
    const r = await fetch(`${base}/state`, {cache: "no-store"});
    if (show(await r.json())) return;
  } catch (e) {}
  setTimeout(poll, 1500);
}
async function begin() {
  $("intro").hidden = true;
  document.querySelector("header .bar").hidden = false;
  $("status").textContent = TEXT.starting;
  const layout = Math.min(innerWidth, screen.width) < 700 ? "phone" : "desktop";
  try {
    if (show(await post("start", {layout}))) return;
  } catch (e) {}
  poll();
}
$("go").addEventListener("click", begin);
addEventListener("pagehide", () => { if (!over && navigator.sendBeacon) navigator.sendBeacon(`${base}/cancel`); });
</script>"""
