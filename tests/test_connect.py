"""Sign-in links (connect.py): made only for the caller's account (never the default one, whose session is the
operator's), open without the bearer but only with the link's id, single use and short-lived; a sign-in binds the
account to the Google address it proved, never to another account's, and nothing here shows a session."""

from __future__ import annotations

import json
import re
import time

import pytest
from signin_window import relay as relay_module
from signin_window.client import STATIC, LoginBusy
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_accounts import HEADER, OTHER, PROXY, make_settings, publish, write_meta_default

from photos_mcp import accounts, connect, server
from photos_mcp.auth import bearer_auth_from_env
from photos_mcp.connections_auth import ConnectionsConfig

VNC_HTML = b"<!doctype html><html><head><script type=module>import UI from './app/ui.js';</script></head></html>"
LINK_ENV = {
    "PHOTOS_PUBLIC_URL": "https://example.com/photos",
    "PHOTOS_LOGIN_URL": "http://photos-mcp-login:8080",
    "PHOTOS_LOGIN_TOKEN": "l" * 32,
}


class FakeLogin:
    """The login service: every window waits for the person until a test ends it."""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.closed: list[str] = []
        self.forgotten: list[str] = []
        self.refreshed: list[str] = []
        self.states: dict[str, dict] = {}
        self.busy = False

    def start(self, layout: str = "desktop", *, account: str, email, taken, epoch=None) -> dict:
        if self.busy:
            raise LoginBusy("busy")
        login_id = f"login-{len(self.started):011d}"
        self.started.append({"account": account, "layout": layout, "email": email, "taken": taken})
        self.states[login_id] = {"state": "waiting", "host": "accounts.google.com"}
        return {"id": login_id, "state": "starting", "view_token": "v" * 24}

    def state(self, login_id: str) -> dict:
        return self.states.get(login_id, {"state": "unknown"})

    def close(self, login_id: str) -> None:
        self.closed.append(login_id)
        self.states.pop(login_id, None)

    def refresh(self, account: str) -> None:
        self.refreshed.append(account)

    def forget(self, account: str) -> None:
        self.forgotten.append(account)

    def result(self, login_id: str) -> None:
        return None  # the login service never hands a session over

    def static(self, path: str, window_id: str | None = None) -> tuple[int, bytes, str]:
        if not STATIC.match(path):
            return 404, b"", "text/plain"
        if path == "vnc.html":
            return 200, VNC_HTML, "text/html"
        return 200, b"export default {};", "text/javascript"

    async def bridge(self, login_id: str, view_token: str, ws) -> None:  # type: ignore[no-untyped-def]
        await ws.send_bytes(b"RFB 003.008\n")

    def end(self, login_id: str, state: str, email: str | None = None) -> None:
        self.states[login_id] = {"state": state, "host": "photos.google.com", **({"email": email} if email else {})}


@pytest.fixture
def login() -> FakeLogin:
    return FakeLogin()


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "session").mkdir()
    write_meta_default(tmp_path / "session")
    settings = make_settings(tmp_path, **LINK_ENV)
    accounts.add(settings, "second", rpc_budget=1000, thumb_budget=500)
    return settings


FLOWS: list[connect.ConnectFlow] = []


class RecordedFlow(connect.ConnectFlow):
    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        FLOWS.append(self)


@pytest.fixture
def http(settings, login, monkeypatch):
    FLOWS.clear()
    monkeypatch.setattr(server, "ConnectFlow", RecordedFlow)
    auth = bearer_auth_from_env({"MCP_BEARER_TOKEN": PROXY})
    registry = server.Accounts.from_settings(settings)
    config = ConnectionsConfig(
        "https://identity.example.com",
        "photos-ui",
        settings.public_url,
        {"subject-one": "second"},
        "photos-users",
        settings.state_dir,
    )
    mcp = server.create_server(registry, auth=auth, login=login, connections=config)
    portal = mcp.connections_ui
    sid = "s" * 43
    portal.auth.store.save(
        sid,
        {
            "issuer": config.issuer,
            "sub": "subject-one",
            "groups": [config.required_group],
            "claims": {},
            "expires": time.time() + 900,
        },
    )
    app = mcp.http_app(**server.HTTP_OPTIONS)
    with TestClient(app, base_url="https://example.com", headers={"origin": "https://example.com"}) as client:
        client.cookies.set(portal.auth.cookie, sid)
        yield client


def tool(client, name: str, person: str) -> dict:
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {PROXY}",
               "mcp-protocol-version": "2025-03-26", HEADER: person}  # fmt: skip
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": {}}}
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def link_path(link: dict) -> str:
    assert link["url"].startswith("https://example.com/photos/connect/"), link
    return link["url"].removeprefix("https://example.com/photos")


def owner_post(client, url, **kwargs):
    """Submit a current owner UI form; explicit data exercises stale forms."""
    if "data" not in kwargs:
        page = client.get(url.rsplit("/", 1)[0] + "/")
        match = re.search(r'<input type=hidden name=revision value="([^"]+)">', page.text)
        kwargs["data"] = {"revision": match[1]} if match else {}
    return client.post(url, **kwargs)


def owner_window(client):
    """An authenticated owner explicitly creates the expiring viewer attempt."""
    response = owner_post(client, "/connections/start", follow_redirects=False)
    if response.status_code == 429:
        return {"error": {"code": "connect_limited"}}
    assert response.status_code == 303, response.text
    return {"url": response.headers["location"]}


def wait_for(condition) -> None:  # type: ignore[no-untyped-def]
    for _ in range(100):
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


# -- the tool ---------------------------------------------------------------------------------------------------------


def test_the_link_is_for_the_callers_account_only(http, login):
    mine = tool(http, "photos_connect", "second")
    assert mine["account_id"] == "second" and mine["signed_in"] is False
    assert mine["expires_at"] is None and "nothing else" in mine["next_step"]
    assert mine["url"] == "https://example.com/photos/connections/"
    assert not FLOWS[0].relay.links("second")
    assert tool(http, "photos_connect", "main")["url"].endswith("/connections/")
    assert tool(http, "photos_connect", "missing")["error"]["code"] == "account_not_connected"
    assert login.started == []  # making a link opens nothing


def test_status_and_the_sign_in_errors_point_to_photos_connect(http):
    status = tool(http, "photos_status", "second")
    assert status["sign_in_links"] is True and status["session"] == {"state": "not_signed_in"}
    assert tool(http, "photos_status", "main")["sign_in_links"] is True
    error = tool(http, "photos_canary", "second")
    assert error["code"] == "not_signed_in" and error["ok"] is False
    error = tool(http, "photos_albums", "second")["error"]
    assert error["code"] == "not_signed_in" and "photos_connect" in error["next_step"]


def test_a_replaced_link_closes_its_window_and_opens_no_more(http, login):
    first = link_path(owner_window(http))
    assert http.post(first + "/start", json={}).json()["state"] == "starting"
    link_path(owner_window(http))
    wait_for(lambda: login.closed)
    assert login.closed == ["login-00000000000"]
    assert http.post(first + "/start", json={}).status_code == 404


def test_links_are_rate_limited_per_account(http):
    for _ in range(relay_module.Relay.__init__.__kwdefaults__["links_per_hour"]):
        owner_window(http)
    assert owner_window(http)["error"]["code"] == "connect_limited"


# -- the page ---------------------------------------------------------------------------------------------------------


def test_the_page_needs_no_bearer_and_is_never_cached_or_referred(http):
    path = link_path(owner_window(http))
    page = http.get(path)
    assert page.status_code == 200 and "Connect your Google Photos" in page.text
    for response in (page, http.get(path + "/state"), http.get("/connect/" + "x" * 32)):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    # It names the account it connects and opens nothing until the person continues (a link preview cannot start it).
    assert "<b>second</b>" in page.text and "Continue to Google" in page.text


@pytest.mark.parametrize("suffix", ["", "/state", "/novnc/vnc.html"])
def test_an_unknown_link_is_the_expired_page(http, suffix):
    response = http.get("/connect/" + "Z" * 32 + suffix)
    assert response.status_code == 404
    if not suffix:
        assert "expired" in response.text


def test_the_mcp_endpoint_still_needs_the_bearer(http):
    tool(http, "photos_connect", "second")
    response = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream", HEADER: "second"},
    )
    assert response.status_code == 401


def test_guessing_ids_is_slowed_down(http):
    headers = {"X-Forwarded-For": "203.0.113.7"}
    for _ in range(relay_module.MISSES_PER_ADDRESS):
        assert http.get("/connect/" + "Q" * 32 + "/state", headers=headers).status_code == 404
    assert http.get("/connect/" + "Q" * 32 + "/state", headers=headers).status_code == 429
    path = link_path(owner_window(http))
    # A real link always works, so nobody guessing can lock a person out of theirs.
    assert http.get(path, headers=headers).status_code == 200


def test_the_link_expires_and_closes_its_window(http, login, monkeypatch):
    path = link_path(owner_window(http))
    assert http.post(path + "/start", json={"layout": "phone"}).json()["state"] == "starting"
    for link in [x for flow in FLOWS for x in flow.relay.links("second")]:
        link.expires = 0  # past
    assert http.get(path).status_code == 404
    assert http.post(path + "/start", json={}).status_code == 404
    wait_for(lambda: login.closed)
    assert login.closed == ["login-00000000000"]


# -- signing in -------------------------------------------------------------------------------------------------------


def test_a_sign_in_binds_the_links_account_to_the_address_it_proved(http, login, settings):
    path = link_path(owner_window(http))
    assert http.post(path + "/start", json={"layout": "phone"}).json()["state"] == "starting"
    # The login service is told which address it may accept: any but the ones other accounts serve.
    assert login.started == [{"account": "second", "layout": "phone", "email": None, "taken": ["owner@example.com"]}]
    assert http.get(path + "/state").json() == {"state": "waiting", "host": "accounts.google.com"}
    login.end("login-00000000000", "saving")
    assert http.get(path + "/state").json()["state"] == "saving"
    publish(settings, "second", OTHER)  # the login service writes the snapshot itself
    login.end("login-00000000000", "connected", OTHER)
    done = http.get(path + "/state").json()
    assert done == {"state": "connected", "host": "photos.google.com", "email": "s•••@example.com"}
    assert accounts.load(settings, "second").email == OTHER
    status = tool(http, "photos_status", "second")
    assert status["session"]["state"] == "ok" and status["account_id"] == "second"
    # Single use: the page is gone and nothing more can start.
    assert http.get(path).status_code == 404
    assert http.post(path + "/start", json={}).json()["state"] == "connected"
    assert len(login.started) == 1
    # The next link for this account allows only that address.
    again = link_path(owner_window(http))
    http.post(again + "/start", json={})
    assert login.started[-1]["email"] == OTHER


@pytest.mark.parametrize(
    ("ending", "shown"),
    [("wrong_account", "wrong_account"), ("in_use", "in_use"), ("empty_library", "empty_library"),
     ("failed", "failed"), ("cancelled", "cancelled"), ("expired", "failed")],  # a window past its time: try again
)  # fmt: skip
def test_a_refused_or_failed_sign_in_binds_nothing(http, login, settings, ending, shown):
    path = link_path(owner_window(http))
    http.post(path + "/start", json={})
    login.end("login-00000000000", ending)
    assert http.get(path + "/state").json()["state"] == shown
    assert accounts.load(settings, "second").email is None


def test_an_unknown_ending_is_a_failure(http, login):
    path = link_path(owner_window(http))
    http.post(path + "/start", json={})
    login.states.clear()  # the login service forgot the window (restarted)
    assert http.get(path + "/state").json()["state"] == "failed"


def test_losing_the_binding_race_withdraws_the_new_sign_in(http, login, settings):
    accounts.add(settings, "third", rpc_budget=1, thumb_budget=1)
    path = link_path(owner_window(http))
    http.post(path + "/start", json={})
    accounts.bind_email(settings, "third", OTHER)  # bound meanwhile, after the login service checked
    login.end("login-00000000000", "connected", OTHER)
    assert http.get(path + "/state").json()["state"] == "in_use"
    assert accounts.load(settings, "second").email is None and login.forgotten == ["second"]


def test_closing_the_page_while_saving_keeps_the_sign_in(http, login, settings):
    path = link_path(owner_window(http))
    http.post(path + "/start", json={})
    login.end("login-00000000000", "saving")
    assert http.get(path + "/state").json()["state"] == "saving"
    assert http.post(path + "/cancel").status_code == 204
    assert login.closed == [] and http.get(path + "/state").json()["state"] == "saving"


def test_busy_cancel_and_the_start_limit(http, login):
    path = link_path(owner_window(http))
    login.busy = True
    assert http.post(path + "/start", json={}).json()["state"] == "busy"
    login.busy = False
    assert http.post(path + "/start", json={}).json()["state"] == "starting"
    assert http.post(path + "/start", json={}).json()["state"] == "starting"  # no second window
    assert http.post(path + "/cancel").status_code == 204
    assert login.closed == ["login-00000000000"]
    assert http.get(path + "/state").json()["state"] == "cancelled"
    for _ in range(relay_module.Relay.__init__.__kwdefaults__["max_starts"] - 2):
        http.post(path + "/start", json={})
        http.post(path + "/cancel")
    assert http.post(path + "/start", json={}).json()["state"] == "cancelled"  # used up
    assert http.get(path).status_code == 404


# -- the viewer -------------------------------------------------------------------------------------------------------


def test_the_viewer_is_novnc_plus_the_paste_add_on(http):
    path = link_path(owner_window(http))
    page = http.get(path + "/novnc/vnc.html?autoconnect=1")
    assert page.status_code == 200 and page.headers["x-frame-options"] == "SAMEORIGIN"
    assert page.content == VNC_HTML.replace(b"</head>", relay_module.PASTE_TAG + b"\n</head>")
    paste = http.get(path + "/novnc/paste.js")
    assert paste.status_code == 200 and paste.headers["content-type"].startswith("text/javascript")
    for bad in ("/novnc/../vnc.html", "/novnc/.hidden", "/novnc/a/../../x"):
        assert http.get(path + bad).status_code == 404, bad


def test_the_live_view_needs_an_open_window(http):
    path = link_path(owner_window(http))
    with pytest.raises(WebSocketDisconnect), http.websocket_connect(path + "/vnc") as ws:
        ws.receive_bytes()
    http.post(path + "/start", json={})
    http.get(path + "/state")
    with http.websocket_connect(path + "/vnc", subprotocols=["binary"]) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"


def test_the_live_view_ends_with_the_sign_in_page(http, login):
    # Before Google's sign-in page is up (the profile may still be signed in) and while the snapshot is written,
    # nobody, the link's holder included, may watch the window.
    path = link_path(owner_window(http))
    http.post(path + "/start", json={})
    login.end("login-00000000000", "saving")
    http.get(path + "/state")
    with pytest.raises(WebSocketDisconnect), http.websocket_connect(path + "/vnc") as ws:
        ws.receive_bytes()


def test_link_requests_do_not_replace_owner_attempt_or_resume_disconnect(http, login):
    path = link_path(owner_window(http))
    http.post(path + "/start", json={})
    before = tuple(FLOWS[0].relay.links("second"))
    for _ in range(10):
        assert tool(http, "photos_connect", "second")["expires_at"] is None
    assert tuple(FLOWS[0].relay.links("second")) == before
    assert login.closed == []
    assert owner_post(http, "/connections/disconnect", follow_redirects=False).status_code == 303
    recovery = tool(http, "photos_connect", "second")
    assert recovery["url"].endswith("/connections/")
    assert http.get("/connections/status").json()["state"] == "disconnected"
