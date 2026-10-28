"""Real OIDC validation, owner isolation and application-owned connection recovery."""

import time
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
from connections_provider import Provider
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from test_accounts import make_settings
from test_connect import FakeLogin, owner_post

from photos_mcp import accounts
from photos_mcp.config import ConfigError
from photos_mcp.connections import Connections
from photos_mcp.connections_auth import ConnectionsConfig


def path(url):
    parsed = urlsplit(url)
    return parsed.path + ("?" + parsed.query if parsed.query else "")


@pytest.fixture
def system(tmp_path):
    settings = make_settings(
        tmp_path,
        PHOTOS_PUBLIC_URL="https://example.com",
        PHOTOS_LOGIN_URL="http://photos-mcp-login:8080",
        PHOTOS_LOGIN_TOKEN="fake-login-token" * 3,
    )
    accounts.add(settings, "first", rpc_budget=500, thumb_budget=100)
    accounts.add(settings, "second", rpc_budget=500, thumb_budget=100)
    config = ConnectionsConfig(
        "https://identity.example.com",
        "photos-ui",
        "https://example.com",
        {"subject-one": "first", "subject-two": "second"},
        "photos-users",
        tmp_path / "state",
    )
    provider, backend = Provider(), FakeLogin()
    portal = Connections(settings, backend, config, oidc_transport=provider.transport())
    app = Starlette(routes=[Mount("/connections", app=portal.app()), Mount("/connect", app=portal.viewer_app())])
    with TestClient(app, base_url="https://example.com", follow_redirects=False) as client:
        yield portal, provider, backend, client, settings


def sign_in(portal, provider, client, *, subject="subject-one", groups=None):
    start = client.get("/connections/login")
    callback = provider.authorize(start.headers["location"], subject, groups)
    return client.get(path(callback))


def start(client):
    response = owner_post(client, "/connections/start", headers={"origin": "https://example.com"})
    assert response.status_code == 303, response.text
    return path(response.headers["location"])


def test_oidc_pkce_server_side_state_and_owner_bound_mcp_links(system):
    portal, provider, backend, client, settings = system
    signed_out = client.get("/connections/")
    assert signed_out.status_code == 303 and signed_out.headers["location"] == "/connections/login"
    assert sign_in(portal, provider, client).status_code == 303
    home = client.get("/connections/")
    assert "Example One" in home.text and "Sign-in required" in home.text
    assert "Example Two" not in home.text
    cookies = str(client.cookies)
    assert all(secret not in cookies for secret in ["subject-one", "code_verifier", "fake-oidc-access-token"])
    link = portal.flow.create(accounts.load(settings, "first"), signed_in=False)
    page = client.get(path(link["url"]))
    assert page.status_code == 200 and '"autoStart": true' in page.text  # owner-bound: no intro to click through
    other = portal.flow.create(accounts.load(settings, "second"), signed_in=False)
    for suffix in ["", "/state", "/novnc/vnc.html"]:
        response = client.get(path(other["url"]) + suffix)
        assert response.status_code == 404 and "second" not in response.text
    for suffix in ["/start", "/cancel"]:
        assert client.post(path(other["url"]) + suffix, headers={"origin": "https://example.com"}).status_code == 404
    assert not backend.started


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "wrong"},
        {"aud": "wrong-client"},
        {"iss": "https://wrong.example.com"},
        {"exp": 1},
        {"sub": "unbound-subject"},
        {"groups": []},
    ],
)
def test_forged_stale_or_unentitled_oidc_result_cannot_create_a_browser_session(system, claims):
    portal, provider, _, client, _ = system
    provider.tamper = claims
    assert sign_in(portal, provider, client).status_code == 403
    assert client.get("/connections/status").status_code == 401


def test_callback_replay_and_cross_browser_state_are_refused(system):
    portal, provider, _, client, _ = system
    response = client.get("/connections/login")
    callback = path(provider.authorize(response.headers["location"]))
    saved = dict(client.cookies)
    client.cookies.clear()
    assert client.get(callback).status_code == 403
    client.cookies.update(saved)
    assert client.get(callback).status_code == 303
    assert client.get(callback).status_code == 403


def test_forwarded_link_requires_login_and_wrong_person_cannot_claim_it(system):
    portal, provider, _, client, settings = system
    link = portal.flow.create(accounts.load(settings, "first"), signed_in=False)
    response = client.get(path(link["url"]))
    assert response.status_code == 303 and "/connections/login?" in response.headers["location"]
    assert sign_in(portal, provider, client, subject="subject-two").status_code == 303
    assert client.get(path(link["url"])).status_code == 404
    assert owner_post(client, "/connections/start", headers={"origin": "https://other.example.com"}).status_code == 403


def test_reload_reuses_attempt_and_logout_or_expiry_removes_access(system):
    portal, provider, backend, client, _ = system
    assert sign_in(portal, provider, client).status_code == 303
    link = start(client)
    origin = {"origin": "https://example.com"}
    assert client.post(link + "/start", headers=origin).status_code == 200
    assert len(backend.started) == 1
    assert client.get(link).status_code == 200
    assert len(backend.started) == 1
    assert client.post("/connections/logout", headers=origin).status_code == 303
    assert client.get(link + "/state").status_code == 404
    assert sign_in(portal, provider, client).status_code == 303
    portal.auth.store.clock = lambda: time.time() + 901
    assert client.get("/connections/status").status_code == 401


def test_disconnect_barrier_survives_restart_and_fences_old_completion(system):
    portal, provider, backend, client, settings = system
    assert sign_in(portal, provider, client).status_code == 303
    url = start(client)
    old = next(iter(portal.flow.relay._links.values()))
    backend.forget = lambda account: (_ for _ in ()).throw(RuntimeError("fake transport failure"))
    failed = owner_post(client, "/connections/disconnect", headers={"origin": "https://example.com"})
    assert failed.status_code == 503
    assert 'href="/connections/"' in failed.text
    assert client.get("/connections/").status_code == 200
    assert owner_post(client, "/connections/start", headers={"origin": "https://example.com"}).status_code == 409
    replacement = Connections(settings, backend, portal.config, oidc_transport=provider.transport())
    assert not replacement.account_allowed("first")
    assert portal.flow._ended(old, {"state": "connected", "email": "first@example.com"}) == "failed"
    assert accounts.load(settings, "first").email is None
    assert client.get(url + "/state").status_code == 404
    backend.forget = lambda account: None
    assert owner_post(client, "/connections/disconnect", headers={"origin": "https://example.com"}).status_code == 303
    assert client.get("/connections/status").json()["state"] == "disconnected"
    start(client)
    assert portal.account_allowed("first")


def test_auth_config_and_return_destinations_fail_closed(system):
    from photos_mcp.server import Accounts, create_server

    portal, provider, backend, client, settings = system
    with pytest.raises(ConfigError):
        ConnectionsConfig.from_env("https://example.com", portal.config.state_dir, {})
    with pytest.raises(ConfigError):
        replace(portal.config, issuer="http://identity.example.com")
    with pytest.raises(ConfigError, match="Photos state directory"):
        create_server(
            Accounts.from_settings(settings, login=backend),
            connections=replace(portal.config, state_dir=settings.state_dir / "different"),
        )
    response = client.get("/connections/login?next=https://other.example.com")
    assert client.get(path(provider.authorize(response.headers["location"]))).headers["location"] == "/connections/"


def test_signed_administrator_claim_maps_existing_account_without_a_second_directory(system):
    portal, provider, backend, client, settings = system
    config = replace(portal.config, bindings={}, account_claim="account_alias")
    mapped = Connections(settings, backend, config, oidc_transport=provider.transport())
    app = Starlette(routes=[Mount("/connections", app=mapped.app())])
    with TestClient(app, base_url="https://example.com", follow_redirects=False) as browser:
        provider.tamper = {"account_alias": "first"}
        assert sign_in(mapped, provider, browser).status_code == 303
        assert "Example One" in browser.get("/connections/").text
        assert owner_post(browser, "/connections/start", headers={"origin": "https://example.com"}).status_code == 303
        provider.tamper = {"account_alias": "../second"}
        assert sign_in(mapped, provider, browser).status_code == 403
        assert browser.get("/connections/status").status_code == 401


def test_browser_identity_never_comes_from_proxy_headers(system):
    portal, provider, _, client, settings = system
    link = portal.flow.create(accounts.load(settings, "first"), signed_in=False)
    headers = {"X-Test-Account": "first", "X-Forwarded-User": "subject-one", "origin": "https://example.com"}
    assert client.get("/connections/status", headers=headers).status_code == 401
    assert client.post(path(link["url"]) + "/start", headers=headers).status_code == 404


def test_prefixed_proxy_login_preserves_only_the_owner_attempt_destination(system):
    portal, provider, backend, _, settings = system
    config = replace(portal.config, public_url="https://example.com/photos")
    settings = replace(settings, public_url=config.public_url)
    prefixed = Connections(settings, backend, config, oidc_transport=provider.transport())
    # The proxy strips /photos before forwarding; the browser retains that external prefix.
    app = Starlette(routes=[Mount("/connections", app=prefixed.app()), Mount("/connect", app=prefixed.viewer_app())])
    with TestClient(app, base_url="https://example.com", follow_redirects=False) as browser:
        link = prefixed.flow.create(accounts.load(settings, "first"), signed_in=False)
        external = path(link["url"])
        redirect = browser.get(external.removeprefix("/photos"))
        assert redirect.status_code == 303
        response = browser.get(redirect.headers["location"].removeprefix("/photos"))
        callback = path(provider.authorize(response.headers["location"]))
        done = browser.get(callback.removeprefix("/photos"))
        assert done.status_code == 303 and done.headers["location"] == external
        assert browser.get(external.removeprefix("/photos")).status_code == 200


def test_sign_in_links_require_oidc_configuration_at_startup(system, monkeypatch):
    from photos_mcp.server import create_server

    _, _, _, _, settings = system
    for name in ("ISSUER", "CLIENT_ID", "GROUP", "BINDINGS_FILE", "ACCOUNT_CLAIM"):
        monkeypatch.delenv("PHOTOS_CONNECTIONS_" + name, raising=False)
    with pytest.raises(ConfigError, match="Configure Photos connections"):
        from photos_mcp.server import Accounts

        create_server(Accounts.from_settings(settings))


def test_returning_home_resumes_one_attempt_and_refreshes_during_save(system):
    portal, provider, backend, client, _ = system
    assert sign_in(portal, provider, client).status_code == 303
    url = start(client)
    home = client.get("/connections/")
    assert "Sign-in in progress" in home.text and "Continue sign-in" in home.text
    assert url in home.text and home.headers["refresh"] == "2"
    origin = {"origin": "https://example.com"}
    client.post(url + "/start", headers=origin)
    attempt = next(iter(portal.flow.relay._links.values()))
    backend.states[attempt.window_id]["state"] = "saving"
    client.get(url + "/state")
    home = client.get("/connections/")
    assert "Saving connection" in home.text and "Continue sign-in" not in home.text
    assert home.headers["refresh"] == "2" and len(backend.started) == 1


def test_disconnect_blocks_the_mcp_account_registry_even_with_a_saved_snapshot(system):
    from test_accounts import publish

    from photos_mcp.errors import PhotosError
    from photos_mcp.server import Accounts, create_server

    portal, provider, backend, client, settings = system
    publish(settings, "first", "first@example.com")
    assert accounts.bind_email(settings, "first", "first@example.com")
    registry = Accounts.from_settings(settings, login=backend)
    create_server(registry, connections=portal.config)
    assert registry.get("first").account.id == "first"
    assert sign_in(portal, provider, client).status_code == 303
    backend.forget = lambda account: (_ for _ in ()).throw(RuntimeError("fake uncertain delete"))
    assert owner_post(client, "/connections/disconnect", headers={"origin": "https://example.com"}).status_code == 503
    with pytest.raises(PhotosError, match="connection_disconnected"):
        registry.get("first")
    assert registry.get("second").account.id == "second"
    # Removing the browser configuration must not turn a saved disconnect into access.
    reader_only = replace(settings, public_url=None, login_url=None, login_token=None)
    after_restart = Accounts.from_settings(reader_only)
    create_server(after_restart)
    with pytest.raises(PhotosError, match="connection_disconnected"):
        after_restart.get("first")
    assert after_restart.get("second").account.id == "second"


def test_default_owner_can_reconnect_without_changing_native_identity_or_storage(system):
    from test_accounts import write_meta_default

    portal, provider, backend, _, settings = system
    settings.session_dir.mkdir(exist_ok=True)
    write_meta_default(settings.session_dir)
    config = replace(portal.config, bindings={"subject-one": settings.default_account})
    managed = Connections(settings, backend, config, oidc_transport=provider.transport())
    app = Starlette(routes=[Mount("/connections", app=managed.app()), Mount("/connect", app=managed.viewer_app())])
    with TestClient(app, base_url="https://example.com", follow_redirects=False) as browser:
        assert sign_in(managed, provider, browser).status_code == 303
        home = browser.get("/connections/")
        assert "Connection needs attention" in home.text
        assert 'action="/connections/start"' in home.text and "Reconnect" in home.text
        entry = start(browser)
        assert browser.post(entry + "/start", headers={"origin": "https://example.com"}).status_code == 200
        assert backend.started[-1]["account"] == settings.default_account
        assert backend.started[-1]["email"] == settings.expected_email
        assert settings.expected_email not in backend.started[-1]["taken"]
        link = next(iter(managed.flow.relay._links.values()))
        assert managed.flow._bind(link, settings.expected_email) == "connected"
        assert managed.flow._bind(link, "different@example.com") == "in_use"
        assert (
            owner_post(browser, "/connections/disconnect", headers={"origin": "https://example.com"}).status_code == 303
        )
        assert not managed.account_allowed(settings.default_account)
        assert "browser profile is retained" in browser.get("/connections/").text
        assert owner_post(browser, "/connections/start", headers={"origin": "https://example.com"}).status_code == 303
        assert managed.account_allowed(settings.default_account)
        assert (settings.session_dir / "meta.json").exists()
        assert not accounts.load(settings, settings.default_account).linked
        assert not (settings.state_dir / "accounts" / settings.default_account).exists()


def test_account_alias_reassignment_does_not_transfer_a_pinned_owner(system):
    portal, provider, _, client, _ = system
    assert sign_in(portal, provider, client).status_code == 303
    start(client)
    assert portal.auth.store.owner("first") == (portal.config.issuer, "subject-one")
    portal.auth.bindings_config = replace(portal.config, bindings={"subject-two": "first"})
    assert sign_in(portal, provider, client, subject="subject-two").status_code == 403
    assert client.get("/connections/status").status_code == 401


def test_lost_controls_database_is_not_silently_reinitialized(system):
    from photos_mcp.connections_auth import BrowserStore

    portal, provider, _, client, _ = system
    assert sign_in(portal, provider, client).status_code == 303
    assert owner_post(client, "/connections/disconnect", headers={"origin": "https://example.com"}).status_code == 303
    portal.auth.store.path.unlink()
    with pytest.raises(ConfigError, match="durable connection controls"):
        BrowserStore(portal.config.state_dir / "connections")


@pytest.mark.skipif(__import__("os").name != "posix", reason="Hosted account admission uses POSIX locks")
def test_disconnect_drains_admitted_operations_and_refuses_new_work(system):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from photos_mcp.connection_controls import ConnectionAdmission
    from photos_mcp.errors import PhotosError

    portal, provider, backend, client, settings = system
    sign_in(portal, provider, client)
    admission = ConnectionAdmission(portal.auth.store)
    entered, release = Event(), Event()

    def old_call():
        with admission.operation("first"):
            entered.set()
            assert release.wait(5)

    portal.drain = admission.drain
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(old_call)
        assert entered.wait(5)

        # A timeout cannot reopen access or let the UI claim disconnection succeeded.
        def bounded_drain(account):
            admission.drain(account, timeout=0.02)

        portal.drain = bounded_drain
        try:
            assert (
                owner_post(client, "/connections/disconnect", headers={"origin": "https://example.com"}).status_code
                == 503
            )
            assert portal.auth.store.control("first")[1] == 2
            assert not backend.forgotten
            with pytest.raises(PhotosError, match="connection_disconnected"), admission.operation("first"):
                pytest.fail("A new provider operation was admitted")
        finally:
            release.set()
        old.result(timeout=5)
    portal.drain = admission.drain
    assert owner_post(client, "/connections/disconnect", headers={"origin": "https://example.com"}).status_code == 303
    assert backend.forgotten == ["first"]
    assert portal.auth.store.control("first")[1] == 1


def test_separate_browser_origin_preserves_data_link_origin(system):
    from dataclasses import replace

    portal, provider, backend, client, settings = system
    separate = Connections(settings, backend, replace(portal.config, public_url="https://connect.example.com"))
    link = separate.flow.create(accounts.load(settings, "first"), signed_in=False)
    assert link["url"].startswith("https://connect.example.com/connect/")
    assert settings.public_url == "https://example.com"


def test_concurrent_disconnect_cannot_delete_a_newer_owner_attempt(system):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    portal, provider, backend, client, settings = system
    sign_in(portal, provider, client)
    entered, release = Event(), Event()
    original = backend.forget

    def delayed_forget(account):
        entered.set()
        assert release.wait(5)
        original(account)

    backend.forget = delayed_forget
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(owner_post, client, "/connections/disconnect", headers={"origin": "https://example.com"})
        assert entered.wait(5)
        try:
            assert (
                owner_post(client, "/connections/disconnect", headers={"origin": "https://example.com"}).status_code
                == 409
            )
            assert (
                owner_post(client, "/connections/start", headers={"origin": "https://example.com"}).status_code == 409
            )
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 303
    start(client)
    assert portal.auth.store.control("first")[1] == 0
    assert backend.forgotten == ["first"]


def test_lost_controls_and_dangling_location_marker_refuse_reader_without_ui(system):
    import shutil

    from photos_mcp.config import ConfigError
    from photos_mcp.server import Accounts

    portal, provider, backend, client, settings = system
    marker = settings.state_dir / "connections-controls-initialized"
    shutil.rmtree(settings.state_dir / "connections")
    marker.unlink()
    marker.symlink_to(settings.state_dir / "missing-marker")
    with pytest.raises(ConfigError, match="controls"):
        Accounts.from_settings(settings)


def test_delayed_owner_forms_cannot_override_newer_disconnect_or_start(system):
    portal, provider, backend, client, settings = system
    sign_in(portal, provider, client)
    old = portal.auth.store.control("first")[0]
    assert owner_post(client, "/connections/disconnect", headers={"origin": "https://example.com"}).status_code == 303
    disconnected = portal.auth.store.control("first")
    stale = owner_post(client, "/connections/start", data={"revision": old}, headers={"origin": "https://example.com"})
    assert stale.status_code == 409
    assert 'href="/connections/"' in stale.text
    current = client.get("/connections/")
    assert current.status_code == 200
    assert f'name=revision value="{disconnected[0]}"' in current.text
    assert portal.auth.store.control("first") == disconnected
    start(client)
    new = portal.auth.store.control("first")
    assert (
        owner_post(
            client, "/connections/disconnect", data={"revision": old}, headers={"origin": "https://example.com"}
        ).status_code
        == 409
    )
    assert portal.auth.store.control("first") == new
    assert backend.forgotten == ["first"]


def test_repeated_origin_headers_do_not_authorize_owner_mutations(system):
    portal, provider, backend, client, settings = system
    sign_in(portal, provider, client)
    before = portal.auth.store.control("first")
    response = owner_post(
        client,
        "/connections/disconnect",
        headers=[("origin", "https://example.com"), ("origin", "https://other.example.com")],
    )
    assert response.status_code == 403
    assert portal.auth.store.control("first") == before
    assert not backend.forgotten


def test_default_recent_but_rejected_snapshot_offers_recovery_without_changing_budgets(system):
    import json
    from datetime import UTC, datetime

    from test_accounts import write_meta_default

    from photos_mcp.state import StateStore

    portal, _, _, _, settings = system
    settings.session_dir.mkdir()
    write_meta_default(settings.session_dir)
    meta = settings.session_dir / "meta.json"
    data = json.loads(meta.read_text())
    data["verified_at"] = datetime.now(UTC).isoformat()
    meta.write_text(json.dumps(data))
    state = StateStore(settings.state_dir, rpc_budget=3000, thumb_budget=2000)
    state.take("rpc")
    state.mark_dead(7, "session_expired")
    saved = state.path.read_bytes()
    status = portal.status({"account": settings.default_account})
    assert status["state"] == "check_needed" and status["label"] == "Connection needs attention"
    assert state.path.read_bytes() == saved


def test_pending_native_publication_blocks_new_reader_and_owner_start_until_recovery(system):
    from photos_mcp.connection_controls import pending_path

    portal, provider, backend, client, _ = system
    sign_in(portal, provider, client)
    marker = pending_path(portal.auth.store, "first")
    marker.write_text('{"id":"synthetic-commit","phase":"prepared"}')
    assert not portal.account_allowed("first")
    assert client.get("/connections/status").json()["state"] == "saving"
    assert owner_post(client, "/connections/start", headers={"origin": "https://example.com"}).status_code == 409
    assert not backend.started


@pytest.mark.parametrize("hint", ["signed_out", "error"])
def test_native_health_fact_matches_generation_and_survives_same_generation_reconnect(system, hint):
    import json
    from datetime import UTC, datetime

    from test_accounts import write_meta_default

    portal, _, _, _, settings = system
    settings.session_dir.mkdir()
    write_meta_default(settings.session_dir)
    meta = settings.session_dir / "meta.json"
    data = json.loads(meta.read_text())
    data["verified_at"] = datetime.now(UTC).isoformat()
    meta.write_text(json.dumps(data))
    keeper = settings.session_dir / "keeper.json"
    keeper.write_text(json.dumps({"state": hint, "generation": 6}))
    person = {"account": settings.default_account}
    assert portal.status(person)["state"] == "connected"
    keeper.write_text(json.dumps({"state": hint, "generation": 7}))
    evidence = keeper.read_bytes()
    expected = "not_connected" if hint == "signed_out" else "check_needed"
    assert portal.status(person)["state"] == expected
    portal.auth.store.set_control(settings.default_account, 0)
    assert portal.status(person)["state"] == expected
    assert keeper.read_bytes() == evidence
    data["generation"] = 8
    meta.write_text(json.dumps(data))
    assert portal.status(person)["state"] == "connected"
    assert keeper.read_bytes() == evidence


def test_hosted_controls_require_explicit_initialization_after_directory_loss(tmp_path, monkeypatch):
    import shutil

    from photos_mcp.config import ConfigError
    from photos_mcp.connections_auth import BrowserStore

    folder = tmp_path / "controls"
    monkeypatch.setenv("CONNECTIONS_REQUIRE_CONTROLS", "1")
    with pytest.raises(ConfigError):
        BrowserStore(folder)
    control = BrowserStore(folder, require_existing=False)
    control.set_control("default", 1)
    shutil.rmtree(folder)
    folder.mkdir(mode=0o700)
    with pytest.raises(ConfigError):
        BrowserStore(folder)
    with pytest.raises(ConfigError):
        BrowserStore(folder, require_existing=False)


@pytest.mark.parametrize("table", ["controls", "owners"])
def test_missing_durable_tables_are_not_recreated_from_disposable_sessions(tmp_path, table):
    from photos_mcp.connections_auth import BrowserStore

    store = BrowserStore(tmp_path / "controls")
    store.claim_owner("default", "https://identity.example.com", "owner")
    store.set_control("default", 1)
    with store.db() as db:
        db.execute("DROP TABLE " + table)
    with pytest.raises(ConfigError, match="durable connection tables"):
        BrowserStore(tmp_path / "controls")


def test_missing_owned_control_row_does_not_clear_disconnect(tmp_path):
    from photos_mcp.connections_auth import BrowserStore

    store = BrowserStore(tmp_path / "controls")
    store.claim_owner("default", "https://identity.example.com", "owner")
    store.set_control("default", 1)
    with store.db() as db:
        db.execute("DELETE FROM controls WHERE account=?", ("default",))
    with pytest.raises(ConfigError, match="durable account control"):
        store.control("default")
    with pytest.raises(ConfigError, match="durable account control"):
        store.claim_owner("default", "https://identity.example.com", "owner")


def test_missing_owner_row_cannot_transfer_an_adopted_account(tmp_path):
    from photos_mcp.connections_auth import BrowserStore

    store = BrowserStore(tmp_path / "controls")
    store.claim_owner("default", "https://identity.example.com", "owner")
    with store.db() as db:
        db.execute("DELETE FROM owners WHERE account=?", ("default",))
    for current in (store, BrowserStore(tmp_path / "controls")):
        with pytest.raises(ConfigError, match="durable account owner"):
            current.owner("default")
        with pytest.raises(ConfigError, match="durable account owner"):
            current.control("default")
        with pytest.raises(ConfigError, match="durable account owner"):
            current.claim_owner("default", "https://identity.example.com", "replacement")


def test_required_reader_cannot_ignore_replacement_of_the_whole_state_root(tmp_path, monkeypatch):
    from photos_mcp.config import Settings
    from photos_mcp.server import Accounts

    monkeypatch.setenv("CONNECTIONS_REQUIRE_CONTROLS", "1")
    with pytest.raises(ConfigError, match="durable connection controls"):
        Accounts(object(), Settings(expected_email="owner@example.com", state_dir=tmp_path / "new-state"))
