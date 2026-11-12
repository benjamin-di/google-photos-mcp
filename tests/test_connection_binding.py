"""Conditional calls use durable connection identity, never the native snapshot number."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from threading import Event

import pytest
from test_accounts import make_settings
from test_albums import ALBUM, key
from test_albums import google as google
from test_albums import svc as svc
from test_connect import owner_post
from test_connections import sign_in
from test_connections import system as system
from test_server import _call, run

from photos_mcp.connection_controls import PublicationGuard, pending_path
from photos_mcp.connections_auth import BrowserStore
from photos_mcp.errors import PhotosError
from photos_mcp.server import Accounts, create_server


@pytest.fixture
def registry(tmp_path, svc, monkeypatch):
    settings = make_settings(tmp_path, PHOTOS_DEFAULT_LOGIN_URL="http://127.0.0.1:8081")
    store = BrowserStore(settings.state_dir / "connections")
    store.claim_owner("main", "https://identity.example.com", "fake-owner")
    store.set_control("main", 0)
    with PublicationGuard(store, "main").snapshot(store.control("main")[0]):
        pass  # a successful synthetic native publication, before any conditional capture
    monkeypatch.setenv("PHOTOS_CONDITIONAL_NATIVE_ACCOUNT", "main")
    return Accounts(svc, settings)


def listing(registry, token=None, **kwargs):
    return registry.run(
        None,
        lambda svc: svc.list("album", ALBUM, 2, **kwargs),
        connection_listing=True,
        expected_connection=token,
    )


@pytest.mark.parametrize("later_page", [False, True])
@pytest.mark.parametrize(
    "malformation",
    [
        "null_items",
        "object_items",
        "text_items",
        "nonlist_record",
        "mixed_records",
        "invalid_id",
        "numeric_token",
        "boolean_token",
        "object_token",
        "list_token",
        "truncated",
        "missing_meta",
        "wrong_album",
    ],
)
def test_conditional_album_malformed_rpc_data_never_certifies_absence(
    registry, tmp_path, clock, later_page, malformation
):
    from conftest import BATCH, PAGE, app_page, make_transport, ok, rpc_body, wrb
    from test_albums import record

    token = listing(registry)["connection_epoch"]
    meta = [None] * 22
    meta[0], meta[1], meta[21] = ALBUM, "Synthetic album", 1
    data = [None, [], None, meta]
    if malformation == "null_items":
        data[1] = None
    elif malformation == "object_items":
        data[1] = {"unexpected": "items"}
    elif malformation == "text_items":
        data[1] = "unexpected"
    elif malformation == "nonlist_record":
        data[1] = [{"unexpected": "record"}]
    elif malformation == "mixed_records":
        data[1] = [record(key(1)), None]
    elif malformation == "invalid_id":
        data[1] = [record("invalid id")]
    elif malformation.endswith("_token"):
        data[2] = {"numeric_token": 12, "boolean_token": False, "object_token": {}, "list_token": []}[malformation]
    elif malformation == "truncated":
        data = [None]
    elif malformation == "missing_meta":
        data[3] = None
    else:
        data[3] = ["AF1QipOTHER"]
    first = [None, [record(key(1))], "synthetic-next", meta]
    transport, sender = make_transport(
        tmp_path,
        {
            PAGE: ok(app_page(), "text/html"),
            BATCH: ok(rpc_body(wrb("snAcKc", first if later_page else data))),
        },
        clock,
    )
    registry.default._t = transport
    server = create_server(registry)
    args = {"source": "album", "album_id": ALBUM, "expected_connection": token}
    if later_page:
        page = json.loads(run(_call(server, "photos_list", args)).content[0].text)
        assert not page["coverage"]["pagination_complete"] and page["next_cursor"]
        args["cursor"] = page["next_cursor"]
        sender.routes[BATCH] = ok(rpc_body(wrb("snAcKc", data)))
    result = run(_call(server, "photos_list", args, raise_on_error=False))
    assert result.is_error
    body = json.loads(result.content[0].text)
    assert body["error"]["code"] == "schema_changed"
    assert "connection_epoch" not in body and "coverage" not in body


@pytest.mark.parametrize("terminal_token", [None, ""])
def test_conditional_album_explicit_empty_page_remains_complete(registry, google, terminal_token):
    from photos_mcp.transport import RpcResult

    meta = [None] * 22
    meta[0], meta[1], meta[21] = ALBUM, "Synthetic empty album", 0
    google.rpc = lambda _: [RpcResult("snAcKc", "generic", [None, [], terminal_token, meta])]
    body = listing(registry)
    assert body["items"] == [] and body["next_cursor"] is None
    assert body["coverage"]["pagination_complete"] and body["connection_epoch"]


@pytest.mark.parametrize("bad_page", [1, 2, 3])
def test_conditional_removal_malformed_observation_preserves_invocation_phase(registry, google, bad_page):
    token = listing(registry)["connection_epoch"]
    original = google.rpc
    album_reads = 0

    def rpc(payloads):
        nonlocal album_reads
        results = original(payloads)
        if payloads[0].rpcid == "snAcKc":
            album_reads += 1
            if album_reads == bad_page:
                results[0].data[1] = None
        return results

    google.rpc = rpc
    result = run(
        _call(
            create_server(registry),
            "photos_album_remove",
            {
                "album_id": ALBUM,
                "ids": [key(1)],
                "expected_connection": token,
            },
            raise_on_error=False,
        )
    )
    assert result.is_error
    body = json.loads(result.content[0].text)
    error = body["error"]
    assert error["code"] == "schema_changed"
    assert "connection_epoch" not in body and "coverage" not in body
    if bad_page < 3:
        assert error["mutation"] == {"phase": "before_write", "definitely_unsent": True}
        assert google.writes == []
    else:
        assert error["mutation"] == {"phase": "read_back", "definitely_unsent": False}
        assert len(google.writes) == 1 and not error["retryable"]


def test_reconnect_with_equal_snapshot_generation_refuses_old_read_and_write(registry, google):
    captured = listing(registry)
    generation = captured["freshness"]["generation"]
    registry.admission.store.set_control("main", 0)
    called = []
    for operation in (lambda svc: svc.album_remove(ALBUM, [key(1)]), lambda svc: called.append("read")):
        with pytest.raises(PhotosError, match="connection_changed"):
            registry.run(None, operation, expected_connection=captured["connection_epoch"])
    assert not called and not google.writes
    with PublicationGuard(registry.admission.store, "main").snapshot(registry.admission.store.control("main")[0]):
        pass
    replacement = listing(registry)
    assert replacement["freshness"]["generation"] == generation
    assert replacement["connection_epoch"] != captured["connection_epoch"]


def test_capture_during_reconnect_waits_for_accepted_native_promotion(registry, google, tmp_path):
    from test_default_recovery import snapshot

    from photos_maintenance.human_state import NativePromotion, target_store
    from photos_maintenance.outcomes import HoldReason

    store = registry.admission.store
    guard = PublicationGuard(store, "main")
    original = listing(registry)["connection_epoch"]
    promotion = NativePromotion(tmp_path / "native", tmp_path / "export", guard)
    promotion.profile.mkdir(parents=True)
    (promotion.profile / "identity").write_text("synthetic accepted profile")
    snapshot(promotion.export, 7)
    state = target_store(promotion.root)
    hold = state.record_hold("google", HoldReason.SIGN_IN_BLOCKED)
    # The owner start path rotates/enables controls while accepted credentials remain readable.
    store.set_control("main", 0)
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry)
    promotion.stage()
    (promotion.candidate / "identity").write_text("synthetic replacement profile")
    snapshot(promotion.snapshot, 7)  # equal native number cannot establish an accepted connection
    (promotion.snapshot / "keeper.json").write_text('{"state":"signed_in","email":"owner@example.com"}')
    promotion.promote(guard, store.control("main")[0], generation=7, hold_id=hold.hold_id)
    accepted = listing(registry)["connection_epoch"]
    assert accepted != original
    with pytest.raises(PhotosError, match="connection_changed"):
        registry.run(None, lambda svc: svc.album_remove(ALBUM, [key(1)]), expected_connection=original)
    assert google.writes == []


def test_failed_publication_durably_invalidates_binding_until_new_acceptance(registry):
    store = registry.admission.store
    token = listing(registry)["connection_epoch"]
    epoch = store.control("main")[0]
    with pytest.raises(RuntimeError, match="interrupted"), PublicationGuard(store, "main").snapshot(epoch):
        raise RuntimeError("interrupted after external snapshot writes")
    # Reopen the database: rollback of the publication transaction must not erase the hold.
    registry.admission.store = BrowserStore(store.path.parent)
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry, token)
    with PublicationGuard(store, "main").snapshot(epoch):
        pass
    assert listing(registry)["connection_epoch"] != token


def test_keeper_error_status_after_partial_snapshot_cannot_clear_publication_hold(registry, tmp_path, monkeypatch):
    from test_maintenance import Harness

    from photos_maintenance.outcomes import IdentityOutcome
    from photos_maintenance.snapshot import COOKIES_FILE

    harness = Harness(tmp_path / "keeper")
    harness.keeper._connection_guard = PublicationGuard(registry.admission.store, "main")
    assert harness.run(allow=False).outcome == IdentityOutcome.PUBLISHED
    assert listing(registry)["connection_epoch"]  # successful snapshot plus status must stay usable

    def partial_publication(export, staging):
        # The actual keeper failure chain: replace cookies, fail before metadata,
        # return an error result, then write keeper.json through the status path.
        (export / COOKIES_FILE).write_bytes((staging / COOKIES_FILE).read_bytes())
        raise OSError("synthetic interruption before metadata replacement")

    monkeypatch.setattr("photos_maintenance.keeper.publish_snapshot", partial_publication)
    assert harness.run(allow=False).outcome != IdentityOutcome.PUBLISHED
    assert json.loads((harness.export / "keeper.json").read_text())["state"] == "error"
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry)


def test_status_writes_never_establish_or_clear_snapshot_acceptance(registry):
    store = registry.admission.store
    guard = PublicationGuard(store, "main")
    epoch = store.control("main")[0]
    token = listing(registry)["connection_epoch"]
    with guard.status(epoch):
        pass
    assert listing(registry, token)["connection_epoch"] == token
    with pytest.raises(RuntimeError), guard.snapshot(epoch):
        raise RuntimeError("partial external snapshot")
    with guard.status(epoch):
        pass
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry)
    with guard.snapshot(epoch):
        pass
    assert listing(registry)["connection_epoch"] != token


def test_legacy_publication_cannot_establish_snapshot_receipt(registry):
    store = registry.admission.store
    guard = PublicationGuard(store, "main")
    epoch = store.control("main")[0]
    with guard.publish(epoch):
        pass
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry)


def test_status_without_prior_snapshot_never_establishes_binding(registry):
    store = registry.admission.store
    with store.db() as db:
        db.execute("DELETE FROM accepted_publications")
    with PublicationGuard(store, "main").status(store.control("main")[0]):
        pass
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry)


def test_completed_publication_in_same_epoch_invalidates_earlier_capture(registry):
    store = registry.admission.store
    token = listing(registry)["connection_epoch"]
    with PublicationGuard(store, "main").snapshot(store.control("main")[0]):
        pass
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry, token)
    assert listing(registry)["connection_epoch"] != token


def test_intermediate_refresh_during_reconnect_cannot_survive_later_replacement(registry, google):
    store = registry.admission.store
    store.set_control("main", 0)
    epoch = store.control("main")[0]
    guard = PublicationGuard(store, "main")
    # A maintenance publication can finish while a human reconnect is still open.
    with guard.snapshot(epoch):
        pass
    captured = listing(registry)["connection_epoch"]
    with guard.snapshot(epoch):
        pass  # the later human replacement publishes under the same connection epoch
    with pytest.raises(PhotosError, match="connection_changed"):
        registry.run(None, lambda svc: svc.album_remove(ALBUM, [key(1)]), expected_connection=captured)
    assert google.writes == []


def interrupt_publication(folder, epoch):
    import os

    store = BrowserStore(folder)
    with PublicationGuard(store, "main").snapshot(epoch):
        os._exit(83)


def test_process_interruption_cannot_roll_back_the_durable_publication_hold(registry):
    import multiprocessing

    store = registry.admission.store
    token = listing(registry)["connection_epoch"]
    epoch = store.control("main")[0]
    child = multiprocessing.get_context("fork").Process(target=interrupt_publication, args=(store.path.parent, epoch))
    child.start()
    child.join(5)
    assert child.exitcode == 83
    registry.admission.store = BrowserStore(store.path.parent)
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry, token)


def test_additive_publication_migration_on_copied_synthetic_controls(tmp_path):
    from photos_mcp.connection_controls import ConnectionAdmission

    original = BrowserStore(tmp_path / "original")
    original.claim_owner("main", "https://identity.example.com", "fake-owner")
    original.set_control("main", 0)
    epoch = original.control("main")[0]
    copied_folder = tmp_path / "copy"
    copied_folder.mkdir()
    with original.db() as source, sqlite3.connect(copied_folder / original.path.name) as target:
        source.backup(target)
    copied = BrowserStore(copied_folder)
    for store in (original, copied):
        with (
            pytest.raises(PhotosError, match="connection_changed"),
            ConnectionAdmission(store).bound_operation("main", "synthetic-scope"),
        ):
            pytest.fail("legacy controls must not authorize conditional capture")
    guard = PublicationGuard(copied, "main")
    with guard.snapshot(epoch):
        pass
    with ConnectionAdmission(copied).bound_operation("main", "synthetic-scope") as token:
        assert len(token) == 64
    with guard.snapshot(epoch):
        pass
    with (
        pytest.raises(PhotosError, match="connection_changed"),
        ConnectionAdmission(copied).bound_operation("main", "synthetic-scope", token),
    ):
        pytest.fail("a subsequent publication must invalidate the original token")
    assert copied.control("main") == original.control("main") == (epoch, 0)
    assert copied.owner("main") == original.owner("main")
    with original.db() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='accepted_publications'").fetchone()


def test_changed_account_scope_and_unsupported_custody_cannot_use_token(registry):
    token = listing(registry)["connection_epoch"]
    registry.settings = replace(registry.settings, expected_email="replacement@example.com")
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry, token)
    registry.conditional_native_account = None
    assert "connection_epoch" not in listing(registry)
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry, token)
    with pytest.raises(PhotosError, match="connection_changed"):
        registry.run("second", lambda _: pytest.fail("unsupported provider call"), expected_connection=token)


def test_equal_generation_replacement_uses_current_credentials_in_warm_and_fresh_processes(
    tmp_path, clock, monkeypatch
):
    from conftest import BATCH, PAGE, FakeSender, app_page, make_transport, ok, rpc_body, wrb, write_meta
    from test_albums import record

    from photos_mcp.cache import ThumbCache
    from photos_mcp.client import PhotosService
    from photos_mcp.session import SessionReader
    from photos_mcp.transport import GoogleTransport

    settings = make_settings(tmp_path, PHOTOS_DEFAULT_LOGIN_URL="http://127.0.0.1:8081")
    store = BrowserStore(settings.state_dir / "connections")
    store.claim_owner("main", "https://identity.example.com", "fake-owner")
    store.set_control("main", 0)
    monkeypatch.setenv("PHOTOS_CONDITIONAL_NATIVE_ACCOUNT", "main")
    transport, _ = make_transport(tmp_path, {}, clock)
    senders = []

    def sender(snapshot):
        current = "replacement-photos-osid" in snapshot.cookies.header_for("photos.google.com", "/", clock())
        meta = [None] * 22
        meta[0], meta[1], meta[21] = ALBUM, "Synthetic album", 1
        batch = rpc_body(wrb("snAcKc", [None, [record(key(2 if current else 1))], None, meta]))
        result = FakeSender({PAGE: ok(app_page(), "text/html"), BATCH: ok(batch)})
        senders.append(result)
        return result

    transport._sender_factory = sender
    service = PhotosService(transport, ThumbCache(settings.cache_dir, 1024 * 1024))
    registry = Accounts(service, settings)
    guard = PublicationGuard(store, "main")
    epoch = store.control("main")[0]
    with guard.snapshot(epoch):
        pass
    # Warm BOTH SessionReader and the HTTP context with the old accepted credentials.
    assert service.list("album", ALBUM)["items"][0]["id"] == key(1)
    with guard.snapshot(epoch):
        cookies = settings.session_dir / "cookies.txt"
        cookies.write_text(cookies.read_text().replace("fake-photos-osid", "replacement-photos-osid"))
        write_meta(settings.session_dir, 7)  # replacement uses exactly the same native number
    captured = listing(registry)
    fresh_transport = GoogleTransport(
        SessionReader(settings.session_dir),
        transport.state,
        expected_email=settings.expected_email,
        sender_factory=sender,
        pacer=transport.pacer,
        image_pacer=transport.image_pacer,
        upload_pacer=transport.upload_pacer,
        clock=clock,
    )
    restarted = Accounts(PhotosService(fresh_transport, service._cache), settings)
    observed = listing(restarted, captured["connection_epoch"])
    assert captured["freshness"]["generation"] == observed["freshness"]["generation"] == 7
    assert captured["connection_epoch"] == observed["connection_epoch"]
    assert [item["id"] for item in captured["items"]] == [item["id"] for item in observed["items"]] == [key(2)]
    assert any(
        "replacement-photos-osid" in request["headers"].get("cookie", "")
        for instance in senders
        for request in instance.requests
    )


def test_initial_or_missing_controls_never_mint_binding(registry):
    store = registry.admission.store
    with store.db() as db:
        db.execute("UPDATE controls SET epoch='initial' WHERE account='main'")
    assert "connection_epoch" not in listing(registry)
    with pytest.raises(PhotosError, match="connection_changed"):
        listing(registry, "0" * 64)


@pytest.mark.parametrize("change_kind", ["reconnect", "disconnect", "publisher"])
def test_bound_mutation_serializes_epoch_rotation_and_native_publication(registry, google, change_kind):
    token = listing(registry)["connection_epoch"]
    store = registry.admission.store
    epoch = store.control("main")[0]
    entered, release, competing = Event(), Event(), Event()
    original = google.write

    def write(payloads):
        entered.set()
        assert release.wait(5)
        assert store.control("main") == (epoch, 0)
        return original(payloads)

    google.write = write

    def change():
        competing.set()
        if change_kind == "publisher":
            with PublicationGuard(store, "main").snapshot(epoch):
                return "published"
        store.set_control("main", 2 if change_kind == "disconnect" else 0)
        return change_kind

    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation = pool.submit(
            registry.run, None, lambda svc: svc.album_remove(ALBUM, [key(1)]), expected_connection=token
        )
        assert entered.wait(5)
        blocked = pool.submit(change)
        try:
            assert competing.wait(5)
            with pytest.raises(TimeoutError):
                blocked.result(timeout=0.05)
        finally:
            release.set()
        assert mutation.result(timeout=5)["connection_epoch"] == token
        assert blocked.result(timeout=5) == ("published" if change_kind == "publisher" else change_kind)
    assert len(google.writes) == 1


def test_pending_publication_before_or_during_call_never_returns_confirmation(registry):
    token = listing(registry)["connection_epoch"]
    pending = pending_path(registry.admission.store, "main")
    pending.write_text("{}")
    with pytest.raises(PhotosError):
        registry.run(None, lambda _: pytest.fail("pending call"), expected_connection=token)
    pending.unlink()

    def publication_started(_):
        pending.write_text("{}")
        return {"absent": True}

    with pytest.raises(PhotosError, match="connection_changed"):
        registry.run(None, publication_started, expected_connection=token)


def test_bound_listing_ignores_shared_pages_and_album_metadata(registry, google, monkeypatch):
    first = listing(registry)
    google.albums[ALBUM] = [key(9)]
    registry.default._album_auth[ALBUM] = "stale-auth"
    registry.default._album_shared[ALBUM] = True
    monkeypatch.setattr(registry.default, "_page", lambda *_: pytest.fail("shared page cache used"))
    fresh = listing(registry, first["connection_epoch"])
    assert [item["id"] for item in fresh["items"]] == [key(9)]
    assert registry.default._album_auth[ALBUM] == "stale-auth"
    assert registry.default._album_shared[ALBUM] is True


def test_mcp_conditional_removal_nested_refusal_is_proven_before_write(registry, google):
    server = create_server(registry)
    captured = run(_call(server, "photos_list", {"source": "album", "album_id": ALBUM}))
    token = json.loads(captured.content[0].text)["connection_epoch"]
    registry.admission.store.set_control("main", 0)
    result = run(
        _call(
            server,
            "photos_album_remove",
            {
                "album_id": ALBUM,
                "ids": [key(1)],
                "expected_connection": token,
            },
            raise_on_error=False,
        )
    )
    error = json.loads(result.content[0].text)["error"]
    assert error["code"] == "connection_changed"
    assert error["mutation"] == {"phase": "before_write", "definitely_unsent": True}
    assert google.writes == []


def test_pending_publication_after_applied_removal_is_unknown(registry, google):
    token = listing(registry)["connection_epoch"]
    original = google.write

    def write(payloads):
        result = original(payloads)
        pending_path(registry.admission.store, "main").write_text("{}")
        return result

    google.write = write
    result = run(
        _call(
            create_server(registry),
            "photos_album_remove",
            {
                "album_id": ALBUM,
                "ids": [key(1)],
                "expected_connection": token,
            },
            raise_on_error=False,
        )
    )
    error = json.loads(result.content[0].text)["error"]
    assert error["mutation"] == {"phase": "write_attempted", "definitely_unsent": False}
    assert not error["retryable"] and len(google.writes) == 1


def test_busy_connection_control_never_claims_disconnect_success(system, monkeypatch):
    portal, provider, backend, client, _ = system
    sign_in(portal, provider, client)

    def busy(*_):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(portal.auth.store, "set_control", busy)
    response = owner_post(client, "/connections/disconnect", headers={"origin": "https://example.com"})
    assert response.status_code == 409
    assert backend.forgotten == []
    assert portal.auth.store.control("first")[1] == 0
