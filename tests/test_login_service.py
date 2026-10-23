"""The login service (photos_login): its API is behind its bearer and never returns a session; a window ends
connected only with an address the account may use (else the profile is wiped); the keeper refreshes each signed-in
account, withdraws the snapshot only on proof of a signed-out profile, and never signs in. Nothing here starts a
browser or a display."""

from __future__ import annotations

import ast
import json
import threading
import time
from pathlib import Path

import pytest
from conftest import EMAIL, write_meta
from signin_window.service import Windows
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from photos_login import keeper as keeper_mod
from photos_login import window
from photos_login.app import build_app
from photos_login.keeper import Keeper
from photos_login.profiles import ERROR, SIGNED_IN, SIGNED_OUT, Paths
from photos_login.windows import SignIn
from photos_mcp.session_export import ExportError

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LOGIN = Path(__file__).resolve().parents[1] / "src" / "photos_login"
SAMPLE = Path(__file__).parent / "fixtures" / "session" / "cookies.txt"


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(profiles=tmp_path / "profiles", sessions=tmp_path / "sessions")


def snapshot(paths: Paths, account: str, generation: int) -> dict:
    folder = paths.session(account)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cookies.txt").write_text(SAMPLE.read_text())
    write_meta(folder, generation, EMAIL)
    return {"email": EMAIL, "generation": generation}


class Script:
    """A sign-in standing in for the browser (window.sign_in's interface): does what the test says, when it says."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.go = threading.Event()
        self.saved = threading.Event()  # set: the snapshot write finishes
        self.saved.set()
        self.outcome: object = None
        self.calls: list[tuple] = []

    def __call__(self, *, profile, session_dir, layout, env, email, taken, min_generation, cancel, deadline, on_state,
                 require_policy):  # type: ignore[no-untyped-def]  # fmt: skip
        account = session_dir.name
        self.calls.append((account, email, taken, min_generation))
        profile.mkdir(parents=True, exist_ok=True)
        on_state("waiting", "accounts.google.com")
        while not self.go.wait(0.01):
            if cancel.is_set():
                raise window.Cancelled()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        on_state("saving", "photos.google.com")
        self.saved.wait(5)
        return snapshot(self.paths, account, min_generation + 1)


def service(paths: Paths, script: Script, lock: threading.Lock | None = None, *, require_policy: bool = False):
    """The login service with ``script`` instead of a browser and no display: (its windows, a client)."""
    lock = lock or threading.Lock()
    windows = Windows(SignIn(paths, require_policy=require_policy, sign_in=script), slots=1, lock=lock, lock_wait=0.2)
    windows.start_display = lambda login: None  # type: ignore[method-assign]
    windows.stop_display = lambda login: login.procs.clear()  # type: ignore[method-assign]
    keeper = Keeper(paths, lock, refresher=lambda **kw: {})
    app = build_app(windows, keeper, paths, lock, token=TOKEN, novnc_dir=Path("/nonexistent"),
                    require_policy=require_policy)  # fmt: skip
    return windows, TestClient(app)


def start(client: TestClient, account: str = "second", layout: str = "desktop", email: str | None = None,
          taken: tuple[str, ...] = ()) -> str:  # fmt: skip
    body = {"account": account, "layout": layout, "email": email, "taken": list(taken)}
    started = client.post("/logins", json=body, headers=AUTH)
    assert started.status_code == 201, started.text
    return started.json()["id"]


def state(client: TestClient, login_id: str, until: tuple[str, ...]) -> dict:
    for _ in range(500):
        body = client.get(f"/logins/{login_id}", headers=AUTH).json()
        if body.get("state") in until:
            return body
        time.sleep(0.01)
    raise AssertionError(f"never {until}: {body}")


ENDED = ("connected", "wrong_account", "in_use", "empty_library", "failed", "cancelled", "expired", "busy")


# -- windows ----------------------------------------------------------------------------------------------------------


def test_a_window_connects_and_records_the_account(paths):
    script = Script(paths)
    _, client = service(paths, script)
    with client:
        login_id = start(client, layout="phone", taken=("owner@example.org",))
        script.go.set()
        ended = state(client, login_id, ENDED)
    assert ended["state"] == "connected" and ended["email"] == EMAIL and ended["account"] == "second"
    assert script.calls == [("second", None, ["owner@example.org"], 0)]
    kept = paths.keeper("second")
    assert kept["state"] == SIGNED_IN and kept["email"] == EMAIL and kept["generation"] == 1
    assert paths.kept() == ["second"]


@pytest.mark.parametrize("code", ["wrong_account", "in_use"])
def test_a_refused_sign_in_wipes_the_profile(paths, code):
    script = Script(paths)
    script.outcome = window.Refused(code)
    _, client = service(paths, script)
    with client:
        login_id = start(client)
        script.go.set()
        assert state(client, login_id, ENDED)["state"] == code
    assert not paths.profile("second").exists() and paths.keeper("second") is None


def test_a_refused_sign_in_of_a_kept_account_withdraws_its_snapshot(paths):
    snapshot(paths, "second", 4)
    paths.record("second", SIGNED_IN, email=EMAIL, generation=4)
    script = Script(paths)
    script.outcome = window.Refused("wrong_account")
    _, client = service(paths, script)
    with client:
        login_id = start(client, email=EMAIL)
        script.go.set()
        assert state(client, login_id, ENDED)["state"] == "wrong_account"
    assert not (paths.session("second") / "meta.json").exists()
    assert paths.keeper("second")["state"] == SIGNED_OUT and paths.generation("second") == 4


def test_a_cancelled_window_leaves_no_profile_of_an_unkept_account(paths):
    script = Script(paths)
    _, client = service(paths, script)
    with client:
        login_id = start(client)
        state(client, login_id, ("waiting",))
        assert client.delete(f"/logins/{login_id}", headers=AUTH).status_code == 204
        assert state(client, login_id, ENDED)["state"] == "cancelled"
    assert not paths.profile("second").exists()


def test_a_failed_window_keeps_a_kept_accounts_profile(paths):
    paths.profile("second").mkdir(parents=True)
    paths.record("second", SIGNED_IN, email=EMAIL, generation=2)
    script = Script(paths)
    script.outcome = ExportError("no", "empty_library")
    _, client = service(paths, script)
    with client:
        login_id = start(client, email=EMAIL)
        script.go.set()
        assert state(client, login_id, ENDED)["state"] == "empty_library"
    assert paths.profile("second").is_dir() and paths.keeper("second")["state"] == SIGNED_IN


def test_no_window_without_the_browser_policy(paths, monkeypatch):
    # The window could otherwise open file:// pages: every other account's profile and snapshot is in this container.
    from photos_login import app as login_app

    monkeypatch.setattr(login_app, "policy_installed", lambda: False)
    script = Script(paths)
    _, client = service(paths, script, require_policy=True)
    with client:
        body = {"account": "second", "layout": "desktop", "taken": []}
        assert client.post("/logins", json=body, headers=AUTH).status_code == 400
    assert script.calls == []
    monkeypatch.setattr(window, "policy_installed", lambda: False)
    with pytest.raises(window.PolicyMissing):
        window.sign_in(profile=paths.profile("second"), session_dir=paths.session("second"), layout="desktop",
                       env={}, email=None, taken=[], min_generation=0, cancel=threading.Event(), deadline=0.0,
                       on_state=lambda state, host: None)  # fmt: skip
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile.login").read_text()
    assert "python -m signin_window.browser install-policy" in dockerfile


def test_the_policy_closes_local_pages_and_tools():
    from signin_window.browser import POLICY as policy

    for scheme in ("file://*", "chrome://*", "devtools://*", "view-source:*", "data:*", "blob:*"):
        assert scheme in policy["URLBlocklist"], scheme
    # DeveloperToolsAvailability=2 would also close the debugging pipe the window is driven through; DevTools
    # stays useless without its devtools:// pages and file dialogs, and kiosk mode keeps it closed.
    assert "DeveloperToolsAvailability" not in policy and policy["AllowFileSelectionDialogs"] is False
    assert policy["DownloadRestrictions"] == 3 and policy["PasswordManagerEnabled"] is False
    assert not any(entry.startswith("http") for entry in policy["URLBlocklist"])  # Google's own pages still load


def test_a_notice_nobody_may_watch_ends_the_window_and_keeps_a_kept_profile(paths):
    paths.profile("second").mkdir(parents=True)
    paths.record("second", SIGNED_IN, email=EMAIL, generation=2)
    script = Script(paths)
    script.outcome = window.NotViewable("interstitial")
    _, client = service(paths, script)
    with client:
        login_id = start(client, email=EMAIL)
        script.go.set()
        ended = state(client, login_id, ENDED)
    assert ended["state"] == "failed" and ended["error"] == "interstitial"
    assert paths.profile("second").is_dir() and paths.keeper("second")["state"] == SIGNED_IN


def test_a_cancel_while_saving_changes_nothing(paths):
    script = Script(paths)
    script.saved.clear()
    _, client = service(paths, script)
    with client:
        login_id = start(client)
        script.go.set()
        state(client, login_id, ("saving",))
        assert client.delete(f"/logins/{login_id}", headers=AUTH).status_code == 204
        script.saved.set()
        assert state(client, login_id, ENDED)["state"] == "connected"


def test_one_window_at_a_time_and_none_while_the_browser_is_busy(paths):
    script = Script(paths)
    _, client = service(paths, script)
    with client:
        start(client)
        body = {"account": "third", "layout": "desktop", "taken": []}
        assert client.post("/logins", json=body, headers=AUTH).status_code == 503
        script.go.set()
    lock = threading.Lock()
    lock.acquire()  # the keeper is refreshing
    _, busy = service(paths, Script(paths), lock)
    with busy:
        assert state(busy, start(busy, account="third"), ENDED)["state"] == "busy"
    lock.release()


# -- the API ----------------------------------------------------------------------------------------------------------


def test_the_api_needs_the_bearer_and_never_returns_a_session(paths):
    script = Script(paths)
    _, client = service(paths, script)
    with client:
        assert client.get("/health").status_code == 200
        body = {"account": "second", "layout": "phone", "email": None, "taken": [EMAIL]}
        assert client.post("/logins", json=body).status_code == 401
        for bad in ({**body, "account": "../x"}, {**body, "taken": "x"}, {**body, "email": "nobody"}, []):
            assert client.post("/logins", json=bad, headers=AUTH).status_code == 400, bad
        started = client.post("/logins", json=body, headers=AUTH)
        assert started.status_code == 201 and started.json()["view_token"]
        login_id = started.json()["id"]
        assert client.get(f"/logins/{login_id}").status_code == 401
        assert client.get(f"/view/{login_id}?t={started.json()['view_token']}").status_code in (401, 404)
        script.go.set()
        ended = state(client, login_id, ENDED)
        assert ended["state"] == "connected" and ended["email"] == EMAIL
        assert "cookie" not in json.dumps(ended).lower() and "view_token" not in ended
        assert client.post(f"/logins/{login_id}/result", headers=AUTH).status_code == 409  # nothing to hand over
        for headers, code in (({}, 4401), (AUTH, 4404)):  # no bearer; the bearer but not the window's token
            url = f"/logins/{login_id}/vnc?t=x"
            with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect(url, headers=headers) as ws:
                ws.receive_bytes()
            assert closed.value.code == code


def test_the_live_view_works_only_on_the_sign_in_page(paths):
    script = Script(paths)
    windows, client = service(paths, script)
    with client:
        started = client.post("/logins", json={"account": "second", "layout": "desktop", "taken": []}, headers=AUTH)
        state(client, started.json()["id"], ("waiting",))
        login = windows.get(started.json()["id"])
        url = f"/logins/{login.id}/vnc?t={login.view_token}"
        for shown in ("starting", "saving"):
            login.state = shown
            with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect(url, headers=AUTH) as ws:
                ws.receive_bytes()
            assert closed.value.code == 4404
        login.state = "waiting"
        script.go.set()


def test_refresh_and_forget(paths):
    _, client = service(paths, Script(paths))
    with client:
        assert client.post("/accounts/second/refresh", headers=AUTH).status_code == 404  # not kept
        paths.profile("second").mkdir(parents=True)
        snapshot(paths, "second", 1)
        paths.record("second", SIGNED_IN, email=EMAIL, generation=1)
        assert client.post("/accounts/second/refresh").status_code == 401
        assert client.post("/accounts/second/refresh", headers=AUTH).status_code == 202
        assert client.delete("/accounts/second", headers=AUTH).status_code == 204
    assert not paths.profile("second").exists() and not paths.session("second").exists()


# -- the keeper -------------------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def __call__(self) -> float:
        return self.now


def kept_account(paths: Paths, clock: Clock, generation: int = 3) -> None:
    paths.profile("second").mkdir(parents=True, exist_ok=True)
    snapshot(paths, "second", generation)
    paths.record("second", SIGNED_IN, email=EMAIL, generation=generation, clock=clock)


def test_the_keeper_refreshes_hourly_and_counts_generations_on(paths):
    clock = Clock()
    kept_account(paths, clock)
    calls: list[dict] = []

    def refresher(**kw):
        calls.append(kw)
        return snapshot(paths, "second", kw["min_generation"] + 1)

    keeper = Keeper(paths, threading.Lock(), refresher=refresher, clock=clock)
    keeper.round()
    assert calls == []  # just signed in
    clock.now += keeper_mod.EVERY
    keeper.round()
    assert calls[0]["email"] == EMAIL and calls[0]["min_generation"] == 3
    assert paths.keeper("second")["state"] == SIGNED_IN and paths.generation("second") == 4
    keeper.round()
    assert len(calls) == 1


@pytest.mark.parametrize("code", ["signed_out", "challenge"])
def test_a_signed_out_profile_withdraws_the_snapshot_when_seen_twice(paths, code):
    clock = Clock()
    kept_account(paths, clock)

    def refresher(**kw):
        raise ExportError("signed out", code)

    keeper = Keeper(paths, threading.Lock(), refresher=refresher, clock=clock)
    assert keeper.refresh_one("second") == ERROR  # once: the snapshot stays, a retry follows
    assert (paths.session("second") / "meta.json").exists() and paths.kept() == ["second"]
    clock.now += keeper_mod.RETRY
    assert keeper.refresh_one("second") == SIGNED_OUT
    assert not (paths.session("second") / "meta.json").exists()
    assert paths.keeper("second")["state"] == SIGNED_OUT and paths.generation("second") == 3
    assert paths.kept() == []  # not refreshed again until a new sign-in


def test_a_signed_out_reading_after_a_good_one_starts_counting_again(paths):
    clock = Clock()
    kept_account(paths, clock)
    outcomes = iter([ExportError("x", "signed_out"), None, ExportError("x", "signed_out")])

    def refresher(**kw):
        outcome = next(outcomes)
        if outcome:
            raise outcome
        return snapshot(paths, "second", kw["min_generation"] + 1)

    keeper = Keeper(paths, threading.Lock(), refresher=refresher, clock=clock)
    assert [keeper.refresh_one("second") for _ in range(3)] == [ERROR, SIGNED_IN, ERROR]


def test_another_account_in_the_profile_withdraws_at_once(paths):
    clock = Clock()
    kept_account(paths, clock)

    def refresher(**kw):
        raise ExportError("mismatch", "account_mismatch")

    keeper = Keeper(paths, threading.Lock(), refresher=refresher, clock=clock)
    assert keeper.refresh_one("second") == SIGNED_OUT
    assert not (paths.session("second") / "meta.json").exists()


@pytest.mark.parametrize("code", ["interstitial", "no_account", "empty_library", "failed"])
def test_anything_else_keeps_the_snapshot_and_retries_sooner(paths, code):
    clock = Clock()
    kept_account(paths, clock)

    def refresher(**kw):
        raise ExportError("no", code)

    keeper = Keeper(paths, threading.Lock(), refresher=refresher, clock=clock)
    assert keeper.refresh_one("second") == ERROR
    assert (paths.session("second") / "meta.json").exists()
    assert paths.keeper("second")["error"] == code and paths.kept() == ["second"]
    clock.now += keeper_mod.RETRY - 1
    assert not keeper.due("second")
    clock.now += 1
    assert keeper.due("second")


def test_an_early_refresh_is_rate_limited(paths):
    clock = Clock()
    kept_account(paths, clock)
    keeper = Keeper(paths, threading.Lock(), refresher=lambda **kw: {}, clock=clock)
    assert keeper.request("second") and not keeper.request("nobody")
    assert not keeper.due("second")
    clock.now += keeper_mod.REQUEST_GAP
    assert keeper.due("second")


def test_the_keeper_waits_for_a_window(paths, monkeypatch):
    clock = Clock()
    kept_account(paths, clock)
    monkeypatch.setattr(keeper_mod, "LOCK_WAIT", 0.05)
    lock = threading.Lock()
    lock.acquire()
    keeper = Keeper(paths, lock, refresher=lambda **kw: pytest.fail("ran during a window"), clock=clock)
    assert keeper.refresh_one("second") is None


# -- what the window may do -------------------------------------------------------------------------------------------

INPUT_CALLS = {
    "click", "dblclick", "tap", "fill", "type", "press", "press_sequentially", "check", "uncheck",
    "select_option", "set_input_files", "set_checked", "dispatch_event", "drag_to", "focus", "hover",
    "insert_text", "down", "up", "move", "wheel",
}  # fmt: skip


def test_the_login_service_never_acts_on_the_page():
    for f in sorted(LOGIN.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("keyboard", "mouse", "touchscreen"), f"{node.attr} in {f.name}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in INPUT_CALLS, f".{node.func.attr}() in {f.name}"


def test_the_window_shows_only_googles_sign_in_pages():
    assert window.SIGN_IN_URL.startswith("https://accounts.google.com/")
    allowed = (
        "https://accounts.google.com/v3/signin/identifier",
        "https://photos.google.com/",
        "https://www.google.com/photos/about/",
        "https://accounts.youtube.com/accounts/SetSID",
        "https://accounts.google.co.uk/accounts/SetSID",
        "https://accounts.google.com.au/accounts/SetSID",
        "https://consent.google.com/ml",
        "https://gds.google.com/web/recoveryoptions",
        "about:blank",
    )
    for url in allowed:
        assert window.allowed_document(url), url
    refused = ("http://accounts.google.com/", "https://google.com.evil.example/", "https://evil.example/",
               "https://evilgoogle.com/", "https://www.youtube.com/", "https://accounts.google.com:8443/",
               "https://mail.google.com/", "https://myaccount.google.com/", "https://www.google.com/search?q=x",
               "https://drive.google.com/", "https://www.google.com.au/", "data:text/html,x", "blob:https://x/y",
               "about:srcdoc", "javascript:alert(1)", "file:///etc/passwd")  # fmt: skip
    for url in refused:
        assert not window.allowed_document(url), url


def test_the_window_is_a_kiosk_without_passkeys():
    import inspect

    assert "--kiosk" in window.LAUNCH_ARGS
    source = inspect.getsource(window.sign_in)
    assert "harden_sync(context)" in source and "top_level(request)" in source
    assert "harden_sync" not in inspect.getsource(window.refresh)  # nobody watches a refresh
