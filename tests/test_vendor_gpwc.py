"""The vendored gpwc framing: request shape (as verified live 2026-10-14), every envelope of a
chunk, and reading WIZ_global_data."""

import json
from urllib.parse import parse_qs

from conftest import GLOBALS, app_page, rpc_body, wrb

from photos_mcp.vendor.gpwc import Client, parse_api_response, parse_envelopes, parse_main_page, payloads


def test_single_rpc_request_shape_matches_the_web_app():
    client = Client(GLOBALS)
    payload = payloads.GetLibraryPageByTakenDate(page_id=None, source="library", page_size=3)
    url, query, body = client.build_request([payload])
    assert url == "https://photos.google.com/_/PhotosUi/data/batchexecute"
    assert query == {
        "rpcids": "lcxiM",
        "source-path": "/",
        "f.sid": GLOBALS["FdrFJe"],
        "bl": GLOBALS["cfb2h"],
        "rt": "c",
    }
    form = parse_qs(body)
    assert form["at"] == [GLOBALS["SNlM0e"]]
    assert json.loads(form["f.req"][0]) == [[["lcxiM", "[null,null,3,null,1,1]", None, "generic"]]]


def test_batched_rpcs_get_numbered_ids():
    client = Client(GLOBALS)
    batch = [payloads.GetItemInfoExt("AF1QipFAKE01"), payloads.GetItemInfo("AF1QipFAKE01")]
    _, query, body = client.build_request(batch)
    assert query["rpcids"] == "fDcn4b,VrseUb"
    sent = json.loads(parse_qs(body)["f.req"][0])[0]
    assert [entry[3] for entry in sent] == ["1", "2"]


def test_every_envelope_of_a_chunk_is_read():
    batch = [payloads.GetItemInfoExt("AF1QipFAKE01"), payloads.GetItemInfo("AF1QipFAKE01")]
    Client(GLOBALS).build_request(batch)
    # Two envelopes in ONE chunk line: upstream gpwc read only the first.
    body = rpc_body(wrb("fDcn4b", [["x"]], "1"), wrb("VrseUb", [["y"]], "2")).decode()
    responses = parse_api_response(body, batch)
    assert [(r.rpcid, r.response_id, r.data, r.success) for r in responses] == [
        ("fDcn4b", "1", [["x"]], True),
        ("VrseUb", "2", [["y"]], True),
    ]


def test_failed_rpc_has_no_data_and_er_entries_are_seen():
    payload = payloads.GetSearchPage("dog")
    Client(GLOBALS).build_request([payload])
    body = rpc_body(wrb("EzkLib", None)).decode()
    (response,) = parse_api_response(body, [payload])
    assert response.success is False and response.data is None
    er = rpc_body(["er", None, None, None, None, 400, None, None, None, 3]).decode()
    assert [entry[0] for entry in parse_envelopes(er)] == ["er"]
    assert parse_api_response(er, [payload]) == []


def test_missing_response_id_falls_back_to_unique_rpcid():
    payload = payloads.GetSearchPage("dog")
    Client(GLOBALS).build_request([payload])
    entry = wrb("EzkLib", [[], None])[:3]
    (response,) = parse_api_response(rpc_body(entry).decode(), [payload])
    assert response.response_id == "generic" and response.data == [[], None]


def test_parse_main_page_reads_gd_script_and_falls_back():
    assert parse_main_page(app_page().decode())["oPEP7c"] == GLOBALS["oPEP7c"]
    bare = f"<html><script>window.WIZ_global_data = {json.dumps(GLOBALS)};</script></html>"
    assert parse_main_page(bare)["SNlM0e"] == GLOBALS["SNlM0e"]
    tricky = '<script data-id="_gd">window.WIZ_global_data = {"a": "};x", "oPEP7c": "me@x"};</script>'
    assert parse_main_page(tricky) == {"a": "};x", "oPEP7c": "me@x"}
    assert parse_main_page("<html><body>Sign in</body></html>") is None
    assert parse_main_page("") is None
