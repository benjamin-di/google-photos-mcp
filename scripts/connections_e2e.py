"""Pocket ID passkeys, Photos connection screens and real noVNC; disposable accounts only.

Requires the Linux display/browser dependencies of login_e2e.py, plus POCKET_ID_BIN
pointing to the checksum-verified Pocket ID 2.19.1 release binary. Never uses Google.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx2
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
from photos_maintenance.cli import stores
from photos_maintenance.human import Config as HumanConfig
from photos_maintenance.human import build_app as human_app
from photos_maintenance.human_state import NativePromotion
from photos_maintenance.outcomes import HoldReason
from photos_mcp import accounts, session_export
from photos_mcp.config import Settings
from photos_mcp.connections import Connections
from photos_mcp.connections_auth import ConnectionsConfig
from photos_mcp.login_client import LoginClient, RoutedLoginClient
from photos_mcp.session import parse_cookies_txt, read_snapshot

# Official Pocket ID 2.19.1 Linux amd64 release checksum, not an application secret.
SHA256 = "fbe58b3af6e225600136c7da033810d2d86436fbd13a135fa52205f467544436"


def port(kind=socket.SOCK_STREAM):
    with socket.socket(type=kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for(check, seconds=30):
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, "acceptance deadline exceeded"
        await asyncio.sleep(0.05)


def serve(app, listen):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=listen, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


class SyntheticSignIn:
    def __init__(self):
        self.saving = threading.Event()
        self.publish = threading.Event()

    def __call__(self, *, profile, session_dir, layout, env, email, min_generation, cancel, deadline,
                 on_state, **kwargs):  # fmt: skip
        from patchright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = window.launch(pw, profile, layout, env)
            try:
                markup = (
                    '<button autofocus style="position:fixed;inset:0;width:100%;height:100%;font-size:28px" '
                    'onclick="document.body.dataset.finished=1">Connect synthetic Photos</button>'
                )
                browser.route(
                    "**/*",
                    lambda route: (
                        route.fulfill(content_type="text/html", body=markup)
                        if route.request.url == "https://accounts.google.com/"
                        else route.abort()
                    ),
                )
                page = browser.pages[0]
                page.goto("https://accounts.google.com/")
                page.bring_to_front()
                page.locator("button").focus()
                on_state("waiting", "accounts.google.com")
                while page.locator("body").get_attribute("data-finished") != "1":
                    if cancel.is_set() or time.monotonic() > deadline:
                        raise window.Cancelled()
                    page.wait_for_timeout(50)
                on_state("saving", "photos.google.com")
                self.saving.set()
                assert self.publish.wait(15), "synthetic save gate was not released"
                address = email or profile.name + "@example.com"
                meta = session_export.build_meta(
                    generation=min_generation + 1,
                    email=address,
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        f"Chrome/{browser.browser.version} Safari/537.36"
                    ),
                    headers=None,
                    verified_at=datetime.now(UTC),
                )
                cookies = parse_cookies_txt(".google.com\tTRUE\t/\tTRUE\t0\tSID\tfake-session-cookie\n")
                session_export.publish(session_dir, cookies, meta)
                return {"email": address, "generation": meta["generation"]}
            finally:
                browser.close()


async def run():
    # DNS denial backs up request interception, including browser background traffic.
    local_only = ("--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1")
    window.LAUNCH_ARGS = (*window.LAUNCH_ARGS, *local_only)
    binary = Path(os.environ["POCKET_ID_BIN"])
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == SHA256, "Pocket ID binary checksum mismatch"
    with tempfile.TemporaryDirectory(prefix="connections-acceptance-") as temp:
        root = Path(temp)
        ports = set()
        while len(ports) < 4:
            ports.add(port())
        identity_port, login_port, ui_port, native_port = ports
        actors_port = port(socket.SOCK_DGRAM)
        while actors_port in ports:
            actors_port = port(socket.SOCK_DGRAM)
        issuer, origin = f"http://localhost:{identity_port}", f"http://127.0.0.1:{ui_port}"
        api_key, login_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        provider_dir = root / "identity"
        provider_dir.mkdir(mode=0o700)
        provider_log = (root / "identity.log").open("wb")
        env = {name: os.environ[name] for name in ("PATH", "LANG", "TZ") if name in os.environ}
        env.update(
            APP_URL=issuer,
            HOST="127.0.0.1",
            PORT=str(identity_port),
            ACTORS_HOST="127.0.0.1",
            ACTORS_PORT=str(actors_port),
            DB_CONNECTION_STRING=str(provider_dir / "provider.db"),
            ENCRYPTION_KEY=secrets.token_urlsafe(32),
            STATIC_API_KEY=api_key,
            ANALYTICS_DISABLED="true",
            VERSION_CHECK_DISABLED="true",
            ALLOW_INSECURE_CALLBACK_URLS="true",
            DISABLE_RATE_LIMITING="true",
        )
        process = subprocess.Popen([str(binary)], cwd=provider_dir, env=env, stdout=provider_log, stderr=provider_log)
        servers = []
        try:
            with httpx2.Client(base_url=issuer, headers={"X-API-Key": api_key}, timeout=10, trust_env=False) as admin:
                for _ in range(200):
                    try:
                        if admin.get("/api/application-configuration").status_code == 200:
                            break
                    except httpx2.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("Disposable Pocket ID did not start")

                def post(url, body):
                    response = admin.post(url, json=body)
                    assert response.status_code in (200, 201), (url, response.status_code, response.text[:300])
                    return response.json()

                group = post("/api/user-groups", {"name": "photos-users", "friendlyName": "Photos users"})
                users = []
                for number in (1, 2, 3):
                    user = post(
                        "/api/users",
                        {
                            "username": f"example{number}",
                            "firstName": "Example",
                            "lastName": str(number),
                            "email": f"example{number}@example.com",
                            "isAdmin": False,
                        },
                    )
                    response = admin.put(f"/api/users/{user['id']}/user-groups", json={"userGroupIds": [group["id"]]})
                    assert response.status_code in (200, 204), response.text[:200]
                    account = {1: "first", 2: "second", 3: "default"}[number]
                    response = admin.put(
                        f"/api/custom-claims/user/{user['id']}", json=[{"key": "account_alias", "value": account}]
                    )
                    assert response.status_code == 200, response.text[:200]
                    users.append(user)
                post(
                    "/api/oidc/clients",
                    {
                        "id": "photos-ui",
                        "name": "Photos connections",
                        "callbackURLs": [origin + "/connections/callback"],
                        "isPublic": True,
                        "pkceEnabled": True,
                        "launchURL": origin + "/connections/",
                    },
                )

                paths = Paths(root / "profiles", root / "sessions")
                lock = threading.Lock()
                synthetic = SyntheticSignIn()
                windows = Windows(
                    SignIn(paths, require_policy=False, sign_in=synthetic),
                    lock=lock,
                    xvfb=os.environ.get("PHOTOS_LOGIN_XVFB", "Xvfb"),
                    x11vnc=os.environ.get("PHOTOS_LOGIN_X11VNC", "x11vnc"),
                )
                login_app = build_app(
                    windows,
                    Keeper(paths, lock, refresher=lambda **kw: {}),
                    paths,
                    lock,
                    token=login_key,
                    novnc_dir=Path(os.environ["PHOTOS_LOGIN_NOVNC"]),
                    require_policy=False,
                )
                servers.append(serve(login_app, login_port))
                settings = Settings(
                    expected_email="operator@example.com",
                    state_dir=root / "state",
                    session_dir=root / "default",
                    account_sessions_dir=paths.sessions,
                    public_url=origin,
                    login_url=f"http://127.0.0.1:{login_port}",
                    default_login_url=f"http://127.0.0.1:{native_port}",
                    login_token=login_key,
                )
                for name in ("first", "second"):
                    accounts.add(settings, name, rpc_budget=500, thumb_budget=100)
                config = ConnectionsConfig(
                    issuer,
                    "photos-ui",
                    origin,
                    {},
                    "photos-users",
                    root / "state",
                    True,
                    account_claim="account_alias",
                )
                portal = Connections(
                    settings,
                    RoutedLoginClient(
                        LoginClient(settings.login_url, login_key),
                        LoginClient(settings.default_login_url, login_key),
                        settings.default_account,
                    ),
                    config,
                )
                native_config = HumanConfig(
                    settings.default_account,
                    root / "maintenance",
                    settings.session_dir,
                    root / "state" / "connections",
                    settings.expected_email,
                )
                native = NativePromotion(native_config.root, native_config.export, native_config.guard())
                native.profile.mkdir(mode=0o700, parents=True)
                (native.profile / "synthetic-native-identity").write_text("accepted native profile")
                cookies = parse_cookies_txt(".google.com\tTRUE\t/\tTRUE\t0\tSID\tfake-old-session\n")
                meta = session_export.build_meta(
                    generation=7,
                    email=settings.expected_email,
                    user_agent="Chrome/142.0.0.0",
                    headers=None,
                    verified_at=datetime.fromtimestamp(0, UTC),
                )
                session_export.publish(settings.session_dir, cookies, meta)
                (settings.session_dir / "keeper.json").write_text('{"state":"signed_out","generation":7}')
                native_state, native_budget = stores(native_config.root)
                native_state.record_hold("google", HoldReason.SIGN_IN_BLOCKED)
                assert native_budget.reserve("google", limit=3)
                consumed = (native_config.root / "session-keeper" / "google.json").read_bytes()
                native_synthetic = SyntheticSignIn()
                servers.append(
                    serve(
                        human_app(
                            native_config,
                            token=login_key,
                            novnc_dir=Path(os.environ["PHOTOS_LOGIN_NOVNC"]),
                            sign_in=native_synthetic,
                            require_policy=False,
                        ),
                        native_port,
                    )
                )

                @contextlib.asynccontextmanager
                async def lifespan(app):
                    async with portal.flow.relay.lifespan():
                        yield

                app = Starlette(
                    routes=[Mount("/connections", app=portal.app()), Mount("/connect", app=portal.viewer_app())],
                    lifespan=lifespan,
                )
                servers.append(serve(app, ui_port))
                await wait_for(lambda: all(server.started for server, _ in servers))
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(
                        executable_path=os.environ["PHOTOS_TEST_CHROME"], headless=True, args=list(local_only)
                    )
                    contexts = []
                    try:
                        for user in users:
                            context = await browser.new_context(
                                viewport={"width": 390, "height": 844} if contexts else {"width": 1280, "height": 800}
                            )
                            contexts.append(context)
                            page = await context.new_page()
                            cdp = await context.new_cdp_session(page)
                            await cdp.send("WebAuthn.enable")
                            authenticator = await cdp.send(
                                "WebAuthn.addVirtualAuthenticator",
                                {
                                    "options": {
                                        "protocol": "ctap2",
                                        "transport": "internal",
                                        "hasResidentKey": True,
                                        "hasUserVerification": True,
                                        "isUserVerified": True,
                                        "automaticPresenceSimulation": True,
                                    }
                                },
                            )
                            token = post(f"/api/users/{user['id']}/one-time-access-token", {})["token"]
                            response = await context.request.post(issuer + "/api/one-time-access-token/" + token)
                            assert response.ok
                            await page.goto(issuer + "/settings/account")
                            await page.get_by_role("button", name=re.compile("add passkey", re.I)).first.click()
                            await page.wait_for_timeout(700)
                            credentials = await cdp.send(
                                "WebAuthn.getCredentials", {"authenticatorId": authenticator["authenticatorId"]}
                            )
                            assert len(credentials["credentials"]) == 1
                            # Confirm enrollment through the provider API.
                            response = admin.get(f"/api/users/{user['id']}/webauthn-credentials")
                            assert response.status_code == 200 and response.json(), "Passkey enrollment did not finish"
                            await context.clear_cookies()
                            await page.goto(origin + "/connections/")
                            await page.get_by_role("link", name="Continue with your identity provider").click()
                            for _ in range(6):
                                await page.wait_for_timeout(500)
                                if page.url == origin + "/connections/":
                                    break
                                button = page.get_by_role(
                                    "button", name=re.compile(r"^(Authenticate|Sign in|Allow|Authorize)$")
                                )
                                if await button.count() and await button.first.is_visible():
                                    await button.first.click()
                            await page.wait_for_url(origin + "/connections/", timeout=20000)
                            assert "Sign-in required" in await page.content()
                            used = await cdp.send(
                                "WebAuthn.getCredentials", {"authenticatorId": authenticator["authenticatorId"]}
                            )
                            assert used["credentials"][0]["signCount"] > credentials["credentials"][0]["signCount"]
                            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                            print("PASS: Pocket ID passkey and OIDC owner binding", flush=True)

                        first, second, default = contexts
                        page = first.pages[0]
                        async with page.expect_response(
                            lambda response: response.url.endswith("/connections/start")
                        ) as posted:
                            await page.get_by_role("button", name="Connect", exact=True).click()
                        started = await posted.value
                        assert started.status == 303, (started.status, started.request.headers.get("origin"))
                        await page.wait_for_url(origin + "/connect/*")
                        link = page.url
                        other = await second.request.get(link)
                        assert other.status == 404
                        assert (await second.request.get(link + "/state")).status == 404
                        assert (await second.request.post(link + "/start", headers={"Origin": origin})).status == 404
                        await page.get_by_role("button", name="Continue to Google's sign-in").click()
                        frame = page.frame_locator("iframe")
                        try:
                            await frame.locator("html.noVNC_connected").wait_for(timeout=30000)
                        except Exception:
                            print("WINDOW", [(w.state, w.error) for w in windows._windows.values()], flush=True)
                            print("SCREEN", (await page.locator("body").inner_text())[:900], flush=True)
                            raise
                        await page.reload()
                        frame = page.frame_locator("iframe")
                        await frame.locator("html.noVNC_connected").wait_for(timeout=20000)
                        await frame.locator("#noVNC_transition").wait_for(state="hidden")
                        await frame.locator("canvas").click(position={"x": 300, "y": 300})
                        await page.keyboard.press("Enter")
                        await wait_for(synthetic.saving.is_set)
                        await page.close()
                        synthetic.publish.set()
                        await wait_for(lambda: (paths.session("first") / "meta.json").exists())
                        page = await first.new_page()
                        await page.goto(origin + "/connections/")
                        await page.get_by_text("Connection saved", exact=True).wait_for()
                        assert "fake-session-cookie" not in await page.content()
                        assert not (paths.session("second") / "meta.json").exists()
                        print("PASS: real viewer, reload, owner isolation and saving after page closure", flush=True)
                        # A new UI derives connection health from the saved snapshot.
                        replacement = Connections(settings, portal.client, config)
                        assert replacement.status({"account": "first"})["state"] == "connected"
                        await page.get_by_role("button", name="Disconnect Google Photos", exact=True).click()
                        await page.get_by_text("Disconnected", exact=True).wait_for()
                        assert not (paths.session("first") / "meta.json").exists()
                        assert not replacement.account_allowed("first")
                        print("PASS: restart recovery and durable disconnect", flush=True)
                        # The existing native default account must recover through its owner's browser too.
                        native_page = default.pages[0]
                        await native_page.get_by_role("button", name="Connect", exact=True).click()
                        await native_page.wait_for_url(origin + "/connect/*")
                        native_link = native_page.url
                        assert (await first.request.get(native_link)).status == 404
                        assert (
                            await second.request.post(native_link + "/start", headers={"Origin": origin})
                        ).status == 404
                        await native_page.get_by_role("button", name="Continue to Google's sign-in").click()
                        native_frame = native_page.frame_locator("iframe")
                        await native_frame.locator("html.noVNC_connected").wait_for(timeout=30000)
                        await native_page.reload()
                        native_frame = native_page.frame_locator("iframe")
                        await native_frame.locator("html.noVNC_connected").wait_for(timeout=20000)
                        await native_frame.locator("#noVNC_transition").wait_for(state="hidden")
                        if await native_frame.locator("#noVNC_control_bar").evaluate(
                            "element => element.classList.contains('noVNC_open')"
                        ):
                            await native_frame.locator("#noVNC_control_bar_handle").click()
                        if os.environ.get("CONNECTIONS_NATIVE_SCREENSHOT"):
                            await native_page.screenshot(path=os.environ["CONNECTIONS_NATIVE_SCREENSHOT"])
                        await native_frame.locator("canvas").click()
                        await native_page.keyboard.press("Enter")
                        await wait_for(native_synthetic.saving.is_set)
                        await native_page.close()
                        native_synthetic.publish.set()
                        await wait_for(lambda: read_snapshot(settings.session_dir).generation == 8)
                        native_page = await default.new_page()
                        await native_page.goto(origin + "/connections/")
                        await native_page.get_by_text("Connection saved", exact=True).wait_for()
                        assert native_state.load("google").hold is None
                        assert (native_config.root / "session-keeper" / "google.json").read_bytes() == consumed
                        assert (native.profile / "synthetic-native-identity").read_text() == "accepted native profile"
                        assert not (paths.sessions / settings.default_account).exists()
                        assert not (settings.state_dir / "accounts" / settings.default_account).exists()
                        assert replacement.status({"account": settings.default_account})["state"] == "connected"
                        await native_page.get_by_role("button", name="Disconnect Google Photos", exact=True).click()
                        await native_page.get_by_text("Disconnected", exact=True).wait_for()
                        assert read_snapshot(settings.session_dir).generation == 8
                        assert native.profile.is_dir() and not replacement.account_allowed(settings.default_account)
                        assert "browser profile is retained" in await native_page.content()
                        print(
                            "PASS: expired native default owner recovery, kept profile, account isolation and ledger",
                            flush=True,
                        )
                        await page.screenshot(
                            path=os.environ.get("CONNECTIONS_SCREENSHOT", "/tmp/connections-screen.png")
                        )
                    finally:
                        await browser.close()
        finally:
            for server, _ in servers:
                server.should_exit = True
            for _, thread in servers:
                await asyncio.to_thread(thread.join, 20)
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
            provider_log.close()


if __name__ == "__main__":
    asyncio.run(run())
