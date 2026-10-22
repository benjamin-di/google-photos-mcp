"""The login service's API. Only photos-mcp holds its bearer; a window's live view also needs that window's token.

POST   /logins                 {"account", "layout", "email", "taken"} -> 201 {id, state, view_token, ...} | 503 busy
GET    /logins/{id}            -> {id, account, state, host, layout} (+ "email" once connected)
DELETE /logins/{id}            -> 204 (closes the window)
WS     /logins/{id}/vnc?t=     -> the window's remote-desktop stream (RFB), for noVNC
POST   /accounts/{id}/refresh  -> 202 | 404 (not kept): refresh that account's snapshot soon
DELETE /accounts/{id}          -> 204: delete its browser profile and snapshot
GET    /novnc/...              -> noVNC's own files (the viewer)                                   (open)
GET    /health                 -> open

It never returns a session: snapshots go only to PHOTOS_LOGIN_SESSIONS, which photos-mcp reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import threading
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from photos_mcp.accounts import valid_id

from .keeper import Keeper
from .profiles import Paths
from .window import PolicyMissing, policy_installed
from .windows import VIEWABLE, Busy, Windows

log = logging.getLogger("photos-login")

MAX_TAKEN = 100


class BearerOnly:
    """Every route but /health and /novnc needs the bearer, checked before any WebSocket handshake too."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.expected = app, f"Bearer {token}".encode()

    @staticmethod
    def _open(scope: Scope) -> bool:
        path = scope.get("path") or ""
        return scope["type"] == "http" and (path == "/health" or path.startswith("/novnc/"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not self._open(scope):
            given = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4401})
                else:
                    await Response(status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _emails(raw: object) -> list[str] | None:
    if not isinstance(raw, list) or len(raw) > MAX_TAKEN:
        return None
    out = []
    for value in raw:
        if not isinstance(value, str) or "@" not in value or len(value) > 254:
            return None
        out.append(value.strip().lower())
    return out


def build_app(windows: Windows, keeper: Keeper, *, token: str, novnc_dir: Path) -> Starlette:
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "healthy"})

    async def start(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid"}, status_code=400)
        account, layout, email = body.get("account"), body.get("layout"), body.get("email")
        taken = _emails(body.get("taken", []))
        if not valid_id(account) or taken is None or (email is not None and not _emails([email])):
            return JSONResponse({"error": "invalid"}, status_code=400)
        try:
            login = await asyncio.to_thread(
                windows.start,
                account,
                layout if layout in ("phone", "desktop") else "desktop",
                email=email.strip().lower() if isinstance(email, str) else None,
                taken=taken,
            )
        except Busy:
            return JSONResponse({"error": "busy"}, status_code=503)
        except PolicyMissing:
            log.error("no sign-in window: the managed Chromium policy is not installed (window.POLICY_DIRS)")
            return JSONResponse({"error": "policy"}, status_code=500)
        except RuntimeError:
            return JSONResponse({"error": "display"}, status_code=500)
        return JSONResponse({**login.public(), "view_token": login.view_token}, status_code=201)

    async def state(request: Request) -> JSONResponse:
        login = windows.get(request.path_params["id"])
        if login is None:
            return JSONResponse({"error": "unknown"}, status_code=404)
        return JSONResponse(login.public())

    async def close(request: Request) -> Response:
        windows.close(request.path_params["id"])
        return Response(status_code=204)

    async def refresh(request: Request) -> Response:
        account = request.path_params["account"]
        if not valid_id(account) or not await asyncio.to_thread(keeper.request, account):
            return JSONResponse({"error": "unknown"}, status_code=404)
        return Response(status_code=202)

    async def forget(request: Request) -> Response:
        account = request.path_params["account"]
        if not valid_id(account):
            return JSONResponse({"error": "unknown"}, status_code=404)
        windows.close_account(account)

        def delete() -> bool:
            # Not while its window or a refresh holds the browser on that profile.
            if not windows.browser_lock.acquire(timeout=120):
                return False
            try:
                windows.paths.forget(account)
                return True
            finally:
                windows.browser_lock.release()

        if not await asyncio.to_thread(delete):
            return JSONResponse({"error": "busy"}, status_code=503)
        log.info("account %s forgotten: browser profile and snapshot deleted", account)
        return Response(status_code=204)

    async def vnc(ws: WebSocket) -> None:
        # Only while Google's sign-in page is up: the window first checks (and at the end exports from) a profile
        # that may be signed in, and nobody, the link's holder included, may watch or steer that.
        login = windows.get(ws.path_params["id"])
        if login is None or not login.view_ok(ws.query_params.get("t", "")) or login.state != VIEWABLE:
            await ws.close(code=4404)
            return
        await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", login.vnc_port)
        except OSError:
            await ws.close(code=1011)
            return

        async def upstream() -> None:
            try:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    writer.write(message.get("bytes") or (message.get("text") or "").encode())
                    await writer.drain()
            except (WebSocketDisconnect, ConnectionError):
                return

        async def downstream() -> None:
            try:
                while data := await reader.read(65536):
                    await ws.send_bytes(data)
            except (WebSocketDisconnect, ConnectionError, RuntimeError):
                return

        async def watch() -> None:
            while login.state == VIEWABLE:
                await asyncio.sleep(0.25)

        tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream()), asyncio.create_task(watch())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        writer.close()
        with contextlib.suppress(RuntimeError):
            await ws.close(code=4404)

    routes = [
        Route("/health", health),
        Route("/logins", start, methods=["POST"]),
        Route("/logins/{id}", state, methods=["GET"]),
        Route("/logins/{id}", close, methods=["DELETE"]),
        WebSocketRoute("/logins/{id}/vnc", vnc),
        Route("/accounts/{account}/refresh", refresh, methods=["POST"]),
        Route("/accounts/{account}", forget, methods=["DELETE"]),
    ]
    if novnc_dir.is_dir():
        routes.append(Mount("/novnc", app=StaticFiles(directory=novnc_dir)))
    return Starlette(routes=routes, middleware=[Middleware(BearerOnly, token=token)])


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise SystemExit(f"{name} must be between {minimum} and {maximum}")
    return value


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    token = (os.environ.get("PHOTOS_LOGIN_TOKEN") or "").strip()
    if len(token) < 32:
        raise SystemExit("PHOTOS_LOGIN_TOKEN (at least 32 characters) is required")
    paths = Paths(
        profiles=Path(os.environ.get("PHOTOS_LOGIN_PROFILES") or "/profiles"),
        sessions=Path(os.environ.get("PHOTOS_LOGIN_SESSIONS") or "/sessions"),
    )
    for folder in (paths.profiles, paths.sessions):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    browser_lock = threading.Lock()
    xvfb = os.environ.get("PHOTOS_LOGIN_XVFB") or "Xvfb"
    # Only for trying it out without the image: the window can then reach local files (window.py).
    unmanaged = (os.environ.get("PHOTOS_LOGIN_UNMANAGED_BROWSER") or "").strip() == "1"
    if not unmanaged and not policy_installed():
        log.error("the managed Chromium policy is not installed: sign-in windows stay closed (window.POLICY_DIRS)")
    windows = Windows(
        paths,
        browser_lock,
        slots=1,
        minutes=_int("PHOTOS_LOGIN_MINUTES", 20, 5, 60),
        xvfb=xvfb,
        x11vnc=os.environ.get("PHOTOS_LOGIN_X11VNC") or "x11vnc",
        require_policy=not unmanaged,
    )
    keeper = Keeper(
        paths,
        browser_lock,
        every=_int("PHOTOS_LOGIN_REFRESH_SECONDS", 3600, 600, 6 * 3600),
        xvfb=xvfb,
    )
    threading.Thread(target=keeper.run, args=(threading.Event(),), daemon=True, name="keeper").start()
    novnc = Path(os.environ.get("PHOTOS_LOGIN_NOVNC") or "/opt/novnc")
    app = build_app(windows, keeper, token=token, novnc_dir=novnc)
    uvicorn.run(app, host="0.0.0.0", port=_int("PORT", 8080, 1, 65535), log_level="warning")
