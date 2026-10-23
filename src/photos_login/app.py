"""The login service's API (signin-window's service). Only photos-mcp holds its bearer; a window's live view also needs
that window's token, and there is no direct view page.

POST   /logins                 {"account", "layout", "email", "taken"} -> 201 {id, state, view_token, ...} | 503 busy
GET    /logins/{id}            -> {id, account, state, host, layout} (+ "email" once connected)
DELETE /logins/{id}            -> 204 (closes the window, unless it is saving a finished sign-in)
WS     /logins/{id}/vnc?t=     -> the window's remote-desktop stream (RFB), for noVNC, while Google's page is up
POST   /accounts/{id}/refresh  -> 202 | 404 (not kept): refresh that account's snapshot soon
DELETE /accounts/{id}          -> 204: delete its browser profile and snapshot
GET    /novnc/...              -> noVNC's own files (the viewer)                                   (open)
GET    /health                 -> open

It never returns a session: snapshots go only to PHOTOS_LOGIN_SESSIONS, which photos-mcp reads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

from signin_window.service import Windows
from signin_window.service import build_app as service_app
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from photos_mcp.accounts import valid_id

from .keeper import Keeper
from .profiles import Paths
from .window import policy_installed
from .windows import SignIn

log = logging.getLogger("photos-login")

MAX_TAKEN = 100


def _emails(raw: object) -> list[str] | None:
    if not isinstance(raw, list) or len(raw) > MAX_TAKEN:
        return None
    out = []
    for value in raw:
        if not isinstance(value, str) or "@" not in value or len(value) > 254:
            return None
        out.append(value.strip().lower())
    return out


def start_params(require_policy: bool):  # type: ignore[no-untyped-def]
    """A start names a valid account, the only address it may sign in as (or none) and the addresses others hold."""

    def params(body: dict[str, Any]) -> dict[str, Any]:
        account, email = body.get("account"), body.get("email")
        taken = _emails(body.get("taken", []))
        if not valid_id(account) or taken is None or (email is not None and not _emails([email])):
            raise ValueError("invalid")
        if require_policy and not policy_installed():
            log.error("no sign-in window: the managed Chromium policy is not installed (window.POLICY_DIRS)")
            raise ValueError("policy")
        email = email.strip().lower() if isinstance(email, str) else None
        return {"account": account, "key": account, "email": email, "taken": taken}

    return params


def build_app(windows: Windows, keeper: Keeper, paths: Paths, browser_lock: threading.Lock, *, token: str,
              novnc_dir: Path, require_policy: bool = True) -> Starlette:  # fmt: skip
    async def refresh(request: Request) -> Response:
        account = request.path_params["account"]
        if not valid_id(account) or not await asyncio.to_thread(keeper.request, account):
            return JSONResponse({"error": "unknown"}, status_code=404)
        return Response(status_code=202)

    async def forget(request: Request) -> Response:
        account = request.path_params["account"]
        if not valid_id(account):
            return JSONResponse({"error": "unknown"}, status_code=404)
        await windows.close_key(account)

        def delete() -> bool:
            # Not while its window or a refresh holds the browser on that profile.
            if not browser_lock.acquire(timeout=120):
                return False
            try:
                paths.forget(account)
                return True
            finally:
                browser_lock.release()

        if not await asyncio.to_thread(delete):
            return JSONResponse({"error": "busy"}, status_code=503)
        log.info("account %s forgotten: browser profile and snapshot deleted", account)
        return Response(status_code=204)

    return service_app(
        windows,
        token=token,
        novnc_dir=novnc_dir,
        start_params=start_params(require_policy),
        routes=[
            Route("/accounts/{account}/refresh", refresh, methods=["POST"]),
            Route("/accounts/{account}", forget, methods=["DELETE"]),
        ],
        direct_view=False,
    )


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
        SignIn(paths, require_policy=not unmanaged),
        slots=1,
        minutes=_int("PHOTOS_LOGIN_MINUTES", 20, 5, 60),
        xvfb=xvfb,
        x11vnc=os.environ.get("PHOTOS_LOGIN_X11VNC") or "x11vnc",
        lock=browser_lock,
    )
    keeper = Keeper(
        paths,
        browser_lock,
        every=_int("PHOTOS_LOGIN_REFRESH_SECONDS", 3600, 600, 6 * 3600),
        xvfb=xvfb,
    )
    threading.Thread(target=keeper.run, args=(threading.Event(),), daemon=True, name="keeper").start()
    novnc = Path(os.environ.get("PHOTOS_LOGIN_NOVNC") or "/opt/novnc")
    app = build_app(windows, keeper, paths, browser_lock, token=token, novnc_dir=novnc, require_policy=not unmanaged)
    uvicorn.run(app, host="0.0.0.0", port=_int("PORT", 8080, 1, 65535), log_level="warning")
