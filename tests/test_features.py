"""Links, list sources and filters (favourites, recently added, date ranges, media type), details, "on this day"
and photo cards: what each sends to Google and what comes back."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, date, datetime

import pytest
from conftest import FakeTransport, all_fixtures, load
from fastmcp import Client

from photos_mcp import client as client_mod
from photos_mcp.cache import ThumbCache
from photos_mcp.client import PhotosService
from photos_mcp.cursors import CursorStore
from photos_mcp.errors import PhotosError
from photos_mcp.server import VIEW_META, VIEW_URI, create_server

HOUR = 3_600_000
DAY = 24 * HOUR


def ms(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


def record(key: str, taken: int, *, tz: int = 0, video: bool = False) -> list:
    """A minimal library record (``rpc.parse_item``'s fields), as Google's pages carry them."""
    raw: list = [key, ["https://photos.fife.usercontent.google.com/pw/FAKE", 100, 100], taken, "dedup", tz, taken]
    if video:
        raw.append({"76647426": [4000]})
    return raw


def key(n: int) -> str:
    return f"AF1QipFAKE{n:04d}" + "0" * 30


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def make_service(state, tmp_path, clock):
    def build(responses: dict) -> tuple[PhotosService, FakeTransport]:
        transport = FakeTransport(state, {**all_fixtures(), **responses})
        cache = ThumbCache(tmp_path / "cache", 50 * 1024 * 1024)
        service = PhotosService(transport, cache, CursorStore(), clock=clock)
        return service, transport

    return build


# -- links ------------------------------------------------------------------------------------------------------------


def test_items_and_albums_link_to_google_photos(service):
    item = service.list()["items"][0]
    assert item["url"] == f"https://photos.google.com/photo/{item['id']}"
    album = service.albums()["albums"][0]
    assert album["url"] == f"https://photos.google.com/album/{album['id']}"
    listed = service.list("album", album["id"])
    assert (
        listed["items"][0]["url"] == f"https://photos.google.com/album/{album['id']}/photo/{listed['items'][0]['id']}"
    )
    assert listed["album"]["url"] == album["url"]


def test_get_adds_the_link_source_and_quality(service):
    item = service.list()["items"][2]
    got = service.get([item["id"]])["items"][0]
    assert got["url"] == item["url"]
    assert "source" in got and "original_quality" in got
    for album in got["albums"]:
        assert album["url"] == f"https://photos.google.com/album/{album['id']}"


# -- sources ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "data"),
    [("favourites", ["Favorites", [[5, "8", 0, 9]], None]), ("recently_added", ["", [[4, "ra", 0, 0]], None])],
)
def test_favourites_and_recently_added_are_the_web_apps_own_views(service, transport, source, data):
    result = service.list(source)
    assert transport.calls[0] == [("EzkLib", data)]
    assert result["items"] and result["coverage"]["archive_included"] is True


def test_dates_only_with_the_timeline(service):
    for source, extra in (("favourites", {}), ("album", {"album_id": key(1)})):
        with pytest.raises(PhotosError) as caught:
            service.list(source, from_date="2024-01-01", **extra)
        assert caught.value.code == "invalid_argument"
    for bad in ({"from_date": "2024-02-30"}, {"from_date": "24-01-01"}, {"to_date": "1800-01-01"},
                {"from_date": "2024-03-02", "to_date": "2024-03-01"}):  # fmt: skip
        with pytest.raises(PhotosError):
            service.list(**bad)
    with pytest.raises(PhotosError):
        service.list(media="gifs")


# -- date ranges and media filters ------------------------------------------------------------------------------------


def test_a_date_range_starts_at_its_end_and_keeps_each_items_own_local_date(make_service):
    pages = {
        None: [
            [
                record(key(1), ms(2024, 3, 19, 1), tz=-8 * HOUR),  # 18 March, 17:00 local: in
                record(key(2), ms(2024, 3, 18, 23), tz=2 * HOUR),  # 19 March, 01:00 local: out
                record(key(3), ms(2024, 3, 17, 10)),  # in
                record(key(4), ms(2024, 3, 16, 10), video=True),  # in, a video
            ],
            "PAGE2",
        ],
        "PAGE2": [[record(key(5), ms(2024, 3, 15, 23)), record(key(6), ms(2024, 3, 14, 10))], "PAGE3"],
    }
    seen: list = []

    def lcxim(payload):
        seen.append(payload.data)
        return pages[payload.data[0]]

    service, transport = make_service({"lcxiM": lcxim})
    result = service.list(from_date="2024-03-16", to_date="2024-03-18")
    assert [i["id"] for i in result["items"]] == [key(1), key(3), key(4)]
    # The first page starts at the end of the range (plus the largest offset), the next follows Google's token.
    anchor = ms(2024, 3, 19, 0) + 14 * HOUR
    assert seen[0] == [None, anchor, 100, None, 1, 1]
    assert seen[1] == ["PAGE2", None, 100, None, 1, 1]
    # It stopped reading once the listing passed the range's start: no third page, nothing more to page through.
    assert len(seen) == 2 and result["next_cursor"] is None and result["coverage"]["pagination_complete"]
    videos = make_service({"lcxiM": lambda p: pages[p.data[0]]})[0].list(
        from_date="2024-03-16", to_date="2024-03-18", media="videos"
    )
    assert [i["id"] for i in videos["items"]] == [key(4)]


def test_a_media_filter_reads_on_until_it_has_matches(make_service, monkeypatch):
    monkeypatch.setattr(client_mod, "FILTER_TARGET", 2)
    pages = {
        None: [[record(key(1), ms(2024, 1, 3)), record(key(2), ms(2024, 1, 2))], "P2"],
        "P2": [[record(key(3), ms(2024, 1, 1), video=True)], "P3"],
        "P3": [[record(key(4), ms(2023, 12, 31), video=True)], "P4"],
        "P4": [[record(key(5), ms(2023, 12, 30), video=True)], None],
    }
    service, transport = make_service({"lcxiM": lambda p: pages[p.data[0]], "EzkLib": lambda p: pages[p.data[2]]})
    first = service.list(media="videos", limit=1)
    assert [i["id"] for i in first["items"]] == [key(3)]
    second = service.list(media="videos", limit=5, cursor=first["next_cursor"])
    assert [i["id"] for i in second["items"]] == [key(4)]
    rest = service.list(media="videos", limit=5, cursor=second["next_cursor"])
    assert [i["id"] for i in rest["items"]] == [key(5)] and rest["next_cursor"] is None
    photos = service.search("beach", media="photos")
    assert [i["id"] for i in photos["items"]] == [key(1), key(2)]


# -- details ----------------------------------------------------------------------------------------------------------


def test_details_add_place_coordinates_and_camera(service, transport):
    result = service.search("beach", limit=2, details=True)
    assert transport.rpcids[-1] == ["fDcn4b", "fDcn4b"]
    for item in result["items"]:
        assert {"lat", "lon", "camera"} <= set(item)
    with pytest.raises(PhotosError):
        service.search("beach", limit=21, details=True)
    plain = service.search("beach", limit=2)
    assert "lat" not in plain["items"][0]


# -- on this day ------------------------------------------------------------------------------------------------------


def test_on_this_day_reads_one_page_per_year_and_keeps_that_day(make_service):
    def lcxim(payload):
        anchor = payload.data[1]
        day = datetime.fromtimestamp((anchor - 14 * HOUR - DAY) / 1000, UTC).date()
        if day.year == 2021:
            return [[], None]  # the library starts in 2022
        records = [record(key(day.year * 10 + n), ms(day.year, day.month, day.day, 20 - n)) for n in range(3)]
        records.append(record(key(day.year * 10 + 9), ms(day.year, day.month, day.day - 1)))  # the day before
        return [records, "MORE"]

    service, transport = make_service({"lcxiM": lcxim})
    result = service.on_this_day("2026-10-22", years=10, per_year=2)
    assert result["date"] == "10-22" and result["years_checked"] == 5  # 2025 .. 2021, stops at the empty year
    assert [y["year"] for y in result["years"]] == [2025, 2024, 2023, 2022]
    assert all(len(y["items"]) == 2 and y["more"] for y in result["years"])
    assert all(i["taken_at"].startswith(f"{y['year']}-10-22") for y in result["years"] for i in y["items"])
    assert transport.rpcids.count(["lcxiM"]) == 5 and transport.rpcids[-1] == ["EWgK9e"]


def test_on_this_day_defaults_to_today_and_handles_29_february(make_service, clock):
    asked: list[date] = []

    def lcxim(payload):
        asked.append(datetime.fromtimestamp((payload.data[1] - 14 * HOUR - DAY) / 1000, UTC).date())
        return [[], None]

    service, _ = make_service({"lcxiM": lcxim})
    clock.now = ms(2028, 2, 29) / 1000
    service.on_this_day(years=1)
    assert asked == [date(2027, 2, 28)]
    with pytest.raises(PhotosError):
        service.on_this_day(years=21)


# -- photo cards ------------------------------------------------------------------------------------------------------


def test_show_returns_thumbnails_for_the_card_only(service):
    keys = [i["id"] for i in service.list()["items"][:2]]
    shown, view = service.show([{"id": keys[0], "note": "the one"}, {"id": keys[1]}], "Picks")
    assert shown["kind"] == "photos" and shown["title"] == "Picks"
    assert [i["n"] for i in shown["items"]] == [1, 2] and shown["items"][0]["note"] == "the one"
    assert shown["items"][0]["url"] == f"https://photos.google.com/photo/{keys[0]}"
    for k in keys:
        data = view["thumbs"][k]
        assert data.startswith("data:image/jpeg;base64,") and base64.b64decode(data.split(",", 1)[1])[:2] == b"\xff\xd8"
    assert "thumbs" not in json.dumps(shown)
    with pytest.raises(PhotosError):
        service.show([{"id": keys[0]}, {"id": keys[0]}])


def test_the_card_tool_and_its_page_are_registered(service):
    async def go():
        async with Client(create_server(service)) as c:
            tools = {t.name: t for t in await c.list_tools()}
            page = await c.read_resource(VIEW_URI)
            keys = [i["id"] for i in json.loads((await c.call_tool("photos_list", {})).content[0].text)["items"][:1]]
            result = await c.call_tool("photos_show", {"items": [{"id": keys[0]}]})
            return tools, page, result

    tools, page, result = run(go())
    meta = tools["photos_show"].meta or {}
    assert json.dumps(meta).count(VIEW_URI) >= 1
    html = page[0].text
    assert "ui/initialize" in html and "safeThumb" in html and '"photos.google.com"' in html
    assert "<script src" not in html and "http://" not in html  # loads nothing from anywhere
    assert result.structured_content["kind"] == "photos"
    assert VIEW_META in (result.meta or {})


def test_on_this_day_and_filters_are_tools(service):
    async def go():
        async with Client(create_server(service)) as c:
            listed = await c.call_tool("photos_list", {"source": "favourites", "media": "videos"})
            day = await c.call_tool("photos_on_this_day", {"date": "2026-10-22", "years": 1})
            return json.loads(listed.content[0].text), json.loads(day.content[0].text)

    listed, day = run(go())
    assert all(i["type"] == "video" for i in listed["items"])
    assert day["date"] == "10-22"
    assert load("lcxiM")  # fixtures in use
