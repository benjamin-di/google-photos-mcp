"""Parsers against the recorded fixtures (paths documented in docs/PAYLOADS.md)."""

import pytest
from conftest import load

from photos_mcp import rpc
from photos_mcp.errors import PhotosError


def test_field_reads_positionally_and_from_sparse_tail():
    assert rpc.field(["a", "b"], 1) == "b"
    assert rpc.field(["a", {"3": "c"}], 2) == "c"  # key = index + 1
    assert rpc.field(["a", {"2": "x"}], 1) == "x"  # the tail sits at the requested index
    assert rpc.field(["a", None], 1, default="d") == "d"
    assert rpc.field([], 0) is None
    assert rpc.field(None, 0, default="d") == "d"
    assert rpc.field(["a"], -1) is None


def test_ext_and_path():
    record = ["k", ["thumb", 10, 20], {"76647426": [1234], "163238866": [False]}]
    assert rpc.ext(record, "76647426") == [1234]
    assert rpc.ext(record, "missing", default=0) == 0
    assert rpc.path(record, 1, 0) == "thumb"
    assert rpc.path(record, "76647426", 0) == 1234
    assert rpc.path(record, "163238866", 0) is False
    assert rpc.path(record, 5, 0, default="d") == "d"


def test_iso_time_uses_item_offset():
    assert rpc.iso_time(1576201641000, -18000000) == "2019-12-12T20:47:21-05:00"
    assert rpc.iso_time(1761753048000, 3600000) == "2025-10-29T16:50:48+01:00"
    # No usable offset: the instant in UTC marked Z, never a fake +00:00 local offset.
    assert rpc.iso_time(1761753048000, None) == "2025-10-29T15:50:48Z"
    assert rpc.iso_time(1761753048000, 10**12) == "2025-10-29T15:50:48Z"  # absurd offset → UTC
    assert rpc.iso_time(1761753048000, 0) == "2025-10-29T15:50:48+00:00"  # a known zero offset
    assert rpc.tz_known(0) and rpc.tz_known(-18000000)
    assert not rpc.tz_known(None) and not rpc.tz_known(10**12) and not rpc.tz_known(True)
    assert rpc.utc_time(1761753048000) == "2025-10-29T15:50:48Z"
    assert rpc.iso_time(None) is None
    assert rpc.iso_time("nope") is None
    assert rpc.iso_time(True) is None


def test_e7():
    assert rpc.e7(407696000) == pytest.approx(40.7696)
    assert rpc.e7(-739964000) == pytest.approx(-73.9964)
    assert rpc.e7(None) is None
    assert rpc.e7(True) is None


def test_library_page_lcxiM():
    page = rpc.parse_library_page(load("lcxiM"))
    assert len(page.items) == 4
    assert page.next_token == "FAKE_PAGE_TOKEN_01"
    video = page.items[0]
    assert video.media_key.startswith("AF1QipFAKE")
    assert video.is_video and video.duration_ms == 22932
    assert (video.width, video.height) == (1080, 1920)
    assert video.taken_ts == 1761753048000 and video.tz_offset == 3600000
    assert video.upload_ts == 1761755803160
    assert video.thumbnail_url.startswith("https://photos.fife.usercontent.google.com/pw/")
    assert video.is_archived is False and video.is_favorite is False
    assert any(not item.is_video for item in page.items[1:])  # the fixture mixes photos and videos
    assert all(isinstance(item.dedup_key, str) for item in page.items)


def test_search_page_EzkLib_has_same_item_shape():
    page = rpc.parse_library_page(load("EzkLib"))
    assert len(page.items) == 2
    assert page.next_token == "FAKE_PAGE_TOKEN_02"
    first = page.items[0]
    assert (first.width, first.height) == (3024, 4032)
    assert first.taken_ts == 1724514196859 and first.tz_offset == 10800000


def test_albums_page_Z5xsfc():
    albums, token = rpc.parse_albums_page(load("Z5xsfc"))
    assert len(albums) == 3
    assert token == "FAKE_PAGE_TOKEN_03"
    first = albums[0]
    assert first.title == "location" and first.item_count == 1186
    assert first.cover_thumbnail_url.startswith("https://photos.fife.usercontent.google.com/pw/")
    assert first.is_shared is False and first.auth_key is None
    assert albums[1].title == "ny" and albums[1].item_count == 3


def test_album_page_snAcKc():
    page = rpc.parse_album_page(load("snAcKc"))
    assert len(page.items) == 3
    assert page.next_token is None  # "" on the last page
    assert page.title == "ny" and page.item_count == 3
    assert page.album_key.startswith("AF1QipFAKE")
    assert page.items[0].is_video and page.items[0].duration_ms == 2084  # ext dict at index 9
    assert not page.items[1].is_video


def test_batch_info_EWgK9e():
    rows = rpc.parse_batch_info(load("EWgK9e"))
    assert len(rows) == 3
    info = next(iter(rows.values()))
    assert info.filename == "file_164.mp4"
    assert info.size_bytes == 39680079
    assert info.taken_ts == 1761753048000 and info.tz_offset == 3600000 and info.upload_ts == 1761755803160
    assert info.description is None  # "" becomes None
    assert rpc.parse_batch_info(None) == {}
    assert rpc.parse_batch_info([[None, "not a list"]]) == {}


def test_item_info_ext_fDcn4b():
    info = rpc.parse_item_info_ext(load("fDcn4b"))
    assert info is not None
    assert info.filename == "file_286.mov"
    assert info.description is None
    assert info.size_bytes == 908786
    assert (info.width, info.height) == (1440, 1080)
    assert info.taken_ts == 1576201641000 and info.tz_offset == -18000000
    assert info.lat == pytest.approx(40.7696) and info.lng == pytest.approx(-73.9964)
    assert info.location_name == "TestUser5"
    assert [a.title for a in info.albums] == ["location", "ny"]
    assert info.albums[1].item_count == 3
    assert info.source == ["mobile", "android"]
    assert info.camera is None
    assert info.takes_up_space is False and info.space_used_bytes == 0 and info.is_original_quality is True
    assert isinstance(info.dedup_key, str)
    assert rpc.parse_item_info_ext(None) is None
    assert rpc.parse_item_info_ext([None]) is None


def test_item_info_VrseUb_reads_sparse_tail():
    info = rpc.parse_item_info(load("VrseUb"))
    assert info is not None
    assert info.thumbnail_url == "https://photos.fife.usercontent.google.com/pw/ANON_596"  # tail key "13" → index 12
    assert info.download_url == "https://anon-google-url.example.com/ANON_595"
    assert info.download_original_url is None
    assert info.description is None
    assert info.is_video and info.duration_ms == 2084
    assert info.upload_ts == 1769777938628
    assert (info.width, info.height) == (1440, 1080)
    assert info.is_favorite is False and info.is_archived is False
    assert info.takes_up_space is False and info.is_original_quality is True and info.space_used_bytes == 0
    assert info.camera is None


def test_item_info_VrseUb_short_finds_extensions_at_index_9():
    info = rpc.parse_item_info(load("VrseUb_short"))
    assert info is not None
    assert info.is_video and info.duration_ms == 2084
    assert info.is_favorite is False
    assert info.is_archived is None  # the short (album) record does not say
    assert info.thumbnail_url.endswith("ANON_596")


def test_storage_quota_EzwWhf():
    assert rpc.parse_storage_quota(load("EzwWhf")) == (5217349942, 16106127360, 133544244)


def test_bad_shapes_raise_schema_changed():
    for call, arg in (
        (rpc.parse_library_page, "nope"),
        (rpc.parse_albums_page, None),
        (rpc.parse_album_page, 3),
        (rpc.parse_storage_quota, {}),
        (rpc.parse_item, [None, "no key"]),
    ):
        with pytest.raises(PhotosError) as caught:
            call(arg)
        assert caught.value.code == "schema_changed"
    assert rpc.parse_items(None) == []
    assert rpc.parse_items([None, 5]) == []


def test_item_description_and_place_from_extensions():
    record = [
        "AF1QipFAKE0000000000000000000000000000000001",
        ["https://photos.fife.usercontent.google.com/pw/X", 10, 20],
        1,
        "dedup",
        0,
        2,
        {
            "396644657": ["sunset over the river"],
            "129168200": [None, [[1, 2], None, None, None, [[None, [["Paris, France"]]]]]],
        },
    ]
    item = rpc.parse_item(record)
    assert item.description == "sunset over the river"
    assert item.place == "Paris, France"
    assert rpc.parse_item(record[:6]).place is None
