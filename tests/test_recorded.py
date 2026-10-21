"""Parsers and classifiers over recorded Google responses (``tests/fixtures/recorded/``, written
by ``scripts/capture-fixtures`` and anonymised by it), plus the capture tool and its
anonymiser. New recordings join them as more files and are checked the same way."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import (
    FIXTURES,
    PAGE,
    FakeTransport,
    all_fixtures,
    app_page,
    load,
    make_transport,
)

from photos_mcp import rpc
from photos_mcp.cache import ThumbCache
from photos_mcp.capture import (
    FIXED_E7,
    SYNTH_EPOCH_MS,
    SYNTH_INT_BASE,
    SYNTH_OFFSET_MS,
    Anonymiser,
    capture,
    rescrub,
    rpc_recording,
)
from photos_mcp.client import PhotosService
from photos_mcp.errors import PhotosError
from photos_mcp.state import StateStore
from photos_mcp.transport import HttpResponse

RECORDED = FIXTURES / "recorded"
ITEM_KEYS = {"id", "type", "taken_at", "tz_known", "filename", "description", "place", "favourite", "archived"}
RECORDINGS = sorted(RECORDED.glob("*.json")) if RECORDED.is_dir() else []


def check_recording(record: dict, tmp_path: Path) -> None:
    """Everything a recording must survive: its parser, the uniform item shape, and for HTTP
    answers the transport's classification."""
    assert record["format"] == 1
    if record["kind"] == "http":
        check_http(record, tmp_path)
        return
    rpcid, label, data = record["rpcid"], record["label"], record["data"]
    if rpcid in ("lcxiM", "EzkLib"):
        page = rpc.parse_library_page(data)
        assert page.items or label == "archive", label  # recorded live: an empty archive is []
        check_items(rpcid, data, tmp_path)
        if label == "archive":
            assert all(item.is_archived for item in page.items)
    elif rpcid == "snAcKc":
        assert rpc.parse_album_page(data).items is not None
        check_items(rpcid, data, tmp_path)
    elif rpcid == "Z5xsfc":
        albums, _ = rpc.parse_albums_page(data)
        assert all(album.album_key for album in albums)
    elif rpcid == "EWgK9e":
        assert rpc.parse_batch_info(data)
    elif rpcid == "fDcn4b":
        assert rpc.parse_item_info_ext(data) is not None
    elif rpcid == "VrseUb":
        info = rpc.parse_item_info(data)
        assert info is not None and info.thumbnail_url
        if label == "video":
            assert info.is_video
        if label == "archived":
            assert info.is_archived is True
    elif rpcid == "EzwWhf":
        rpc.parse_storage_quota(data)
    else:
        raise AssertionError(f"unexpected rpcid {rpcid}")


def check_items(rpcid: str, data, tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state", rpc_budget=3000, thumb_budget=2000)
    transport = FakeTransport(state, {rpcid: data, "EWgK9e": [[None, []]]})
    service = PhotosService(transport, ThumbCache(tmp_path / "cache", 0))
    if rpcid == "lcxiM":
        result = service.list(limit=50)
    elif rpcid == "EzkLib":
        result = service.search("recorded", limit=50)
    else:
        result = service.list(source="album", album_id="AF1QipREC000001", limit=50)
    for item in result["items"]:
        assert set(item) == ITEM_KEYS
        assert item["type"] in ("photo", "video") and isinstance(item["tz_known"], bool)
        assert item["taken_at"] is None or item["tz_known"] or item["taken_at"].endswith("Z")


def check_http(record: dict, tmp_path: Path) -> None:
    headers = {"content-type": record["content_type"] or "text/html"}
    if record["location"]:
        headers["location"] = record["location"]
    body = app_page() if record["has_gd"] else b"<!doctype html><html><body>Google Photos</body></html>"
    transport, _ = make_transport(tmp_path, {PAGE: HttpResponse(record["status"], headers, body)}, _Clock())
    from photos_mcp.vendor.gpwc import payloads

    with pytest.raises(PhotosError) as caught:
        transport.rpc([payloads.GetStorageQuota()])
    assert caught.value.code == record["expect"], record["label"]


class _Clock:
    now = 1_790_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("path", RECORDINGS, ids=[p.name for p in RECORDINGS])
def test_recorded_response(path, tmp_path):
    check_recording(json.loads(path.read_text()), tmp_path)


def test_the_checked_in_sample_covers_both_kinds():
    kinds = {json.loads(p.read_text())["kind"] for p in RECORDINGS}
    assert kinds == {"rpc", "http"}
    assert any(json.loads(p.read_text()).get("label") == "signed_out" for p in RECORDINGS)


# -- anonymiser -----------------------------------------------------------------------------


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)


def _shape(value):
    if isinstance(value, list):
        return [_shape(v) for v in value]
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    return type(value).__name__


@pytest.mark.parametrize("name", ["lcxiM", "EzkLib", "snAcKc", "Z5xsfc", "EWgK9e", "fDcn4b", "VrseUb", "EzwWhf"])
def test_anonymiser_keeps_structure_and_drops_every_string(name):
    original = load(name)
    record = rpc_recording(Anonymiser(), name, "x", original, max_items=1000)
    assert _shape(record["data"]) == _shape(original)
    leaked = {s for s in _strings(original) if len(s) >= 3} & set(_strings(record["data"]))
    assert not leaked, leaked


def test_anonymiser_is_stable_and_typed():
    a = Anonymiser()
    key = "AF1QipFAKEAbCdEf0123456789"
    assert a.string(key) == a.string(key) and a.string(key).startswith("AF1QipREC")
    assert a.string("https://photos.fife.usercontent.google.com/pw/secret=w100").startswith(
        "https://photos.fife.usercontent.google.com/ANON_"
    )
    assert a.string("https://evil.example.org/x").startswith("https://example.invalid/")
    assert a.string("someone@example.org").endswith("@example.com")
    assert "someone" not in a.string("someone@example.org")
    assert a.string("IMG_2041.HEIC").startswith("file_") and a.string("IMG_2041.HEIC").endswith(".heic")
    assert a.string("Paris, France").startswith("ANON_")
    # A dotted tail that is not a media extension is free text: nothing of it survives.
    for text in ("Lunch with Jane.Doe", "St.Ives", "51.5074,-0.1278"):
        assert a.string(text).startswith("ANON_")
    assert a.string("") == ""
    geo = a.value({rpc.EXT_GEO: [[488566000, 23522000], "Paris"]})
    assert geo[rpc.EXT_GEO][0] == [0, 0] and geo[rpc.EXT_GEO][1] == a.string("Paris")


def test_fdcn4b_coordinates_are_replaced():
    record = rpc_recording(Anonymiser(), "fDcn4b", "x", load("fDcn4b"))
    ext = rpc.parse_item_info_ext(record["data"])
    assert (round(ext.lat * 1e7), round(ext.lng * 1e7)) == FIXED_E7


# -- the capture run (fake transport; never live) ---------------------------------------------


def test_capture_records_every_rpc_and_the_signed_out_answer(state, tmp_path):
    responses = all_fixtures()
    archive = load("lcxiM")
    archive_items = [list(record) for record in archive[0]]
    for record in archive_items:
        record[13] = True

    def library(payload):
        return [archive_items, None] if payload.data[5] == 2 else load("lcxiM")

    responses["lcxiM"] = library
    archived_keys = {record[0] for record in archive_items}

    def item_info(payload):
        data = load("VrseUb")
        data[0][13] = payload.data[0] in archived_keys
        return data

    responses["VrseUb"] = item_info
    transport = FakeTransport(state, responses)
    service = PhotosService(transport, ThumbCache(tmp_path / "cache", 0))
    probes = []

    def probe(with_cookies):
        probes.append(with_cookies)
        if with_cookies:
            return HttpResponse(302, {"location": "https://consent.google.com/ml?continue=x&gl=US"}, b"secret")
        return HttpResponse(302, {"location": "https://www.google.com/photos/about/?hl=en"}, b"")

    out = tmp_path / "out"
    out.mkdir()
    written, failed = capture(service, probe, out, query="beach", max_items=2)
    assert probes == [False, True]
    assert not failed, failed
    rpcids = {json.loads((out / name).read_text()).get("rpcid") for name in written} - {None}
    assert rpcids == {"lcxiM", "EzkLib", "Z5xsfc", "snAcKc", "EWgK9e", "fDcn4b", "VrseUb", "EzwWhf"}
    assert {"VrseUb.video.json", "VrseUb.archived.json", "http.signed_out.json", "http.consent.json"} <= set(written)
    for name in written:
        text = (out / name).read_text()
        assert "secret" not in text and "continue=" not in text
        (tmp_path / name.replace(".", "_")).mkdir()
        check_recording(json.loads(text), tmp_path / name.replace(".", "_"))
    library_record = json.loads((out / "lcxiM.library.json").read_text())
    assert len(library_record["data"][0]) == 2  # trimmed to max_items


def test_capture_stops_on_a_session_level_failure(state, tmp_path):
    service = PhotosService(FakeTransport(state, {"lcxiM": PhotosError("session_expired")}), ThumbCache(tmp_path, 0))
    with pytest.raises(PhotosError):
        capture(service, lambda cookies: HttpResponse(200, {}, b""), tmp_path, query="x")


def test_coordinates_in_unexpected_positions_are_blanked():
    record = load("lcxiM")[0][0]
    record = [*record[:-1], [[407696000, -739964000], [40.7696, -73.9964], 1080], record[-1]]
    original = [[record], None]
    data = rpc_recording(Anonymiser(), "lcxiM", "x", original)["data"]
    stray = data[0][0][-2]
    assert stray[:2] == [[0, 0], [0.0, 0.0]]  # E7 and degree values gone
    assert SYNTH_INT_BASE < stray[2] < SYNTH_INT_BASE + 10_000  # a width: synthetic, magnitude gone
    item = rpc.parse_item(data[0][0])
    assert item.tz_offset == SYNTH_OFFSET_MS
    # Timestamps become January 2020 instants, in the order they had (taken before uploaded).
    assert SYNTH_EPOCH_MS <= item.taken_ts < item.upload_ts < SYNTH_EPOCH_MS + 31 * 86_400_000
    assert (item.width, item.height) != (1080, 1920) and item.width > 0 and item.height > 0


def test_known_offsets_stay_known_but_never_real():
    for name, get in (
        ("EzkLib", lambda d: [i.tz_offset for i in rpc.parse_library_page(d).items]),
        ("snAcKc", lambda d: [i.tz_offset for i in rpc.parse_album_page(d).items]),
        ("fDcn4b", lambda d: [rpc.parse_item_info_ext(d).tz_offset]),
        ("VrseUb", lambda d: [rpc.parse_item_info(d).tz_offset]),
        ("EWgK9e", lambda d: [i.tz_offset for i in rpc.parse_batch_info(d).values()]),
    ):
        before = get(load(name))
        after = get(rpc_recording(Anonymiser(), name, "x", load(name))["data"])
        assert after == [None if offset is None else SYNTH_OFFSET_MS for offset in before], name
        assert any(offset is not None for offset in before), name


@pytest.mark.parametrize("name", ["lcxiM", "EzkLib", "snAcKc", "Z5xsfc", "EWgK9e", "fDcn4b", "VrseUb", "EzwWhf"])
def test_no_number_about_the_account_survives(name):
    original = load(name)
    data = rpc_recording(Anonymiser(), name, "x", original, max_items=1000)["data"]
    kept = {n for n in _numbers(original) if abs(n) > 64} & set(_numbers(data))
    # SYNTH_INT_BASE + n values are what the anonymiser itself hands out.
    assert not {n for n in kept if not _synthetic(n)}, kept


def test_rescrub_is_stable_across_files(tmp_path):
    a = Anonymiser()
    for name, label in (("lcxiM", "library"), ("EWgK9e", "filenames")):
        record = {"format": 1, "kind": "rpc", "rpcid": name, "label": label, "captured_at": "2020-01-01"}
        record["data"] = load(name)
        (tmp_path / f"{name}.{label}.json").write_text(json.dumps(record))
        a.seed_timestamps(load(name))
    assert sorted(rescrub(tmp_path)) == ["EWgK9e.filenames.json", "lcxiM.library.json"]
    library = json.loads((tmp_path / "lcxiM.library.json").read_text())["data"]
    filenames = json.loads((tmp_path / "EWgK9e.filenames.json").read_text())["data"]
    keys = {i.media_key for i in rpc.parse_library_page(library).items}
    assert keys & set(rpc.parse_batch_info(filenames))  # the same item keeps one key in both files


def _numbers(value):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return []
    if isinstance(value, int | float):
        return [value]
    items = value.values() if isinstance(value, dict) else value
    return [n for v in items for n in _numbers(v)]


def _synthetic(n) -> bool:
    if n in (SYNTH_OFFSET_MS, *FIXED_E7) or SYNTH_EPOCH_MS <= n < SYNTH_EPOCH_MS + 31 * 86_400_000:
        return True
    return float(n).is_integer() and SYNTH_INT_BASE < abs(int(n)) < SYNTH_INT_BASE + 10_000
