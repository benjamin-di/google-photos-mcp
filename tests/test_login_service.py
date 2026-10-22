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
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from photos_login import keeper as keeper_mod
from photos_login import window
from photos_login.app import build_app
from photos_login.keeper import Keeper
from photos_login.profiles import ERROR, SIGNED_IN, SIGNED_OUT, Paths
from photos_login.windows import Busy, Windows
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
    """A sign-in runner standing in for the browser: does what the test says, when it says."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.go = threading.Event()
        self.outcome: object = None
        self.calls: list[tuple] = []

    def __call__(self, login, email, taken, min_generation, on_state):  # type: ignore[no-untyped-def]
        self.calls.append((login.account, email, taken, min_generation))
        login.state = "waiting"
        self.paths.profile(login.account).mkdir(parents=True, exist_ok=True)
        self.go.wait(5)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        on_state("saving", "photos.google.com")
        return snapshot(self.paths, login.account, min_generation + 1)


def windows_for(paths: Paths, script: Script, lock: threading.Lock | None = None, **kw) -> Windows:
    windows = Windows(paths, lock or threading.Lock(), runner=script, lock_wait=0.2, require_policy=False, **kw)
    windows._start_display = lambda login: None  # type: ignore[method-assign]
    windows._stop_display = lambda login: None  # type: ignore[method-assign]
    return windows


def ended(windows: Windows, login_id: str) -> str:
    for _ in range(200):
        login = windows.get(login_id)
        if login is not None and login.ended_at is not None:
            return login.state
        time.sleep(0.01)
    raise AssertionError("the window did not end")


# -- windows ----------------------------------------------------------------------------------------------------------


def test_a_window_connects_and_records_the_account(paths):
    script = Script(paths)
    windows = windows_for(paths, script)
    login = windows.start("second", "phone", email=None, taken=["owner@example.org"])
    script.go.set()
    assert ended(windows, login.id) == "connected"
    assert windows.get(login.id).public()["email"] == EMAIL
    assert script.calls == [("second", None, ["owner@example.org"], 0)]
    kept = paths.keeper("second")
    assert kept["state"] == SIGNED_IN and kept["email"] == EMAIL and kept["generation"] == 1
    assert paths.kept() == ["second"]


@pytest.mark.parametrize("code", ["wrong_account", "in_use"])
def test_a_refused_sign_in_wipes_the_profile(paths, code):
    script = Script(paths)
    script.outcome = window.Refused(code)
    windows = windows_for(paths, script)
    login = windows.start("second", "desktop", email=None, taken=[])
    script.go.set()
    assert ended(windows, login.id) == code
    assert not paths.profile("second").exists() and paths.keeper("second") is None


def test_a_refused_sign_in_of_a_kept_account_withdraws_its_snapshot(paths):
    snapshot(paths, "second", 4)
    paths.record("second", SIGNED_IN, email=EMAIL, generation=4)
    script = Script(paths)
    script.outcome = window.Refused("wrong_account")
    windows = windows_for(paths, script)
    login = windows.start("second", "desktop", email=EMAIL, taken=[])
    script.go.set()
    assert ended(windows, login.id) == "wrong_account"
    assert not (paths.session("second") / "meta.json").exists()
    assert paths.keeper("second")["state"] == SIGNED_OUT and paths.generation("second") == 4


def test_a_cancelled_window_leaves_no_profile_of_an_unkept_account(paths):
    script = Script(paths)
    script.outcome = window.Cancelled()
    windows = windows_for(paths, script)
    login = windows.start("second", "desktop", email=None, taken=[])
    windows.close(login.id)
    script.go.set()
    assert ended(windows, login.id) == "cancelled"
    assert not paths.profile("second").exists()


def test_a_failed_window_keeps_a_kept_accounts_profile(paths):
    paths.profile("second").mkdir(parents=True)
    paths.record("second", SIGNED_IN, email=EMAIL, generation=2)
    script = Script(paths)
    script.outcome = ExportError("no", "empty_library")
    windows = windows_for(paths, script)
    login = windows.start("second", "desktop", email=EMAIL, taken=[])
    script.go.set()
    assert ended(windows, login.id) == "empty_library"
    assert paths.profile("second").is_dir() and paths.keeper("second")["state"] == SIGNED_IN


def test_no_window_without_the_browser_policy(paths, tmp_path, monkeypatch):
    # The window could otherwise open file:// pages: every other account's profile and snapshot is in this container.
    monkeypatch.setattr(window, "POLICY_DIRS", (tmp_path / "policies",))
    windows = Windows(paths, threading.Lock(), runner=Script(paths))
    with pytest.raises(window.PolicyMissing):
        windows.start("second", "desktop", email=None, taken=[])
    keeper = Keeper(paths, windows.browser_lock, refresher=lambda **kw: {})
    client = TestClient(build_app(windows, keeper, token=TOKEN, novnc_dir=tmp_path / "none"))
    body = {"account": "second", "layout": "desktop", "taken": []}
    assert client.post("/logins", json=body, headers=AUTH).json() == {"error": "policy"}
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / window.POLICY_NAME).write_text(json.dumps(window.POLICY))
    assert window.policy_installed()
    partial = {**window.POLICY, "URLBlocklist": ["chrome://*"]}  # without file://: not ours
    (tmp_path / "policies" / window.POLICY_NAME).write_text(json.dumps(partial))
    assert not window.policy_installed()


def test_the_policy_closes_local_pages_and_tools():
    policy = window.POLICY
    for scheme in ("file://*", "chrome://*", "devtools://*", "view-source:*", "data:*", "blob:*"):
        assert scheme in policy["URLBlocklist"], scheme
    # DeveloperToolsAvailability=2 would also close the debugging pipe the window is driven through; DevTools
    # stays useless without its devtools:// pages and file dialogs.
    assert "DeveloperToolsAvailability" not in policy and policy["AllowFileSelectionDialogs"] is False
    assert policy["DownloadRestrictions"] == 3 and policy["PasswordManagerEnabled"] is False
    assert not any(entry.startswith("http") for entry in policy["URLBlocklist"])  # Google's own pages still load


def test_a_notice_nobody_may_watch_ends_the_window_and_keeps_a_kept_profile(paths):
    paths.profile("second").mkdir(parents=True)
    paths.record("second", SIGNED_IN, email=EMAIL, generation=2)
    script = Script(paths)
    script.outcome = window.NotViewable("interstitial")
    windows = windows_for(paths, script)
    login = windows.start("second", "desktop", email=EMAIL, taken=[])
    script.go.set()
    assert ended(windows, login.id) == "failed" and windows.get(login.id).error == "interstitial"
    assert paths.profile("second").is_dir() and paths.keeper("second")["state"] == SIGNED_IN


def test_a_cancel_while_saving_changes_nothing(paths):
    script = Script(paths)
    windows = windows_for(paths, script)
    login = windows.start("second", "desktop", email=None, taken=[])
    for _ in range(100):
        if login.state == "waiting":
            break
        time.sleep(0.01)
    login.state = "saving"
    windows.close(login.id)
    assert not login.cancel.is_set()
    script.go.set()
    assert ended(windows, login.id) == "connected"


def test_one_window_at_a_time_and_none_while_the_browser_is_busy(paths):
    script = Script(paths)
    windows = windows_for(paths, script)
    windows.start("second", "desktop", email=None, taken=[])
    with pytest.raises(Busy):
        windows.start("third", "desktop", email=None, taken=[])
    script.go.set()
    lock = threading.Lock()
    lock.acquire()  # the keeper is refreshing
    busy = windows_for(paths, Script(paths), lock)
    login = busy.start("third", "desktop", email=None, taken=[])
    assert ended(busy, login.id) == "busy"


# -- the API ----------------------------------------------------------------------------------------------------------


def test_the_api_needs_the_bearer_and_never_returns_a_session(paths, tmp_path):
    script = Script(paths)
    windows = windows_for(paths, script)
    keeper = Keeper(paths, windows.browser_lock, refresher=lambda **kw: {})
    client = TestClient(build_app(windows, keeper, token=TOKEN, novnc_dir=tmp_path / "none"))
    assert client.get("/health").status_code == 200
    body = {"account": "second", "layout": "phone", "email": None, "taken": [EMAIL]}
    assert client.post("/logins", json=body).status_code == 401
    for bad in ({**body, "account": "../x"}, {**body, "taken": "x"}, {**body, "email": "nobody"}, []):
        assert client.post("/logins", json=bad, headers=AUTH).status_code == 400, bad
    started = client.post("/logins", json=body, headers=AUTH)
    assert started.status_code == 201 and started.json()["view_token"]
    login_id = started.json()["id"]
    assert client.get(f"/logins/{login_id}").status_code == 401
    script.go.set()
    ended(windows, login_id)
    state = client.get(f"/logins/{login_id}", headers=AUTH).json()
    assert state["state"] == "connected" and state["email"] == EMAIL
    assert "cookie" not in json.dumps(state).lower() and "view_token" not in state
    for headers, code in (({}, 4401), (AUTH, 4404)):  # no bearer; the bearer but not the window's token
        url = f"/logins/{login_id}/vnc?t=x"
        with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect(url, headers=headers) as ws:
            ws.receive_bytes()
        assert closed.value.code == code


def test_the_live_view_works_only_on_the_sign_in_page(paths, tmp_path):
    script = Script(paths)
    windows = windows_for(paths, script)
    keeper = Keeper(paths, windows.browser_lock, refresher=lambda **kw: {})
    client = TestClient(build_app(windows, keeper, token=TOKEN, novnc_dir=tmp_path / "none"))
    started = client.post("/logins", json={"account": "second", "layout": "desktop", "taken": []}, headers=AUTH)
    login = windows.get(started.json()["id"])
    url = f"/logins/{login.id}/vnc?t={login.view_token}"
    for state in ("starting", "saving"):
        login.state = state
        with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect(url, headers=AUTH) as ws:
            ws.receive_bytes()
        assert closed.value.code == 4404
    script.go.set()


def test_refresh_and_forget(paths, tmp_path):
    windows = windows_for(paths, Script(paths))
    keeper = Keeper(paths, windows.browser_lock, refresher=lambda **kw: {})
    client = TestClient(build_app(windows, keeper, token=TOKEN, novnc_dir=tmp_path / "none"))
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


def test_passkeys_are_off_in_the_window():
    assert any("WebAuthentication" in arg for arg in window.LAUNCH_ARGS)
