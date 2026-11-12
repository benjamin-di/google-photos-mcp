"""Conditional capture validates actual media identity and isolates retained image state."""

import json
from dataclasses import replace
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import BATCH, PAGE, app_page, load, make_image, make_transport, ok, rpc_body, wrb
from PIL import Image
from starlette.testclient import TestClient
from test_albums import ALBUM, key, record
from test_albums import google as google
from test_albums import svc as svc
from test_connection_binding import listing
from test_connection_binding import registry as registry
from test_server import _call, run

from photos_mcp.server import create_server


def detail_data():
    ext, info = load("fDcn4b"), load("VrseUb")
    ext[0][0] = info[0][0] = key(1)
    info[0][-1].pop("76647426", None)
    return ext, info


def capture_server(registry, tmp_path, clock, ext, info):
    token = listing(registry)["connection_epoch"]
    batch = rpc_body(wrb("fDcn4b", ext, "1"), wrb("VrseUb", info, "2"), wrb("VrseUb", info))
    transport, sender = make_transport(
        tmp_path,
        {
            PAGE: ok(app_page(), "text/html"),
            BATCH: ok(batch),
            ("GET", "lh3.googleusercontent.com"): ok(make_image(400, 300), "image/jpeg"),
            ("GET", "photos.fife.usercontent.google.com"): ok(make_image(400, 300), "image/jpeg"),
        },
        clock,
    )
    registry.default._t = transport
    registry.settings = replace(registry.settings, public_url="https://example.com/photos")
    return create_server(registry), token, sender


def invoke(server, tool, token):
    return run(_call(server, tool, {"ids": [key(1)], "expected_connection": token}, raise_on_error=False))


@pytest.mark.parametrize("returned_id", [None, 12, "invalid id", key(2), key(1)])
def test_conditional_album_details_reject_misattributed_extended_metadata(registry, tmp_path, clock, returned_id):
    token = listing(registry)["connection_epoch"]
    ext, _ = detail_data()
    ext[0][0] = returned_id
    meta = [None] * 22
    meta[0], meta[1], meta[21] = ALBUM, "Synthetic album", 1
    page = [None, [record(key(1))], None, meta]

    def batch(_method, _url, _params, data):
        requests = json.loads(parse_qs(data.decode() if isinstance(data, bytes) else data)["f.req"][0])[0]
        responses = {"snAcKc": page, "fDcn4b": ext}
        return ok(rpc_body(*(wrb(rpcid, responses.get(rpcid), pid) for rpcid, _, _, pid in requests)))

    transport, _ = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: batch}, clock)
    registry.default._t = transport
    result = run(
        _call(
            create_server(registry),
            "photos_list",
            {"source": "album", "album_id": ALBUM, "details": True, "limit": 1, "expected_connection": token},
            raise_on_error=False,
        )
    )
    body = json.loads(result.content[0].text)
    if returned_id == key(1):
        assert not result.is_error and body["items"][0]["id"] == key(1)
        assert body["connection_epoch"] == token and body["items"][0]["lat"] == 40.7696
        return
    assert result.is_error
    assert body["error"]["code"] == "schema_changed"
    assert "connection_epoch" not in body and "items" not in body


@pytest.mark.parametrize("tool", ["photos_get", "photos_download"])
@pytest.mark.parametrize("bad_id", [None, 12, "invalid id", key(2)])
def test_conditional_capture_rejects_missing_invalid_or_wrong_provider_id(registry, tmp_path, clock, tool, bad_id):
    ext, info = detail_data()
    info[0][0] = bad_id
    server, token, sender = capture_server(registry, tmp_path, clock, ext, info)
    result = invoke(server, tool, token)
    assert result.is_error or "error" in json.loads(result.content[0].text).get("items", [{}])[0]
    assert "schema_changed" in result.content[0].text
    assert '"url"' not in result.content[0].text
    assert all("usercontent" not in r["url"] for r in sender.requests)


@pytest.mark.parametrize("bad_ext", [None, "invalid id", key(2)])
def test_conditional_get_rejects_conflicting_extended_identity(registry, tmp_path, clock, bad_ext):
    ext, info = detail_data()
    ext[0][0] = bad_ext
    server, token, _ = capture_server(registry, tmp_path, clock, ext, info)
    result = invoke(server, "photos_get", token)
    body = json.loads(result.content[0].text)
    assert body["items"][0]["error"]["code"] == "schema_changed"


@pytest.mark.parametrize("tool", ["photos_get", "photos_download"])
def test_conditional_capture_success_and_changed_binding_refusal(registry, tmp_path, clock, tool):
    ext, info = detail_data()
    server, token, sender = capture_server(registry, tmp_path, clock, ext, info)
    result = invoke(server, tool, token)
    assert not result.is_error
    body = json.loads(result.content[0].text)
    assert body["connection_epoch"] == token
    assert body["items"][0]["id"] == key(1) and "error" not in body["items"][0]
    before = len(sender.requests)
    registry.admission.store.set_control("main", 0)
    refused = invoke(server, tool, token)
    assert refused.is_error and "connection_changed" in refused.content[0].text
    assert len(sender.requests) == before


@pytest.mark.parametrize("tool", ["photos_get", "photos_download"])
@pytest.mark.parametrize("bad_data", [[], {}, [None], ["malformed"]])
def test_malformed_detail_does_not_become_successful_partial_capture(registry, tmp_path, clock, tool, bad_data):
    ext, info = detail_data()
    server, token, _ = capture_server(registry, tmp_path, clock, ext, bad_data)
    result = invoke(server, tool, token)
    assert "schema_changed" in result.content[0].text
    assert '"url"' not in result.content[0].text


@pytest.mark.parametrize("tool", ["photos_get", "photos_download"])
@pytest.mark.parametrize("condition", ["pending", "unready", "unsupported", "budget"])
def test_capture_admission_and_budget_still_apply(registry, tmp_path, clock, tool, condition):
    from photos_mcp.connection_controls import pending_path

    ext, info = detail_data()
    server, token, sender = capture_server(registry, tmp_path, clock, ext, info)
    store = registry.admission.store
    if condition == "pending":
        pending_path(store, "main").write_text("{}")
    elif condition == "unready":
        with store.db() as db:
            db.execute("UPDATE accepted_publications SET ready=0 WHERE account='main'")
    elif condition == "unsupported":
        registry.conditional_native_account = None
    else:
        registry.settings = replace(registry.settings, daily_rpc_budget=0, daily_thumb_budget=0)
    result = invoke(server, tool, token)
    assert any(
        code in result.content[0].text for code in ("connection_changed", "connection_disconnected", "budget_exhausted")
    )
    assert '"url"' not in result.content[0].text
    if condition != "budget":
        assert sender.requests == []


def test_valid_partial_metadata_still_establishes_item_identity(registry, tmp_path, clock):
    ext, _ = detail_data()
    server, token, _ = capture_server(registry, tmp_path, clock, ext, None)
    result = invoke(server, "photos_get", token)
    body = json.loads(result.content[0].text)
    assert not result.is_error and body["items"][0]["filename"] == ext[0][2]
    assert body["items"][0]["type"] is None


@pytest.mark.parametrize("tool", ["photos_get", "photos_download"])
def test_capture_never_returns_success_after_publication_becomes_pending(registry, google, tool):
    from photos_mcp.connection_controls import pending_path

    ext, info = detail_data()
    google.responses.update(fDcn4b=ext, VrseUb=info)
    google.image = make_image(400, 300)
    token = listing(registry)["connection_epoch"]
    registry.settings = replace(registry.settings, public_url="https://example.com/photos")
    original = google.rpc

    def publish_started(payloads):
        result = original(payloads)
        pending_path(registry.admission.store, "main").write_text("{}")
        return result

    google.rpc = publish_started
    result = invoke(create_server(registry), tool, token)
    assert result.is_error and "connection_changed" in result.content[0].text
    assert '"url"' not in result.content[0].text and '"items"' not in result.content[0].text


def test_conditional_thumbnail_neither_reads_nor_writes_shared_cache(registry, google, monkeypatch):
    ext, info = detail_data()
    google.responses["VrseUb"] = info
    google.image = make_image(400, 300)
    token = listing(registry)["connection_epoch"]

    def forbidden(*_):
        pytest.fail("conditional thumbnail touched shared cache")

    monkeypatch.setattr(registry.default._cache, "get", forbidden)
    monkeypatch.setattr(registry.default._cache, "put", forbidden)
    result = registry.run(None, lambda service: {"image": service._thumbnail(key(1), 256)}, expected_connection=token)
    assert result["image"][0].startswith(b"\xff\xd8")


def test_download_re_resolution_rejects_different_provider_identity(registry, google):
    from photos_mcp.errors import PhotosError

    _, valid = detail_data()
    _, wrong = detail_data()
    wrong[0][0] = key(2)
    responses = [valid, wrong]
    google.responses["VrseUb"] = lambda _: responses.pop(0)

    def stale(_):
        raise PhotosError("item_unavailable")

    google.image = stale
    token = listing(registry)["connection_epoch"]
    registry.settings = replace(registry.settings, public_url="https://example.com/photos")
    result = invoke(create_server(registry), "photos_download", token)
    body = json.loads(result.content[0].text)
    assert body["items"][0]["error"]["code"] == "schema_changed"
    assert "url" not in body["items"][0] and responses == [] and len(google.gets) == 1


@pytest.mark.parametrize("tool", ["photos_get", "photos_download"])
def test_conditional_capture_uses_replaced_credentials_not_warm_metadata_or_jpeg(registry, tmp_path, clock, tool):
    from conftest import FakeSender, write_meta

    from photos_mcp.client import PhotosService
    from photos_mcp.connection_controls import PublicationGuard
    from photos_mcp.server import Accounts
    from photos_mcp.session import SessionReader
    from photos_mcp.transport import GoogleTransport

    transport, _ = make_transport(tmp_path, {}, clock)
    senders = []

    def sender(snapshot):
        current = "replacement-photos-osid" in snapshot.cookies.header_for("photos.google.com", "/", clock())
        ext, info = detail_data()
        ext[0][2] = "new.jpg" if current else "old.jpg"
        batch = rpc_body(wrb("fDcn4b", ext, "1"), wrb("VrseUb", info, "2"), wrb("VrseUb", info))
        result = FakeSender(
            {
                PAGE: ok(app_page(), "text/html"),
                BATCH: ok(batch),
                ("GET", "photos.fife.usercontent.google.com"): ok(
                    make_image(600 if current else 400, 300), "image/jpeg"
                ),
            }
        )
        senders.append(result)
        return result

    transport._sender_factory = sender
    registry.default._t = transport
    registry.settings = replace(registry.settings, public_url="https://example.com/photos")
    assert registry.default.get([key(1)])["items"][0]["filename"] == "old.jpg"
    old_jpeg = registry.default._thumbnail(key(1), 2048)[0]
    store = registry.admission.store
    with PublicationGuard(store, "main").snapshot(store.control("main")[0]):
        cookies = registry.settings.session_dir / "cookies.txt"
        cookies.write_text(cookies.read_text().replace("fake-photos-osid", "replacement-photos-osid"))
        write_meta(registry.settings.session_dir, 7)
    token = registry.run(None, lambda _: {}, connection_listing=True)["connection_epoch"]
    fresh_transport = GoogleTransport(
        SessionReader(registry.settings.session_dir),
        transport.state,
        expected_email=registry.settings.expected_email,
        sender_factory=sender,
        pacer=transport.pacer,
        image_pacer=transport.image_pacer,
        clock=clock,
    )
    restarted = Accounts(PhotosService(fresh_transport, registry.default._cache), registry.settings)
    for instance in (registry, restarted):
        server = create_server(instance)
        result = invoke(server, tool, token)
        assert not result.is_error
        body = json.loads(result.content[0].text)
        assert body["connection_epoch"] == token and "error" not in body["items"][0]
        if tool == "photos_get":
            assert body["items"][0]["filename"] == "new.jpg"
        else:
            assert body["items"][0]["width"] == 600
            # A later publication must never replace bytes already retained by the link.
            with PublicationGuard(store, "main").snapshot(store.control("main")[0]):
                pass
            path = urlsplit(body["items"][0]["url"]).path.removeprefix("/photos")
            with TestClient(server.http_app()) as client:
                response = client.get(path)
                assert response.status_code == 200
                assert Image.open(BytesIO(response.content)).size == (600, 300)
                assert client.get(path).status_code == 404
            token = registry.run(None, lambda _: {}, connection_listing=True)["connection_epoch"]
    assert registry.default._cache.get(key(1), 2048)[0] == old_jpeg
