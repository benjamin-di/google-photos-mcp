"""Only evidence before the mutation sender handoff proves an unsent removal."""

import json

import pytest
from conftest import BATCH, PAGE, app_page, make_transport, ok, rpc_body, status, wrb
from test_albums import ALBUM, key
from test_albums import google as google
from test_albums import svc as svc
from test_server import _call, run

from photos_mcp.errors import PhotosError
from photos_mcp.server import create_server
from photos_mcp.transport import NetworkError
from photos_mcp.vendor.gpwc import album_payloads


def evidence(error, phase):
    body = error.payload()
    assert body.get("mutation") == {"phase": phase, "definitely_unsent": phase == "before_write"}
    if phase != "before_write":
        assert not body["retryable"]
        assert "Do not repeat" in body["next_step"]
        assert "nothing was changed" not in body["next_step"]


@pytest.mark.parametrize("failure", ["busy", "budget_exhausted", "schema_changed"])
def test_applied_removal_then_readback_failure_is_not_unsent(svc, google, failure):
    before = svc._album_state

    def readback(album):
        if google.writes:
            raise PhotosError(failure)
        return before(album)

    svc._album_state = readback
    with pytest.raises(PhotosError) as caught:
        svc.album_remove(ALBUM, [key(1)])
    evidence(caught.value, "read_back")
    assert len(google.writes) == 1 and key(1) not in google.albums[ALBUM]


def test_local_write_budget_proves_unsent_and_does_not_mutate_shared_error(svc, google):
    svc.state.write_budget = 0
    with pytest.raises(PhotosError) as caught:
        svc.album_remove(ALBUM, [key(1)])
    evidence(caught.value, "before_write")
    assert google.writes == []
    original = PhotosError("busy")
    original.at_mutation("read_back")
    assert "mutation" not in original.payload()


def test_created_album_then_unsent_add_never_reports_whole_invocation_unsent(svc, google):
    original = google.write

    def write(payloads):
        if google.writes:
            raise PhotosError("budget_exhausted").at_mutation("before_write")
        return original(payloads)

    google.write = write
    result = run(
        _call(
            create_server(svc),
            "photos_album_create",
            {
                "title": "Synthetic album",
                "ids": [key(1)],
            },
            raise_on_error=False,
        )
    )
    error = json.loads(result.content[0].text)["error"]
    assert len(google.writes) == 1 and google.writes[0][0][0] == "OXvT9d"
    assert error["mutation"] == {"phase": "write_attempted", "definitely_unsent": False}
    assert not error["retryable"] and "Do not repeat" in error["next_step"]


@pytest.mark.parametrize("failure", [NetworkError("lost"), status(500), ok(b"malformed")])
def test_transport_unknown_reply_never_retries_or_claims_unsent(tmp_path, clock, failure):
    transport, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: failure}, clock)
    with pytest.raises(PhotosError) as caught:
        transport.write([album_payloads.RemoveItemsFromAlbum([key(1)])])
    evidence(caught.value, "write_attempted")
    assert len([r for r in sender.requests if r["method"] == "POST"]) == 1


def test_transport_rpc_budget_refusal_before_mutation_sender_is_proven(tmp_path, clock):
    transport, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html")}, clock, rpc_budget=0)
    with pytest.raises(PhotosError) as caught:
        transport.write([album_payloads.RemoveItemsFromAlbum([key(1)])])
    evidence(caught.value, "before_write")
    assert not [r for r in sender.requests if r["method"] == "POST"]


def test_transport_known_refusal_refresh_is_bounded_but_not_claimed_unsent(tmp_path, clock):
    transport, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: status(403)}, clock)
    with pytest.raises(PhotosError) as caught:
        transport.write([album_payloads.RemoveItemsFromAlbum([key(1)])])
    evidence(caught.value, "write_attempted")
    assert len([r for r in sender.requests if r["method"] == "POST"]) == 2


def test_transport_success_returns_once(tmp_path, clock):
    transport, sender = make_transport(
        tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: ok(rpc_body(wrb("ycV3Nd", [])))}, clock
    )
    assert transport.write([album_payloads.RemoveItemsFromAlbum([key(1)])])
    assert len([r for r in sender.requests if r["method"] == "POST"]) == 1


def test_mcp_sanitizes_unexpected_readback_failure_without_losing_uncertainty(svc, google):
    before = svc._album_state

    def readback(album):
        if google.writes:
            raise RuntimeError("private-exception-text")
        return before(album)

    svc._album_state = readback
    result = run(
        _call(
            create_server(svc),
            "photos_album_remove",
            {
                "album_id": ALBUM,
                "ids": [key(1)],
            },
            raise_on_error=False,
        )
    )
    text = result.content[0].text
    error = json.loads(text)["error"]
    assert "private-exception-text" not in text
    assert error["mutation"] == {"phase": "read_back", "definitely_unsent": False}
    assert not error["retryable"] and len(google.writes) == 1
