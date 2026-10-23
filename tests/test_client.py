"""The service over a fake transport: request shapes, item shape, cursors (scope, recovery,
loop guard, TTL), albums, details, thumbnails with caching, contact sheets."""

from io import BytesIO

import pytest
from conftest import FakeTransport, all_fixtures, load, make_image
from PIL import Image

from photos_mcp.cache import ThumbCache
from photos_mcp.client import PhotosService, sized_url
from photos_mcp.cursors import CursorStore
from photos_mcp.errors import PhotosError

NULLS_24 = [None] * 24
NULLS_10 = [None] * 10
KEY = "AF1QipFAKE0000000000000000000000000000000000"
ITEM_KEYS = {"id", "url", "type", "taken_at", "tz_known", "filename", "description", "place", "favourite", "archived"}


def codes(excinfo):
    return excinfo.value.code


def make(state, tmp_path, responses=None, image=None, cursors=None):
    transport = FakeTransport(state, all_fixtures() if responses is None else responses, image)
    cache = ThumbCache(tmp_path / "cache", 50 * 1024 * 1024)
    return PhotosService(transport, cache, cursors or CursorStore()), transport


def test_list_library_request_shape_item_shape_and_filenames(service, transport):
    result = service.list()
    assert transport.rpcids == [["lcxiM"], ["EWgK9e"]]
    # source 1 = library only; always Google's page of 100, sliced to the limit.
    assert transport.calls[0][0][1] == [None, None, 100, None, 1, 1]
    keys = [item["id"] for item in result["items"]]
    assert transport.calls[1][0][1] == [[[[[k] for k in keys]], [[*NULLS_24, [], *NULLS_10, []]]]]
    first = result["items"][0]
    assert first == {
        "id": keys[0],
        "url": f"https://photos.google.com/photo/{keys[0]}",
        "type": "video",
        "taken_at": "2025-10-29T16:50:48+01:00",
        "tz_known": True,
        "filename": "file_164.mp4",
        "description": None,
        "place": first["place"],
        "favourite": False,
        "archived": False,
    }
    assert set(first) == ITEM_KEYS
    assert [item["type"] for item in result["items"]] == ["video", "video", "photo", "photo"]
    assert result["next_cursor"].startswith("c1.")
    assert result["coverage"] == {
        "pagination_complete": False,
        "shared_partner": "not_covered",
        "archive_included": False,
    }
    assert result["freshness"]["generation"] == 7


def test_archive_source(service, transport):
    result = service.list(source="archive", limit=5)
    assert transport.calls[0][0][1] == [None, None, 100, None, 1, 2]
    assert result["coverage"]["archive_included"] is True


def test_continuation_is_served_from_the_page_cache(service, transport):
    page1 = service.list(limit=2)
    page2 = service.list(limit=2, cursor=page1["next_cursor"])
    assert [i["id"] for i in page1["items"] + page2["items"]] == [r[0] for r in load("lcxiM")[0]]
    assert transport.rpcids.count(["lcxiM"]) == 1
    page3 = service.list(limit=2, cursor=page2["next_cursor"])
    assert transport.calls[-2][0][1] == ["FAKE_PAGE_TOKEN_01", None, 100, None, 1, 1]
    assert len(page3["items"]) == 2


def test_first_pages_are_never_cached(service, transport):
    service.list()
    service.list()
    assert transport.rpcids.count(["lcxiM"]) == 2


def test_cursor_is_scope_bound(service):
    page = service.search("dog", limit=1)
    with pytest.raises(PhotosError) as caught:
        service.list(cursor=page["next_cursor"])
    assert codes(caught) == "invalid_cursor"
    with pytest.raises(PhotosError) as caught:
        service.search("cat", cursor=page["next_cursor"])
    assert codes(caught) == "invalid_cursor"
    service.search("DOG", cursor=page["next_cursor"])  # same query, other case: same scope
    for bad in ("not-a-cursor", "c1.x.y.z", "c1." + "a" * 300):
        with pytest.raises(PhotosError) as caught:
            service.search("dog", cursor=bad)
        assert codes(caught) == "invalid_cursor"


def test_expired_cursor_is_recovered_and_flagged(state, tmp_path):
    now = [0.0]
    cursors = CursorStore(clock=lambda: now[0])
    service, transport = make(state, tmp_path, cursors=cursors)
    page1 = service.list(limit=1)
    now[0] += 3601  # TTL is 1 h
    page2 = service.list(limit=1, cursor=page1["next_cursor"])
    ids = [r[0] for r in load("lcxiM")[0]]
    assert [i["id"] for i in page2["items"]] == [ids[1]]  # skipped what was delivered
    assert page2["coverage"]["cursor_recovered"] is True
    assert page2["coverage"]["dedupe_required"] is True
    page3 = service.list(limit=1, cursor=page2["next_cursor"])
    assert [i["id"] for i in page3["items"]] == [ids[2]]
    assert "cursor_recovered" not in page3["coverage"]


def test_recovery_after_a_restart_walks_pages(state, tmp_path):
    def pages(payload):
        token = payload.data[0]
        if token is None:
            return [load("lcxiM")[0][:2], "T2"]
        if token == "T2":
            return [load("lcxiM")[0][2:], None]
        raise AssertionError(token)

    service, _ = make(state, tmp_path, {"lcxiM": pages, "EWgK9e": load("EWgK9e")})
    a = service.list(limit=2)
    b = service.list(limit=1, cursor=a["next_cursor"])
    restarted, transport = make(state, tmp_path, {"lcxiM": pages, "EWgK9e": load("EWgK9e")})
    c = restarted.list(limit=5, cursor=b["next_cursor"])
    ids = [r[0] for r in load("lcxiM")[0]]
    assert [i["id"] for i in c["items"]] == ids[3:]
    assert c["coverage"]["cursor_recovered"] and c["coverage"]["pagination_complete"]
    assert c["next_cursor"] is None


def test_loop_guard_stops_a_repeating_page_token(state, tmp_path):
    def pages(payload):
        return [load("lcxiM")[0][:1], "SAME"]

    service, _ = make(state, tmp_path, {"lcxiM": pages, "EWgK9e": load("EWgK9e")})
    first = service.list(limit=1)
    second = service.list(limit=1, cursor=first["next_cursor"])
    assert second["next_cursor"] is None
    assert second["coverage"]["loop_detected"] is True
    assert second["coverage"]["pagination_complete"] is False


def test_search_request_shape(service, transport):
    result = service.search("  beach ", limit=10)
    assert transport.calls[0][0] == ("EzkLib", ["beach", None, None])
    assert len(result["items"]) == 2
    assert result["coverage"]["archive_included"] is True
    for item in result["items"]:
        assert set(item) == ITEM_KEYS and item["place"] is None  # EzkLib records carry no place
    for bad in ("", "   ", "x" * 201):
        with pytest.raises(PhotosError) as caught:
            service.search(bad)
        assert codes(caught) == "invalid_argument"


def test_search_without_data_is_schema_changed(state, tmp_path):
    service, _ = make(state, tmp_path, {"EzkLib": None})
    with pytest.raises(PhotosError) as caught:
        service.search("dog")
    assert codes(caught) == "schema_changed"


def test_limit_bounds(service):
    for limit in (0, 51, True, "5"):
        with pytest.raises(PhotosError) as caught:
            service.list(limit=limit)
        assert codes(caught) == "invalid_argument"


def test_album_listing(service, transport):
    key = load("snAcKc")[3][0]
    result = service.list(source="album", album_id=key, limit=2)
    assert transport.calls[0][0] == ("snAcKc", [key, None, None, None])
    assert result["album"] == {"id": key, "url": f"https://photos.google.com/album/{key}", "title": "ny", "count": 3}
    assert len(result["items"]) == 2 and result["items"][0]["type"] == "video"
    assert set(result["items"][0]) == ITEM_KEYS and result["items"][0]["archived"] is None  # not in snAcKc
    assert result["coverage"]["archive_included"] is True
    rest = service.list(source="album", album_id=key, limit=2, cursor=result["next_cursor"])
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    assert rest["coverage"]["pagination_complete"] is True
    with pytest.raises(PhotosError):
        service.list(source="album")
    with pytest.raises(PhotosError):
        service.list(source="library", album_id=key)
    with pytest.raises(PhotosError):
        service.list(source="album", album_id="bad key!")


def test_unknown_album_is_item_unavailable(state, tmp_path):
    service, _ = make(state, tmp_path, {"snAcKc": None})
    with pytest.raises(PhotosError) as caught:
        service.list(source="album", album_id=KEY)
    assert codes(caught) == "item_unavailable"


def test_albums(service, transport):
    result = service.albums(limit=50)
    assert transport.calls[0][0][1] == [None, None, None, None, 1, None, None, 100, [2], 5]
    assert result["albums"][0] == {
        "id": result["albums"][0]["id"],
        "url": f"https://photos.google.com/album/{result['albums'][0]['id']}",
        "title": "location",
        "count": 1186,
    }
    assert result["albums"][1]["title"] == "ny"
    assert result["next_cursor"] is not None
    last = service.albums(limit=50, cursor=result["next_cursor"])
    assert last["albums"] == [] and last["coverage"]["pagination_complete"] is True


def test_small_album_pages_still_ask_google_for_a_full_page(service, transport):
    first = service.albums(limit=1)
    assert transport.calls[0][0][1][7] == 100
    assert len(first["albums"]) == 1 and first["next_cursor"] is not None
    second = service.albums(limit=1, cursor=first["next_cursor"])
    assert second["albums"][0]["title"] == "ny"


def test_get_merges_both_rpcs_in_one_post(service, transport):
    key = load("fDcn4b")[0][0]
    result = service.get([key])
    assert transport.rpcids == [["fDcn4b", "VrseUb"]]
    (item,) = result["items"]
    assert item["filename"] == "file_286.mov" and item["type"] == "video" and item["duration_s"] == 2.1
    assert item["taken_at"] == "2019-12-12T20:47:21-05:00" and item["tz_known"] is True
    assert item["uploaded_at"] == "2026-01-30T12:58:58Z"  # UTC, not the taken-at zone
    assert (item["width"], item["height"], item["size_bytes"]) == (1440, 1080, 908786)
    assert item["lat"] == pytest.approx(40.7696) and item["lon"] == pytest.approx(-73.9964)
    assert item["place"] == "TestUser5"
    assert [a["title"] for a in item["albums"]] == ["location", "ny"]
    assert item["favourite"] is False and item["archived"] is False
    assert set(item) >= ITEM_KEYS


def test_get_reports_missing_items_individually(state, tmp_path):
    service, _ = make(state, tmp_path, {"fDcn4b": None, "VrseUb": None})
    result = service.get([KEY])
    assert result["items"] == [{"id": KEY, "error": result["items"][0]["error"]}]
    assert result["items"][0]["error"]["code"] == "item_unavailable"


def test_session_errors_abort_multi_item_calls(state, tmp_path):
    service, _ = make(state, tmp_path, {"fDcn4b": PhotosError("session_expired")})
    with pytest.raises(PhotosError) as caught:
        service.get([KEY])
    assert codes(caught) == "session_expired"


def test_ids_validation(service):
    for ids in ([], ["../etc/passwd"], [KEY] * 0, [f"{KEY[:-2]}{i:02d}" for i in range(11)], "AF1Qip"):
        with pytest.raises(PhotosError) as caught:
            service.get(ids)
        assert codes(caught) == "invalid_argument"
    assert len(service.get([KEY, KEY])["items"]) == 1  # duplicates collapse


def test_view_resolves_sizes_and_caches(service, transport):
    key = load("VrseUb")[0][0]
    ((label, jpeg),) = service.view([key], max_px=256)
    assert transport.rpcids == [["VrseUb"]]
    assert transport.gets == ["https://photos.fife.usercontent.google.com/pw/ANON_596=w256-h256-k-no"]
    assert jpeg[:3] == b"\xff\xd8\xff"
    assert label["video_poster"] is True and label["type"] == "video"
    assert (label["width"], label["height"]) == (256, 192)
    ((again, jpeg2),) = service.view([key], max_px=256)
    assert again["from_cache"] is True and jpeg2 == jpeg and len(transport.gets) == 1
    assert transport.rpcids == [["VrseUb"]]  # nothing new went to Google
    with pytest.raises(PhotosError):
        service.view([key], max_px=100)
    with pytest.raises(PhotosError):
        service.view([key], max_px=2000)


def test_view_uses_listing_thumbnails_without_an_rpc(service, transport):
    listing = service.list(limit=1)
    key = listing["items"][0]["id"]
    service.view([key], max_px=512)
    assert transport.rpcids == [["lcxiM"], ["EWgK9e"]]  # no VrseUb
    assert transport.gets[0].endswith("=w512-h512-k-no")


def test_stale_listing_url_is_refreshed_once(state, tmp_path):
    jpeg = make_image(32, 32, "JPEG")

    def image(url):
        if "ANON_596" in url:
            return jpeg
        raise PhotosError("item_unavailable")

    responses = all_fixtures()
    service, transport = make(state, tmp_path, responses, image=image)
    service.list(limit=1)
    key = service.list(limit=1)["items"][0]["id"]
    ((label, data),) = service.view([key], max_px=256)
    assert data is not None and transport.rpcids[-1] == ["VrseUb"]


def test_view_reports_item_errors_inline(state, tmp_path):
    service, _ = make(state, tmp_path, image=b"<html>login</html>")
    key = load("VrseUb")[0][0]
    ((label, data),) = service.view([key])
    assert data is None and label["error"]["code"] == "item_unavailable"


def test_contact_sheet_layout_and_legend(state, tmp_path):
    keys = [r[0] for r in load("lcxiM")[0]]
    service, transport = make(state, tmp_path)
    service.list(limit=4)
    legend, sheet = service.contact_sheet(keys)
    assert [entry["n"] for entry in legend["legend"]] == [1, 2, 3, 4]
    assert legend["legend"][0] == {"n": 1, "id": keys[0], "date": "2025-10-29", "video": True}
    image = Image.open(BytesIO(sheet))
    assert image.format == "JPEG" and max(image.size) <= 1568
    assert (legend["width"], legend["height"]) == image.size
    assert all(url.endswith("=w256-h256-k-no") for url in transport.gets)


def test_contact_sheet_marks_failed_tiles(state, tmp_path):
    def image(url):
        raise PhotosError("media_session_required")

    service, _ = make(state, tmp_path, image=image)
    key = load("VrseUb")[0][0]
    legend, sheet = service.contact_sheet([key])
    assert legend["legend"] == [{"n": 1, "id": key, "error": "media_session_required"}]
    assert sheet[:3] == b"\xff\xd8\xff"


def test_contact_sheet_limits(service):
    with pytest.raises(PhotosError):
        service.contact_sheet([f"{KEY[:-2]}{i:02d}" for i in range(25)])


def test_status_never_calls_google(service, transport):
    status = service.status()
    assert transport.calls == [] and transport.gets == []
    assert status["session"]["state"] == "ok" and status["session"]["generation"] == 7
    assert status["changes"] == ["albums", "uploads"]
    assert status["budgets"]["rpc_left"] == 3000 and status["breaker"]["open"] is False
    service.record_error("blocked")
    service.state.trip_breaker()
    status = service.status()
    assert status["session"]["state"] == "blocked" and status["last_error"]["code"] == "blocked"


def test_status_without_a_snapshot(service, transport):
    transport.snapshot = None
    assert service.status()["session"] == {"state": "session_unavailable"}


def test_sized_url():
    assert sized_url("https://x/pw/ABC", 512) == "https://x/pw/ABC=w512-h512-k-no"
    assert sized_url("https://x/pw/ABC=w100-h100", 256) == "https://x/pw/ABC=w256-h256-k-no"


# -- optional filenames, batching, recovery, last_error ----------------------------------


@pytest.mark.parametrize("code", ["schema_changed", "budget_exhausted", "upstream_error", "busy"])
def test_filename_failure_returns_items_without_filenames(state, tmp_path, code):
    responses = all_fixtures()
    responses["EWgK9e"] = PhotosError(code)
    service, _ = make(state, tmp_path, responses)
    for result in (service.list(), service.search("beach")):
        assert result["items"] and all(item["filename"] is None for item in result["items"])


def test_filename_session_failure_still_fails_the_call(state, tmp_path):
    responses = all_fixtures()
    responses["EWgK9e"] = PhotosError("session_expired")
    service, _ = make(state, tmp_path, responses)
    with pytest.raises(PhotosError) as caught:
        service.list()
    assert codes(caught) == "session_expired"


def test_get_sends_every_id_in_one_batch(service, transport):
    keys = [load("fDcn4b")[0][0], KEY, f"{KEY[:-1]}9"]
    result = service.get(keys)
    assert transport.rpcids == [["fDcn4b", "VrseUb"] * 3]
    assert [item["id"] for item in result["items"]] == keys


def test_get_without_vrseub_has_unknown_type(state, tmp_path):
    responses = all_fixtures()
    responses["VrseUb"] = None
    service, _ = make(state, tmp_path, responses)
    (item,) = service.get([load("fDcn4b")[0][0]])["items"]
    assert item["type"] is None and item["archived"] is None and item["favourite"] is None


def test_get_batch_item_level_failure_marks_every_item(state, tmp_path):
    service, _ = make(state, tmp_path, {"fDcn4b": PhotosError("upstream_error")})
    result = service.get([KEY, f"{KEY[:-1]}9"])
    assert [item["error"]["code"] for item in result["items"]] == ["upstream_error"] * 2


def synthetic_items(count, prefix="AF1QipSYN"):
    template = load("lcxiM")[0][2]
    out = []
    for index in range(count):
        record = list(template)
        record[0] = f"{prefix}{index:05d}"
        out.append(record)
    return out


def test_library_cursor_recovery_reaches_beyond_five_times_the_limit(state, tmp_path):
    everything = synthetic_items(250)

    def pages(payload):
        token, size = payload.data[0], payload.data[2]
        start = int(token or 0)
        rest = everything[start : start + size]
        nxt = str(start + size) if start + size < len(everything) else None
        return [rest, nxt]

    now = [0.0]
    service, transport = make(
        state, tmp_path, {"lcxiM": pages, "EWgK9e": [[None, []]]}, cursors=CursorStore(clock=lambda: now[0])
    )
    cursor, seen = None, []
    for _ in range(7):  # 140 items at limit 20
        page = service.list(limit=20, cursor=cursor)
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
    now[0] += 3601  # the cursor expired
    recovered = service.list(limit=20, cursor=cursor)
    assert recovered["coverage"]["cursor_recovered"] is True
    assert [item["id"] for item in recovered["items"]] == [r[0] for r in everything[140:160]]
    assert len(set(seen)) == 140


def test_item_errors_reach_last_error_but_agent_mistakes_do_not(state, tmp_path):
    def image(url):
        raise PhotosError("media_session_required")

    service, _ = make(state, tmp_path, image=image)
    service.view([load("VrseUb")[0][0]])
    assert service.status()["last_error"]["code"] == "media_session_required"
    service.record_error("invalid_argument")
    service.record_error("invalid_cursor")
    assert service.status()["last_error"]["code"] == "media_session_required"


def test_view_and_sheet_fetch_thumbnails_concurrently_in_order(state, tmp_path):
    import threading
    import time

    lock, active, peak = threading.Lock(), [0], [0]
    keys = [r[0] for r in synthetic_items(12)]
    jpeg = make_image(64, 48, "JPEG")

    def image(url):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return jpeg

    responses = all_fixtures()
    responses["lcxiM"] = [synthetic_items(12), None]
    service, _ = make(state, tmp_path, responses, image=image)
    service.list(limit=12)
    legend, _ = service.contact_sheet(keys)
    assert [entry["id"] for entry in legend["legend"]] == keys
    assert 1 < peak[0] <= 4


def test_session_failure_in_one_tile_aborts_the_sheet(state, tmp_path):
    def image(url):
        raise PhotosError("blocked")

    service, _ = make(state, tmp_path, image=image)
    with pytest.raises(PhotosError) as caught:
        service.contact_sheet([load("VrseUb")[0][0], KEY])
    assert codes(caught) == "blocked"


# -- photos_canary -------------------------------------------------------------------------


def test_canary_checks_list_and_thumbnail_then_caches(state, tmp_path):
    now = [1_790_000_000.0]
    transport = FakeTransport(state, all_fixtures())
    service = PhotosService(transport, ThumbCache(tmp_path / "cache", 50 * 1024 * 1024), clock=lambda: now[0])
    now[0] = transport.snapshot.verified_at + 600
    first = service.canary()
    assert first["ok"] is True and first["code"] is None and first["cached"] is False
    assert first["generation"] == 7 and first["snapshot_age_s"] == 600
    assert transport.rpcids == [["lcxiM"]] and transport.calls[0][0][1][2] == 1  # an upstream page of 1
    assert len(transport.gets) == 1 and transport.gets[0].endswith("=w256-h256-k-no")
    calls = len(transport.calls)
    now[0] += 30 * 60
    second = service.canary()
    assert second["cached"] is True and second["ok"] is True and second["checked_at"] == first["checked_at"]
    assert len(transport.calls) == calls and len(transport.gets) == 1
    now[0] += 26 * 60  # 56 min after the real check: check again, bypassing the thumbnail cache
    third = service.canary()
    assert third["cached"] is False and len(transport.gets) == 2


def test_canary_reports_errors_and_staleness_without_raising(state, tmp_path):
    now = [0.0]
    transport = FakeTransport(state, {"lcxiM": PhotosError("session_expired")})
    service = PhotosService(transport, ThumbCache(tmp_path / "cache", 0), clock=lambda: now[0])
    now[0] = transport.snapshot.verified_at + 60
    result = service.canary()
    assert (result["ok"], result["code"]) == (False, "session_expired")
    assert service.status()["last_error"]["code"] == "session_expired"

    def boom(payload):
        raise RuntimeError("SID=secret")

    transport2 = FakeTransport(state, {"lcxiM": boom})
    service2 = PhotosService(transport2, ThumbCache(tmp_path / "cache2", 0), clock=lambda: now[0])
    assert service2.canary()["code"] == "internal_error"

    transport3 = FakeTransport(state, all_fixtures())
    service3 = PhotosService(transport3, ThumbCache(tmp_path / "cache3", 0), clock=lambda: now[0])
    now[0] = transport3.snapshot.verified_at + 10801
    stale = service3.canary()
    assert stale["ok"] is False and stale["code"] == "snapshot_stale" and stale["snapshot_age_s"] == 10801

    transport3.snapshot = None
    missing = service3.canary()  # cached real check, but no snapshot now
    assert missing["ok"] is False and missing["snapshot_age_s"] is None and missing["generation"] is None


def test_contact_sheet_of_24_tiles_is_fast_over_the_real_transport(tmp_path):
    """24 tiles took 21 s live with serial, RPC-paced image fetches. With the default pacers
    and 400 ms per image fetch (live: ~0.5 s), the sheet must stay well under 8 s."""
    import json
    import time

    from conftest import BATCH, EMAIL, PAGE, FakeSender, app_page, ok, rpc_body, session_dir, wrb

    from photos_mcp.session import SessionReader
    from photos_mcp.state import StateStore
    from photos_mcp.transport import GoogleTransport

    records = synthetic_items(24)
    for index, record in enumerate(records):
        record[1] = [f"https://photos.fife.usercontent.google.com/pw/ANON_T{index}", 640, 480]
    jpeg = make_image(640, 480, "JPEG")

    def batch(method, url, params, data):
        rpcid = params["rpcids"]
        answer = [records, None] if rpcid == "lcxiM" else [[None, []]]
        return ok(rpc_body(wrb(rpcid, answer)))

    def image(*_):
        time.sleep(0.4)
        return ok(jpeg, "image/jpeg")

    sender = FakeSender(
        {PAGE: ok(app_page(), "text/html"), BATCH: batch, ("GET", "photos.fife.usercontent.google.com"): image}
    )
    state = StateStore(tmp_path / "state", rpc_budget=3000, thumb_budget=2000)
    transport = GoogleTransport(
        SessionReader(session_dir(tmp_path)), state, expected_email=EMAIL, sender_factory=lambda snapshot: sender
    )
    service = PhotosService(transport, ThumbCache(tmp_path / "cache", 50 * 1024 * 1024))
    keys = [item["id"] for item in service.list(limit=24)["items"]]
    assert len(keys) == 24
    started = time.monotonic()
    legend, sheet = service.contact_sheet(keys)
    elapsed = time.monotonic() - started
    assert all("error" not in entry for entry in legend["legend"]) and sheet[:3] == b"\xff\xd8\xff"
    assert elapsed < 5.0, elapsed  # well under 8 s (serial: ~19 s; 4 at a time: ~2.5 s)
    assert json.dumps(legend)  # plain JSON


def _canary_service(state, tmp_path, responses):
    now = [0.0]
    transport = FakeTransport(state, responses)
    service = PhotosService(transport, ThumbCache(tmp_path / "cache", 0), clock=lambda: now[0])
    now[0] = transport.snapshot.verified_at + 60
    return service, transport, now


@pytest.mark.parametrize("code", ["upstream_error", "busy", "internal_error", "stale_token"])
def test_canary_rechecks_a_transient_failure_after_five_minutes(state, tmp_path, code):
    service, transport, now = _canary_service(state, tmp_path, {"lcxiM": PhotosError(code)})
    assert service.canary()["code"] == code
    now[0] += 4 * 60
    assert service.canary()["cached"] is True and len(transport.calls) == 1
    now[0] += 61
    transport.responses = all_fixtures()  # the blip is over
    again = service.canary()
    assert again["cached"] is False and again["ok"] is True


@pytest.mark.parametrize("code", ["session_expired", "blocked", "consent_required", "budget_exhausted"])
def test_canary_caches_a_lasting_failure_for_55_minutes(state, tmp_path, code):
    service, transport, now = _canary_service(state, tmp_path, {"lcxiM": PhotosError(code)})
    assert service.canary()["code"] == code
    now[0] += 54 * 60
    assert service.canary()["cached"] is True and len(transport.calls) == 1
    now[0] += 2 * 60
    assert service.canary()["cached"] is False and len(transport.calls) == 2


def test_canary_treats_a_future_verified_at_as_stale(state, tmp_path):
    service, transport, now = _canary_service(state, tmp_path, all_fixtures())
    now[0] = transport.snapshot.verified_at - 301
    result = service.canary()
    assert (result["ok"], result["code"], result["snapshot_age_s"]) == (False, "snapshot_stale", -301)
    now[0] = transport.snapshot.verified_at - 120  # small clock skew is tolerated
    assert service.canary()["ok"] is True


def test_canary_verdict_belongs_to_the_generation_it_checked(state, tmp_path):
    import dataclasses

    service, transport, now = _canary_service(state, tmp_path, {"lcxiM": PhotosError("session_expired")})
    assert service.canary()["code"] == "session_expired"
    # The exporter publishes a working generation: the next call checks it instead of
    # replaying the old generation's verdict for 55 minutes.
    transport.snapshot = dataclasses.replace(transport.snapshot, generation=transport.snapshot.generation + 1)
    transport.responses = all_fixtures()
    now[0] += 10 * 60
    fresh = service.canary()
    assert fresh["cached"] is False and fresh["ok"] is True and fresh["generation"] == 8
    now[0] += 10 * 60
    assert service.canary()["cached"] is True and len(transport.calls) == 2


def test_library_items_are_known_unarchived_from_their_source(state, tmp_path):
    # Recorded live: lcxiM library records end before the archived flag; the source says it.
    import json
    from pathlib import Path

    recorded = json.loads((Path(__file__).parent / "fixtures/recorded/lcxiM.library.json").read_text())["data"]
    transport = FakeTransport(state, {"lcxiM": recorded, "EWgK9e": [[None, []]]})
    service = PhotosService(transport, ThumbCache(tmp_path / "cache", 0))
    items = service.list(limit=5)["items"]
    assert items and all(item["archived"] is False for item in items)
