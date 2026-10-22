"""Accounts (README "Accounts"): each request acts for one account, named only by the proxy's bearer and header;
each account has its own snapshot, state, cache and cursors; budgets are split from the server's totals; the breaker
and pacing are shared; an account nobody signed in serves nothing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import EMAIL, write_meta
from starlette.testclient import TestClient

from photos_mcp import accounts, server
from photos_mcp.auth import bearer_auth_from_env
from photos_mcp.config import ConfigError, Settings
from photos_mcp.errors import PhotosError

HEADER = "X-Test-Account"
PROXY, DIRECT = "proxy-test-token-0001", "direct-test-token-0002"
OTHER = "second@example.com"


def make_settings(tmp_path: Path, **env: str) -> Settings:
    base = {
        "GOOGLE_PHOTOS_EXPECTED_EMAIL": EMAIL,
        "PHOTOS_SESSION_DIR": str(tmp_path / "session"),
        "PHOTOS_STATE_DIR": str(tmp_path / "state"),
        "PHOTOS_ACCOUNT_SESSIONS_DIR": str(tmp_path / "sessions"),
        "PHOTOS_TRUSTED_ACCOUNT_HEADER": HEADER,
        "PHOTOS_DEFAULT_ACCOUNT": "main",
    }
    return Settings.from_env({**base, **env})


def publish(settings: Settings, account_id: str, email: str, generation: int = 3) -> Path:
    folder = settings.account_sessions_dir / account_id
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parent / "fixtures" / "session" / "cookies.txt"
    (folder / "cookies.txt").write_text(source.read_text())
    write_meta(folder, generation, email)
    return folder


@pytest.fixture
def settings(tmp_path) -> Settings:
    (tmp_path / "session").mkdir()
    write_meta_default(tmp_path / "session")
    return make_settings(tmp_path)


def write_meta_default(folder: Path) -> None:
    source = Path(__file__).parent / "fixtures" / "session" / "cookies.txt"
    (folder / "cookies.txt").write_text(source.read_text())
    write_meta(folder, 7, EMAIL)


# -- settings ---------------------------------------------------------------------------------------------------------


def test_settings_validate_the_account_and_link_options(tmp_path):
    good = make_settings(
        tmp_path,
        PHOTOS_PUBLIC_URL="https://example.com/photos/",
        PHOTOS_LOGIN_URL="http://photos-mcp-login:8080",
        PHOTOS_LOGIN_TOKEN="l" * 32,
    )
    assert good.connect_enabled and good.public_url == "https://example.com/photos"
    assert good.default_account == "main" and good.trusted_account_header == HEADER
    assert not make_settings(tmp_path).connect_enabled
    for bad in (
        {"PHOTOS_DEFAULT_ACCOUNT": "Main"},
        {"PHOTOS_DEFAULT_ACCOUNT": "../x"},
        {"PHOTOS_TRUSTED_ACCOUNT_HEADER": "X Bad"},
        {"PHOTOS_PUBLIC_URL": "http://example.com/photos"},
        {"PHOTOS_PUBLIC_URL": "https://example.com/photos?x=1"},
        {"PHOTOS_LOGIN_URL": "https://photos-mcp-login:8080"},
        {"PHOTOS_LOGIN_URL": "http://photos-mcp-login:8080", "PHOTOS_LOGIN_TOKEN": "short"},
    ):
        with pytest.raises(ConfigError):
            make_settings(tmp_path, **bad)


# -- the account records ----------------------------------------------------------------------------------------------


def test_shares_are_split_from_the_totals(settings):
    assert accounts.default(settings).rpc_budget == 3000
    accounts.add(settings, "second", rpc_budget=1500, thumb_budget=1000)
    rest = accounts.default(settings)
    assert (rest.rpc_budget, rest.thumb_budget) == (1500, 1000)
    with pytest.raises(accounts.AccountConfigError, match="at most 1500 RPCs"):
        accounts.add(settings, "third", rpc_budget=1501, thumb_budget=0)
    accounts.add(settings, "third", rpc_budget=1500, thumb_budget=1000)
    assert (accounts.default(settings).rpc_budget, accounts.default(settings).thumb_budget) == (0, 0)
    accounts.set_budget(settings, "third", rpc_budget=500, thumb_budget=500)
    assert accounts.default(settings).rpc_budget == 1000
    with pytest.raises(accounts.AccountConfigError):
        accounts.set_budget(settings, "second", rpc_budget=2600, thumb_budget=0)


def test_shares_shrink_when_the_totals_are_lowered(settings, tmp_path):
    accounts.add(settings, "second", rpc_budget=2000, thumb_budget=1000)
    accounts.add(settings, "third", rpc_budget=1000, thumb_budget=1000)
    lowered = make_settings(tmp_path, PHOTOS_DAILY_RPC_BUDGET="1500", PHOTOS_DAILY_THUMB_BUDGET="1000")
    assert accounts.shares(lowered) == {"second": (1000, 500), "third": (500, 500)}
    assert accounts.load(lowered, "second").rpc_budget == 1000
    rest = accounts.default(lowered)
    assert (rest.rpc_budget, rest.thumb_budget) == (0, 0)


def test_add_refuses_bad_ids_duplicates_and_taken_addresses(settings):
    for bad in ("main", "Bad", "x/y", "", "a" * 33):
        with pytest.raises(accounts.AccountConfigError):
            accounts.add(settings, bad, rpc_budget=1, thumb_budget=1)
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1, email=" Second@Example.com ")
    assert accounts.load(settings, "second").email == OTHER
    with pytest.raises(accounts.AccountConfigError, match="exists"):
        accounts.add(settings, "second", rpc_budget=1, thumb_budget=1)
    for taken in (EMAIL, OTHER):
        with pytest.raises(accounts.AccountConfigError, match="already serves"):
            accounts.add(settings, "third", rpc_budget=1, thumb_budget=1, email=taken)


def test_binding_happens_once_and_never_to_a_taken_address(settings):
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1)
    accounts.add(settings, "third", rpc_budget=1, thumb_budget=1)
    assert accounts.load(settings, "second").email is None
    assert accounts.taken_emails(settings, "second") == [EMAIL]
    assert not accounts.bind_email(settings, "second", EMAIL)  # the default account's
    assert accounts.bind_email(settings, "second", OTHER)
    assert accounts.bind_email(settings, "second", OTHER.upper())  # the same address again
    assert not accounts.bind_email(settings, "second", "third@example.com")  # never rebound
    assert not accounts.bind_email(settings, "third", OTHER)  # another account has it
    assert accounts.load(settings, "second").email == OTHER


def test_unknown_or_unreadable_accounts_are_never_served(settings):
    with pytest.raises(PhotosError) as caught:
        accounts.load(settings, "nobody")
    assert caught.value.code == "account_not_connected"
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1)
    record = settings.state_dir / "accounts" / "second" / "account.json"
    record.write_text(json.dumps({"daily_rpc_budget": "lots", "daily_thumb_budget": 1}))
    with pytest.raises(PhotosError):
        accounts.load(settings, "second")
    assert accounts.shares(settings) == {}  # and it takes nothing from the default account's budgets


def test_add_refuses_a_left_over_folder(settings):
    leftover = settings.state_dir / "accounts" / "second"
    leftover.mkdir(parents=True)
    with pytest.raises(accounts.AccountConfigError, match="left over"):
        accounts.add(settings, "second", rpc_budget=1, thumb_budget=1)


def test_an_unbound_account_binds_to_what_the_login_service_recorded(settings):
    # The person closed the page while the window was saving: the sign-in still counts.
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1)
    folder = publish(settings, "second", OTHER)
    (folder / "keeper.json").write_text(json.dumps({"state": "signed_in", "email": OTHER, "generation": 3}))
    registry = server.Accounts.from_settings(settings)
    assert registry.get("second").status()["session"]["state"] == "ok"
    assert accounts.load(settings, "second").email == OTHER


def test_a_recorded_address_another_account_serves_is_not_bound(settings):
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1)
    folder = publish(settings, "second", EMAIL)
    (folder / "keeper.json").write_text(json.dumps({"state": "signed_in", "email": EMAIL, "generation": 3}))
    registry = server.Accounts.from_settings(settings)
    assert registry.get("second").status()["session"] == {"state": "not_signed_in"}
    assert accounts.load(settings, "second").email is None


def test_remove_deletes_the_record_and_state(settings):
    folder = accounts.add(settings, "second", rpc_budget=1, thumb_budget=1)
    (folder / "cache").mkdir()
    accounts.remove(settings, "second")
    assert not folder.exists() and accounts.configured(settings) == []
    with pytest.raises(accounts.AccountConfigError):
        accounts.remove(settings, "second")


# -- the registry -----------------------------------------------------------------------------------------------------


def test_each_account_has_its_own_state_snapshot_cache_and_cursors(settings):
    accounts.add(settings, "second", rpc_budget=1000, thumb_budget=500, email=OTHER)
    publish(settings, "second", OTHER)
    registry = server.Accounts.from_settings(settings)
    main, second = registry.get(None), registry.get("second")
    assert registry.get("main") is main and registry.get("second") is second
    assert second.state.path == settings.state_dir / "accounts" / "second" / "state.json"
    assert second._cache.directory == settings.state_dir / "accounts" / "second" / "cache"
    assert second._cursors is not main._cursors
    assert (main.state.rpc_budget, main.state.thumb_budget) == (2000, 1500)
    assert (second.state.rpc_budget, second.state.thumb_budget) == (1000, 500)
    status = second.status()
    assert status["account_id"] == "second" and status["session"]["account"] == OTHER
    assert status["session"]["state"] == "ok" and status["session"]["generation"] == 3
    assert main.status()["account_id"] == "main" and main.status()["session"]["account"] == EMAIL
    # Pacing and the breaker are shared; budgets are not.
    assert second._t.pacer is main._t.pacer and second._t.image_pacer is main._t.image_pacer
    second.state.trip_breaker()
    assert main.status()["breaker"]["open"] and second.status()["breaker"]["open"]
    second.state.take("rpc", 1000)
    with pytest.raises(PhotosError):
        second.state.take("rpc")
    main.state.take("rpc")  # the default account's own budget is untouched


def test_a_share_change_reaches_the_running_server(settings):
    accounts.add(settings, "second", rpc_budget=1000, thumb_budget=500)
    registry = server.Accounts.from_settings(settings)
    second = registry.get("second")
    accounts.set_budget(settings, "second", rpc_budget=200, thumb_budget=100)
    assert registry.get("second") is second and second.state.rpc_budget == 200
    assert registry.get(None).state.rpc_budget == 2800


def test_a_removed_account_is_forgotten(settings):
    accounts.add(settings, "second", rpc_budget=1000, thumb_budget=500, email=OTHER)
    registry = server.Accounts.from_settings(settings)
    second = registry.get("second")
    second.state.mark_dead(5, "session_expired")
    accounts.remove(settings, "second")
    accounts.add(settings, "second", rpc_budget=1000, thumb_budget=500, email=OTHER)
    # Re-added with the same address, without a request in between: still a new account, nothing carried over.
    fresh = registry.get("second")
    assert fresh is not second and fresh.state.dead_code(1) is None
    accounts.remove(settings, "second")
    with pytest.raises(PhotosError):
        registry.get("second")
    assert registry.get(None).state.rpc_budget == 3000


def test_an_account_nobody_signed_in_serves_nothing(settings):
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1)
    registry = server.Accounts.from_settings(settings)
    second = registry.get("second")
    assert second.status()["session"] == {"state": "not_signed_in"}
    with pytest.raises(PhotosError) as caught:
        second.search("beach")
    assert caught.value.code == "not_signed_in"
    # A snapshot alone does not do: until a sign-in binds the account's address, nothing is served.
    publish(settings, "second", OTHER)
    assert registry.get("second").status()["session"] == {"state": "not_signed_in"}
    with pytest.raises(PhotosError) as caught:
        registry.get("second").search("beach")
    assert caught.value.code == "not_signed_in"
    accounts.bind_email(settings, "second", OTHER)
    assert registry.get("second") is not second  # rebuilt for its address
    assert registry.get("second").status()["session"]["state"] == "ok"


def test_a_snapshot_of_another_address_is_a_mismatch(settings):
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1, email=OTHER)
    publish(settings, "second", "intruder@example.com")
    service = server.Accounts.from_settings(settings).get("second")
    assert service.status()["session"]["state"] == "account_mismatch"
    with pytest.raises(PhotosError) as caught:
        service.search("beach")
    assert caught.value.code == "account_mismatch"


def test_a_dead_generation_asks_the_keeper_to_look(settings, monkeypatch):
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1, email=OTHER)
    asked: list[str] = []

    class Login:
        def refresh(self, account):
            asked.append(account)

    monkeypatch.setattr(server.threading, "Thread", _Inline)
    registry = server.Accounts.from_settings(settings, login=Login())  # type: ignore[arg-type]
    transport = registry.get("second")._t
    publish(settings, "second", OTHER)
    ctx = transport._context()
    transport._fail(ctx, "session_expired", "test")
    assert asked == ["second"]
    transport._fail(ctx, "blocked", "test")  # pushback is no proof of a dead session
    assert asked == ["second"]


class _Inline:
    def __init__(self, target, daemon=None, **_):
        self.target = target

    def start(self):
        self.target()


# -- which account a request is for -----------------------------------------------------------------------------------


def _client(settings) -> TestClient:
    auth = bearer_auth_from_env({"MCP_BEARER_TOKEN": f"{PROXY},{DIRECT}"})
    app = server.create_server(server.Accounts.from_settings(settings), auth=auth).http_app(**server.HTTP_OPTIONS)
    return TestClient(app)


def _status(client: TestClient, token: str, **headers: str) -> dict:
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "photos_status", "arguments": {}}}
    response = client.post(
        "/mcp",
        json=body,
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "mcp-protocol-version": "2025-03-26",
            **headers,
        },
    )
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def test_the_proxy_names_the_account_and_nobody_else_can(settings):
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1, email=OTHER)
    publish(settings, "second", OTHER)
    with _client(settings) as client:
        assert _status(client, PROXY, **{HEADER: "second"})["account_id"] == "second"
        assert _status(client, PROXY, **{HEADER: "main"})["account_id"] == "main"
        assert _status(client, DIRECT)["account_id"] == "main"  # another bearer: the default account
        for refused in (
            _status(client, PROXY),  # the proxy forgot the header: never the default account by accident
            _status(client, PROXY, **{HEADER: "nobody"}),
            _status(client, PROXY, **{HEADER: "../main"}),
            _status(client, DIRECT, **{HEADER: "second"}),  # only the proxy may name an account
        ):
            assert refused["error"]["code"] == "account_not_connected", refused


def test_without_a_trusted_header_every_request_is_the_default_accounts(tmp_path):
    (tmp_path / "session").mkdir()
    write_meta_default(tmp_path / "session")
    settings = make_settings(tmp_path, PHOTOS_TRUSTED_ACCOUNT_HEADER="")
    with _client(settings) as client:
        assert _status(client, PROXY, **{HEADER: "second"})["account_id"] == "main"


def test_a_server_built_from_settings_offers_links_when_configured(tmp_path):
    (tmp_path / "session").mkdir()
    write_meta_default(tmp_path / "session")
    settings = make_settings(
        tmp_path,
        PHOTOS_PUBLIC_URL="https://example.com/photos",
        PHOTOS_LOGIN_URL="http://photos-mcp-login:8080",
        PHOTOS_LOGIN_TOKEN="l" * 32,
    )
    registry = server.Accounts.from_settings(settings)
    assert registry.login is not None and registry.login.base == "http://photos-mcp-login:8080"
    with _client(settings) as client:
        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {DIRECT}",
                "mcp-protocol-version": "2025-03-26",
            },
        )
        assert "photos_connect" in {tool["name"] for tool in listed.json()["result"]["tools"]}


def test_the_connect_tool_exists_only_with_sign_in_links(settings):
    with _client(settings) as client:
        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {DIRECT}",
                "mcp-protocol-version": "2025-03-26",
            },
        )
        assert "photos_connect" not in {tool["name"] for tool in listed.json()["result"]["tools"]}


# -- the admin commands -----------------------------------------------------------------------------------------------


def _env(monkeypatch, settings, **extra: str) -> None:
    for name, value in {
        "GOOGLE_PHOTOS_EXPECTED_EMAIL": EMAIL,
        "PHOTOS_SESSION_DIR": str(settings.session_dir),
        "PHOTOS_STATE_DIR": str(settings.state_dir),
        "PHOTOS_ACCOUNT_SESSIONS_DIR": str(settings.account_sessions_dir),
        "PHOTOS_DEFAULT_ACCOUNT": "main",
        **extra,
    }.items():
        monkeypatch.setenv(name, value)


def test_admin_adds_lists_rebudgets_and_removes(settings, monkeypatch, capsys):
    from photos_mcp import admin

    _env(monkeypatch, settings)
    assert admin.main(["account", "add", "second", "--rpc-budget", "1500", "--thumb-budget", "1000"]) == 0
    assert json.loads(capsys.readouterr().out)["default_account_left"] == {
        "daily_rpc_budget": 1500,
        "daily_thumb_budget": 1000,
    }
    assert admin.main(["account", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(a["id"], a["default"], a["session"]) for a in listed] == [
        ("main", True, "ok"),
        ("second", False, "not_signed_in"),
    ]
    assert admin.main(["account", "budget", "second", "--rpc-budget", "4000", "--thumb-budget", "0"]) == 1
    assert "exceed" in capsys.readouterr().err
    assert admin.main(["account", "remove", "second"]) == 0
    assert accounts.configured(settings) == []
    assert admin.main(["account", "remove", "second"]) == 1


def test_admin_remove_deletes_the_sign_in_first(settings, monkeypatch, capsys):
    from photos_mcp import admin
    from photos_mcp.login_client import LoginUnavailable

    _env(monkeypatch, settings, PHOTOS_LOGIN_URL="http://photos-mcp-login:8080", PHOTOS_LOGIN_TOKEN="l" * 32)
    accounts.add(settings, "second", rpc_budget=1, thumb_budget=1)
    forgotten: list[str] = []

    def down(self, account):
        raise LoginUnavailable("unreachable")

    monkeypatch.setattr(admin.LoginClient, "forget", down)
    assert admin.main(["account", "remove", "second"]) == 1
    assert "nothing was changed" in capsys.readouterr().err and accounts.configured(settings) == ["second"]
    monkeypatch.setattr(admin.LoginClient, "forget", lambda self, account: forgotten.append(account))
    assert admin.main(["account", "remove", "second"]) == 0
    assert forgotten == ["second"] and accounts.configured(settings) == []


def test_admin_is_a_subcommand_of_the_server(monkeypatch):
    called: list[list[str]] = []
    from photos_mcp import admin

    monkeypatch.setattr(admin, "main", lambda argv: called.append(argv) or 0)
    with pytest.raises(SystemExit) as done:
        server.main(["admin", "account", "list"])
    assert done.value.code == 0 and called == [["account", "list"]]
