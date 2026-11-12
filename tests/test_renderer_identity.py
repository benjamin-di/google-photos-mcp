"""Exercise actual owned shells with synthetic accounts and local renderer APIs."""

import dataclasses
import time
from functools import wraps
from types import SimpleNamespace

import pytest
from signin_window import connections as renderer
from signin_window.service import Window
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount
from starlette.testclient import TestClient
from test_connect import VNC_HTML, owner_post
from test_connections import path, sign_in, start
from test_connections import system as system  # noqa: F401
from test_default_recovery import native as native  # noqa: F401

from photos_login import app as login_app
from photos_maintenance import human
from photos_mcp import accounts, build_pages, connect, ui
from photos_mcp.build_identity import BuildIdentity

IDENTITY = BuildIdentity("photos-mcp", "9.8.7", "1234567890abcdef" * 2 + "12345678")
TOKEN = "fake-service-token" * 3


@pytest.fixture(autouse=True)
def host_build(monkeypatch):
    monkeypatch.setattr(ui, "BUILD", IDENTITY)
    monkeypatch.setattr(build_pages, "BUILD", IDENTITY)


def assert_identity(markup, *, legacy=False):
    shared_footers = sum(markup.count(f'<footer id="{name}"') for name in ("connection-identity", "page-identity"))
    legacy_footers = markup.count('<footer class="build-identity"')
    if hasattr(renderer, "PageIdentity"):
        assert shared_footers == 1
        assert f'data-service="{IDENTITY.service}"' in markup
        assert f'data-version="{IDENTITY.version}"' in markup
        assert f'data-source-revision="{IDENTITY.source_revision}"' in markup
        assert f"{IDENTITY.service} v{IDENTITY.version} · {IDENTITY.source_revision[:12]}<" in markup
        assert 'class="build-identity"' not in markup
    elif legacy:
        assert legacy_footers == 1 and shared_footers == 0
        assert f'title="{IDENTITY.source_revision}"' in markup
    else:
        assert legacy_footers == shared_footers == 0
        assert IDENTITY.source_revision not in markup


def test_optional_api_requires_both_public_type_and_explicit_factory_parameter(monkeypatch):
    def old_factory(title):
        return title

    def new_factory(*, identity=None):
        return identity

    monkeypatch.delattr(renderer, "PageIdentity", raising=False)
    assert ui.renderer_options(new_factory) == {}
    monkeypatch.setattr(renderer, "PageIdentity", BuildIdentity, raising=False)
    assert ui.renderer_options(old_factory) == {}
    identity = ui.renderer_options(new_factory)["identity"]
    assert identity == IDENTITY
    assert {field.name for field in dataclasses.fields(identity)} == {"service", "version", "source_revision"}


def test_home_auth_failure_notice_logout_and_freshness_pages(system):
    portal, provider, backend, client, _ = system
    failed = client.get("/connections/callback")
    assert failed.status_code == 403
    assert_identity(failed.text)
    assert client.get("/connections/status").status_code == 401
    assert sign_in(portal, provider, client).status_code == 303
    home = client.get("/connections/")
    assert home.status_code == 200
    assert_identity(home.text)
    assert "Sign-in required" in home.text and "Example One" in home.text
    assert '<form method=post action="/connections/start">' in home.text
    assert 'name=revision value="initial"' in home.text
    refused = owner_post(
        client, "/connections/start", data={"revision": "stale"}, headers={"origin": "https://example.com"}
    )
    assert refused.status_code == 409 and "This connection changed" in refused.text
    assert_identity(refused.text)
    wrong_origin = owner_post(client, "/connections/start", headers={"origin": "https://other.example.com"})
    assert wrong_origin.status_code == 403
    request = Request({"type": "http", "headers": [], "session": {}})
    refused = portal.auth.refused(request)
    assert refused.status_code == 401 and "Nothing was changed" in refused.body.decode()
    assert_identity(refused.body.decode())
    fresh = portal.auth.require_fresh(request, action="change this connection", max_age=300)
    assert fresh.status_code == 403 and "Confirm and continue" in fresh.body.decode()
    assert_identity(fresh.body.decode())
    status = client.get("/connections/status")
    assert status.status_code == 200 and IDENTITY.source_revision not in status.text
    assert backend.started == []
    logout = owner_post(client, "/connections/logout", headers={"origin": "https://example.com"})
    assert logout.status_code == 200
    assert_identity(logout.text)


@pytest.mark.parametrize("unmapped", [False, True])
def test_auth_denials_clear_browser_session_and_render_one_footer(system, unmapped):
    portal, provider, backend, client, _ = system
    denied = sign_in(
        portal,
        provider,
        client,
        subject="unbound-subject" if unmapped else "subject-one",
        groups=None if unmapped else [],
    )
    assert denied.status_code == 403
    assert_identity(denied.text, legacy=unmapped)
    assert client.get("/connections/status").status_code == 401
    assert backend.started == []
    if unmapped:
        assert "This sign-in cannot manage a Google Photos connection" in denied.text
        assert 'href="/connections/"' in denied.text
        assert "unbound-subject" not in denied.text and "fake-oidc-access-token" not in denied.text


def test_connection_adapter_keeps_relay_controls_and_vendor_viewer(system):
    portal, provider, backend, client, _ = system
    assert sign_in(portal, provider, client).status_code == 303
    attempt = start(client)
    page = client.get(attempt)
    assert page.status_code == 200
    assert_identity(page.text)
    assert '"autoStart": true' in page.text and '"reconnect": true' in page.text
    assert '"returnUrl": "/connections/"' in page.text
    assert "Check the connection status before starting another sign-in" in page.text
    assert page.headers["cache-control"] == "no-store" and page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-frame-options"] == "DENY"
    assert backend.started == []
    assert client.post(attempt + "/start", headers={"origin": "https://other.example.com"}).status_code == 404
    assert backend.started == []
    assert client.post(attempt + "/start", headers={"origin": "https://example.com"}).status_code == 200
    assert len(backend.started) == 1
    viewer = client.get(attempt + "/novnc/vnc.html")
    assert viewer.status_code == 200
    assert viewer.content == VNC_HTML.replace(b"</head>", b'<script type="module" src="paste.js"></script>\n</head>')
    assert b"<footer " not in viewer.content
    unavailable = client.get("/connect/" + "z" * 40)
    assert unavailable.status_code == 404
    assert_identity(unavailable.text)


@pytest.mark.parametrize("custom", [False, True])
def test_standalone_flow_forwards_host_identity_and_preserves_supplied_text(system, custom):
    _, _, backend, _, settings = system
    supplied = (
        dataclasses.replace(
            connect.TEXT, title="Synthetic sign-in", help_html="<b>Synthetic instructions</b>", return_url="/return"
        ).with_outcomes(failed="<h1>Synthetic failure</h1>")
        if custom
        else connect.TEXT
    )
    if custom and hasattr(renderer, "PageIdentity"):
        supplied = dataclasses.replace(supplied, identity=renderer.PageIdentity("fake-other-service", "0.0", "f" * 40))
    flow = connect.ConnectFlow(
        settings,
        backend,
        text=supplied if custom else None,
        authorize=lambda request, link, operation: operation == "page",
    )
    for field in dataclasses.fields(supplied):
        if field.name != "identity":
            assert getattr(flow.relay.text, field.name) == getattr(supplied, field.name)
    link = flow.create(accounts.load(settings, "first"), signed_in=False)
    app = Starlette(routes=[Mount("/connect", app=flow.app())])
    with TestClient(app, base_url="https://example.com") as browser:
        page = browser.get(path(link["url"]))
        assert page.status_code == 200
        assert_identity(page.text)
        assert '"autoStart": false' in page.text
        assert "Device passkeys do not work here" in page.text if not custom else "Synthetic instructions" in page.text
        assert "Synthetic failure" in page.text if custom else "Nothing was connected" in page.text
        assert "fake-other-service" not in page.text
        denied = browser.post(path(link["url"]) + "/start", headers={"origin": "https://example.com"})
        assert denied.status_code == 404 and backend.started == []
        unavailable = browser.get("/connect/" + "x" * 40)
        assert unavailable.status_code == 404
        assert_identity(unavailable.text)


@pytest.mark.parametrize("entrypoint", ["login", "human"])
def test_service_factories_forward_identity_and_keep_direct_view_disabled(native, monkeypatch, entrypoint):
    module = login_app if entrypoint == "login" else human
    factory = module.service_app
    seen = {}

    @wraps(factory)
    def capture(windows, **options):
        seen.update(windows=windows, options=options)
        return factory(windows, **options)

    monkeypatch.setattr(module, "service_app", capture)
    config, _, _ = native
    if entrypoint == "human":
        app = human.build_app(config, token=TOKEN, novnc_dir=config.root, require_policy=False)
    else:
        from signin_window.service import Windows

        from photos_login.profiles import Paths

        def no_browser(window):
            pytest.fail("No provider browser may start")

        windows = Windows(no_browser)
        app = login_app.build_app(
            windows,
            SimpleNamespace(),
            Paths(config.root / "profiles", config.export),
            windows.lock,
            token=TOKEN,
            novnc_dir=config.root,
            require_policy=False,
        )
    window = Window("w" * 24, "desktop", 1, time.monotonic(), 60.0)
    window.waiting("accounts.google.com")
    monkeypatch.setattr(seen["windows"], "get", lambda key: window if key == window.id else None)
    assert seen["options"]["direct_view"] is False
    browser = TestClient(app)
    url = f"/view/{window.id}?t={window.view_token}"
    assert browser.get(url).status_code == 401
    assert browser.get(url, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404
    state = browser.get(f"/logins/{window.id}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert state.status_code == 200 and state.json()["state"] == "waiting"
    assert IDENTITY.source_revision not in state.text and "identity" not in state.json()
    # Render the real shared shell with captured options; owned services never enable this route.
    viewer = TestClient(factory(seen["windows"], **{**seen["options"], "direct_view": True}))
    rendered = viewer.get(url)
    assert rendered.status_code == 200
    assert_identity(rendered.text)
