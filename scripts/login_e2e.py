"""End-to-end check of sign-in links, with real browsers and no Google.

Runs the login service (``photos_login``) with a stand-in for Google's page (a local page with one text field, in a
headed Chromium on the window's virtual display) and the server (``photos_mcp``) mounted under ``/photos`` as behind
a path-stripping reverse proxy. Then, as the person:

1. asks ``photos_connect`` for a link (the proxy's bearer and account header), opens it in a headless Chromium,
   continues, waits for the live view, puts text on the clipboard, presses Ctrl+V and checks the text arrived in the
   remote field, typed (clipboard *reading* is not granted to the page, so it can only have come from the browser's
   own paste event); the window's browser (launched as the real window launches it, under the managed policy) must
   refuse local files, its own pages and DevTools; closing the page must close the window;
2. asks for a second link and continues: the stand-in "signs in" at once and writes a snapshot, the page must say
   connected, and the account must be bound to that address and serve its session.

Needs Xvfb, x11vnc, patchright's Chromium and noVNC's files (the login image has them all):

    python scripts/login_e2e.py                      # in the login image
    PHOTOS_LOGIN_XVFB=... PHOTOS_LOGIN_NOVNC=... python scripts/login_e2e.py
    PHOTOS_E2E_UNMANAGED=1 ...    # outside the image, without the policy: skips the policy checks

Exits 0 when everything worked. Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from patchright.async_api import async_playwright
from signin_window.service import Windows
from starlette.applications import Starlette
from starlette.routing import Mount

from photos_login import window
from photos_login.app import build_app
from photos_login.keeper import Keeper
from photos_login.profiles import Paths
from photos_login.windows import SignIn
from photos_mcp import accounts, session_export
from photos_mcp.auth import bearer_auth_from_env
from photos_mcp.config import Settings
from photos_mcp.server import HTTP_OPTIONS, Accounts, create_server
from photos_mcp.session import parse_cookies_txt

# Typed without the trailing newline. ASCII only: the virtual display's keyboard has no keys for other characters
# (x11vnc drops their keysyms), as for anyone typing them in the window.
TEXT = "Hello, Photos 123 @#%&*()[]{}<>?/|~ pass:Word_-+=.\n"
EXPECTED = TEXT.rstrip("\n") + "x"  # and a normal key afterwards
FIELD_PAGE = (
    "<!doctype html><title>stand-in</title>"
    "<input id=f autofocus style='position:fixed;inset:0;width:100%;height:100%;font-size:24px'>"
)
BEARER, LOGIN_TOKEN, HEADER = "e2e-bearer-0123456789abcdef", "e2e-login-token-0123456789abcdef0123", "X-E2E-Account"
OWNER, PERSON = "owner@example.com", "person@example.com"
COOKIES = (
    ".google.com\tTRUE\t/\tTRUE\t0\tSID\tfake-sid\n"
    "photos.fife.usercontent.google.com\tFALSE\t/\tTRUE\t0\tOSID\tfake-osid\n"
)
# What the window's browser must refuse (the managed policy): local files, other accounts' snapshots, DevTools.
PROBES = ("file:///etc/hostname", "file://{sessions}/", "devtools://devtools/bundled/inspector.html")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app, port: int) -> uvicorn.Server:  # type: ignore[no-untyped-def]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    asyncio.get_running_loop().create_task(server.serve())
    for _ in range(100):
        if server.started:
            return server
        await asyncio.sleep(0.05)
    raise SystemExit("a server did not start")


def mcp_call(url: str, name: str) -> dict:
    """A tool call as the authenticating proxy sends it for the person "e2e"."""
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}
    request = urllib.request.Request(url, data=json.dumps(call).encode(), method="POST")
    for header, value in {
        "Authorization": f"Bearer {BEARER}",
        HEADER: "e2e",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-03-26",
    }.items():
        request.add_header(header, value)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - the local server above
        answer = json.loads(response.read())
    try:
        return json.loads(answer["result"]["content"][0]["text"])
    except (KeyError, IndexError, ValueError):
        raise SystemExit(f"FAIL: {name} answered {answer}") from None


class StandIn:
    """Instead of Google's page. The first window: one text field until it is closed. The second: "signed in" at
    once, writing a snapshot of a made-up session the way the real window does."""

    def __init__(self, paths: Paths, *, managed: bool) -> None:
        self.paths = paths
        self.managed = managed
        self.calls = 0
        self.field_value: str | None = None
        self.focus = threading.Event()
        self.blocked: list[str] = []

    def __call__(self, *, profile, session_dir, layout, env, email, taken, min_generation, cancel, deadline, on_state,
                 require_policy):  # type: ignore[no-untyped-def]  # fmt: skip
        """window.sign_in's interface (photos_login.windows.SignIn runs it)."""
        from patchright.sync_api import sync_playwright

        self.calls += 1
        if self.calls > 1:
            on_state("saving", "photos.google.com")
            meta = session_export.build_meta(
                generation=min_generation + 1, email=PERSON, user_agent=UA, headers=None, verified_at=datetime.now(UTC)
            )
            session_export.publish(session_dir, parse_cookies_txt(COOKIES), meta)
            return {"email": PERSON, "generation": meta["generation"]}
        with sync_playwright() as pw:
            browser = window.launch(pw, profile, "desktop", env)
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                if self.managed:
                    probe = browser.new_page()
                    for url in PROBES:
                        try:
                            probe.goto(url.format(sessions=self.paths.sessions), timeout=15_000)
                        except Exception as exc:
                            if "ERR_BLOCKED_BY_ADMINISTRATOR" in str(exc):
                                self.blocked.append(url)
                        probe.wait_for_timeout(1500)  # let the error page settle before the next navigation
                    probe.close()
                page.set_content(FIELD_PAGE)
                on_state("waiting", "accounts.google.com")
                while not cancel.is_set():
                    if self.focus.is_set():
                        page.locator("#f").focus()
                        self.focus.clear()
                    self.field_value = page.locator("#f").input_value()
                    page.wait_for_timeout(100)
            finally:
                browser.close()
        raise window.Cancelled()


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="photos-e2e-"))
    login_port, mcp_port = free_port(), free_port()
    paths = Paths(profiles=root / "profiles", sessions=root / "sessions")
    managed = os.environ.get("PHOTOS_E2E_UNMANAGED") != "1"
    if managed and not window.policy_installed():
        print("FAIL: the managed Chromium policy is not installed", file=sys.stderr)
        return 1
    stand_in = StandIn(paths, managed=managed)
    lock = threading.Lock()
    windows = Windows(
        SignIn(paths, require_policy=managed, sign_in=stand_in),
        minutes=5,
        xvfb=os.environ.get("PHOTOS_LOGIN_XVFB") or "Xvfb",
        x11vnc=os.environ.get("PHOTOS_LOGIN_X11VNC") or "x11vnc",
        lock=lock,
    )
    keeper = Keeper(paths, lock, refresher=lambda **kw: {})
    novnc = Path(os.environ.get("PHOTOS_LOGIN_NOVNC") or "/opt/novnc")
    app = build_app(windows, keeper, paths, lock, token=LOGIN_TOKEN, novnc_dir=novnc, require_policy=managed)
    await serve(app, login_port)

    (root / "session").mkdir()
    base = f"http://127.0.0.1:{mcp_port}/photos"
    settings = Settings.from_env(
        {
            "GOOGLE_PHOTOS_EXPECTED_EMAIL": OWNER,
            "PHOTOS_SESSION_DIR": str(root / "session"),
            "PHOTOS_STATE_DIR": str(root / "state"),
            "PHOTOS_ACCOUNT_SESSIONS_DIR": str(paths.sessions),
            "PHOTOS_LOGIN_URL": f"http://127.0.0.1:{login_port}",
            "PHOTOS_LOGIN_TOKEN": LOGIN_TOKEN,
            "PHOTOS_PUBLIC_URL": base,
            "PHOTOS_TRUSTED_ACCOUNT_HEADER": HEADER,
        }
    )
    accounts.add(settings, "e2e", rpc_budget=100, thumb_budget=100)
    auth = bearer_auth_from_env({"MCP_BEARER_TOKEN": BEARER})
    inner = create_server(Accounts.from_settings(settings), auth=auth).http_app(**HTTP_OPTIONS)

    @contextlib.asynccontextmanager
    async def lifespan(_):  # type: ignore[no-untyped-def]
        async with inner.router.lifespan_context(inner):
            yield

    await serve(Starlette(routes=[Mount("/photos", app=inner)], lifespan=lifespan), mcp_port)

    link = await asyncio.to_thread(mcp_call, f"{base}/mcp", "photos_connect")
    assert link["account_id"] == "e2e" and link["url"].startswith(f"{base}/connect/"), link
    print("link made for the caller's account")

    async with async_playwright() as pw:
        viewer = await pw.chromium.launch(headless=True)
        context = await viewer.new_context(viewport={"width": 1280, "height": 800})
        await context.grant_permissions(["clipboard-write"], origin=f"http://127.0.0.1:{mcp_port}")
        page = await context.new_page()
        await page.goto(link["url"])
        await page.click("#go")  # the page names the account and waits for the person to continue
        frame_element = await page.wait_for_selector("iframe", timeout=60_000)
        frame = await frame_element.content_frame()
        assert frame is not None
        await frame.wait_for_function("document.documentElement.classList.contains('noVNC_connected')", timeout=60_000)
        print("live view connected through the link")
        canvas = await frame.wait_for_selector("#noVNC_container canvas")
        await canvas.click()  # focuses the viewer, and the remote field under the pointer
        stand_in.focus.set()
        await page.keyboard.press("y")  # plain typing reaches the field before the paste is tried
        for _ in range(50):
            if stand_in.field_value == "y":
                break
            await asyncio.sleep(0.1)
        await page.keyboard.press("Backspace")
        await page.evaluate("text => navigator.clipboard.writeText(text)", TEXT)
        await page.keyboard.press("Control+V")
        await asyncio.sleep(0.3 + 0.03 * len(TEXT))
        await page.keyboard.press("x")
        for _ in range(50):
            if stand_in.field_value == EXPECTED:
                break
            await asyncio.sleep(0.1)
        if stand_in.field_value != EXPECTED:
            print(f"FAIL: the remote field holds {stand_in.field_value!r}, expected {EXPECTED!r}", file=sys.stderr)
            return 1
        print("Ctrl+V typed the clipboard into the remote page (paste event; clipboard reading not granted)")
        if managed:
            if len(stand_in.blocked) != len(PROBES):
                print(f"FAIL: the window's browser opened local or internal pages: {stand_in.blocked}", file=sys.stderr)
                return 1
            print("the window's browser refuses local files and its own pages (managed policy)")

        await page.close(run_before_unload=True)  # pagehide sends the cancel
        for _ in range(300):  # closing ends Chromium, x11vnc and Xvfb, which can take a few seconds
            if not lock.locked() and all(login.ended_at for login in windows._windows.values()):
                break
            await asyncio.sleep(0.1)
        else:
            print("FAIL: closing the page did not close the sign-in window", file=sys.stderr)
            # Which part did not happen: the cancel never arrived (cancel unset), or the window was slow to end.
            for login in windows._windows.values():
                print(f"  window {login.id[:6]}: state={login.state} cancel={login.cancelled.is_set()} "
                      f"ended={login.ended_at is not None} error={login.error}", file=sys.stderr)  # fmt: skip
            print(f"  lock held: {lock.locked()}", file=sys.stderr)
            return 1
        print("closing the page closed the sign-in window")

        link = await asyncio.to_thread(mcp_call, f"{base}/mcp", "photos_connect")
        page = await context.new_page()
        await page.goto(link["url"])
        await page.click("#go")
        await page.wait_for_selector("text=Connected", timeout=60_000)
        shown = await page.inner_text("#done")
        if "p•••@example.com" not in shown:
            print(f"FAIL: the page shows {shown!r}", file=sys.stderr)
            return 1
        print("the second link connected the account")
        await viewer.close()

    if accounts.load(settings, "e2e").email != PERSON:
        print("FAIL: the account was not bound to the address it signed in as", file=sys.stderr)
        return 1
    status = await asyncio.to_thread(mcp_call, f"{base}/mcp", "photos_status")
    if status["session"].get("state") != "ok" or status["keeper"]["state"] != "signed_in":
        print(f"FAIL: photos_status says {status['session']} / {status.get('keeper')}", file=sys.stderr)
        return 1
    print("the account serves its new session")
    print("login e2e: ok")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
