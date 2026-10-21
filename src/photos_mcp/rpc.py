"""Parsers for raw ``batchexecute`` results.

Every index used here comes from gpwc ``gpwc/parser.py`` or GPTK ``src/api/parser.ts``
and is cross-referenced in ``docs/PAYLOADS.md``. The functions are pure, so they are
unit-tested against the recorded fixtures in ``tests/fixtures``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .errors import PhotosError

# Extension field numbers found in the trailing dict of item and album records.
EXT_VIDEO = "76647426"  # [duration_ms, ...]
EXT_LIVE_PHOTO = "146008172"
EXT_FAVORITE = "163238866"  # [bool]
EXT_TRASH = "225032867"
EXT_SPACE = "318563170"  # [[takes_up_space, bytes, quality, ...]]
EXT_GEO = "129168200"
EXT_DESCRIPTION = "396644657"  # [text]
EXT_ALBUM = "72930366"  # [_, title, [timestamps...], item_count, is_shared, auth_key]

_ITEM_INFO_EXT_KEYS = ("15", EXT_VIDEO, EXT_LIVE_PHOTO, EXT_FAVORITE, EXT_TRASH, EXT_SPACE, "525000000")
_SOURCE = {1: "mobile", 2: "web", 3: "shared", 4: "partnerShared", 7: "drive", 8: "pc", 11: "gmail"}
_SOURCE_OS = {1: "android", 3: "ios"}
_MAX_TZ_MS = 14 * 3600 * 1000


# --- low-level access helpers -------------------------------------------------------


def _bad(why: str) -> PhotosError:
    """A parser invariant failed on a 200 answer: Google changed the format."""
    return PhotosError("schema_changed", log_detail=why)


def field(arr: Any, index: int, default: Any = None) -> Any:
    """Read ``arr[index]`` tolerating Google's sparse tails.

    A record whose trailing positions are mostly empty is serialised with the tail folded
    into a dict keyed by the 1-based field number (``{"13": url}`` stands for index 12).
    """
    if not isinstance(arr, list) or index < 0:
        return default
    size = len(arr)
    tail = arr[-1] if size and isinstance(arr[-1], dict) else None
    if index < size and not (tail is not None and index == size - 1):
        value = arr[index]
        return default if value is None else value
    if tail is not None:
        value = tail.get(str(index + 1))
        return default if value is None else value
    return default


def ext(arr: Any, key: str, default: Any = None) -> Any:
    """Read an extension entry from a record's trailing dict."""
    if isinstance(arr, list) and arr and isinstance(arr[-1], dict):
        value = arr[-1].get(key)
        return default if value is None else value
    return default


def path(data: Any, *steps: int | str, default: Any = None) -> Any:
    """Follow a mix of positional (int) and extension / dict (str) steps."""
    current = data
    for step in steps:
        if isinstance(step, int):
            current = field(current, step)
        elif isinstance(current, dict):
            current = current.get(step)
        elif isinstance(current, list):
            current = ext(current, step)
        else:
            return default
        if current is None:
            return default
    return current


def tz_known(tz_offset_ms: Any) -> bool:
    """Whether Google gave a usable UTC offset for the local time of an item."""
    return _is_num(tz_offset_ms) and abs(tz_offset_ms) <= _MAX_TZ_MS


def iso_time(ts_ms: Any, tz_offset_ms: Any = None) -> str | None:
    """Epoch milliseconds + offset milliseconds → ISO 8601 in that offset.

    Without a usable offset the instant is given in UTC with a ``Z`` suffix: true as an
    instant, and never mistaken for a known ``+00:00`` local offset (see ``tz_known``).
    """
    if not _is_num(ts_ms):
        return None
    known = tz_known(tz_offset_ms)
    tz = timezone(timedelta(milliseconds=tz_offset_ms)) if known else UTC
    try:
        text = datetime.fromtimestamp(ts_ms / 1000, tz).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None
    return text if known else text.replace("+00:00", "Z")


def utc_time(ts_ms: Any) -> str | None:
    """Epoch milliseconds → ISO 8601 UTC (``Z``), e.g. for upload times."""
    return iso_time(ts_ms, None)


def e7(value: Any) -> float | None:
    """Google encodes coordinates as integer degrees × 1e7."""
    return value / 1e7 if _is_num(value) else None


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _token(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _flag(value: Any, truthy: int) -> bool | None:
    return None if value is None else value == truthy


# --- list / search / album items ---------------------------------------------------------


@dataclass(frozen=True)
class RawItem:
    media_key: str
    thumbnail_url: str | None
    width: int | None
    height: int | None
    taken_ts: int | None
    tz_offset: int | None
    upload_ts: int | None
    dedup_key: str | None
    duration_ms: int | None
    is_video: bool
    is_favorite: bool
    # None: the record does not say (album items; live lcxiM records end before index 13,
    # so the client takes it from the listing source).
    is_archived: bool | None
    description: str | None = None
    place: str | None = None


def parse_item(raw: Any) -> RawItem:
    """One record of lcxiM / EzkLib / snAcKc (parser.py L40-65, parser.ts L36-59)."""
    media_key = field(raw, 0)
    if not isinstance(media_key, str) or not media_key:
        raise _bad("media item without a media key")
    video = ext(raw, EXT_VIDEO)
    return RawItem(
        media_key=media_key,
        thumbnail_url=_str(path(raw, 1, 0)),
        width=_int(path(raw, 1, 1)),
        height=_int(path(raw, 1, 2)),
        taken_ts=_int(field(raw, 2)),
        tz_offset=_int(field(raw, 4)),
        upload_ts=_int(field(raw, 5)),
        dedup_key=_str(field(raw, 3)),
        duration_ms=_int(field(video, 0)),
        is_video=video is not None,
        is_favorite=path(raw, EXT_FAVORITE, 0) is True,
        is_archived=_bool(field(raw, 13)),
        description=_text(path(raw, EXT_DESCRIPTION, 0)),
        place=_text(path(raw, EXT_GEO, 1, 4, 0, 1, 0, 0)),
    )


def parse_items(raw_items: Any) -> list[RawItem]:
    if not isinstance(raw_items, list):
        return []
    return [parse_item(raw) for raw in raw_items if isinstance(raw, list)]


@dataclass(frozen=True)
class RawPage:
    items: list[RawItem]
    next_token: str | None


def parse_library_page(data: Any) -> RawPage:
    """lcxiM and EzkLib: ``[items, next_page_id, …]``."""
    if not isinstance(data, list):
        raise _bad("unexpected library page shape")
    return RawPage(items=parse_items(field(data, 0)), next_token=_token(field(data, 1)))


# --- albums ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RawAlbum:
    album_key: str
    title: str | None
    item_count: int | None
    cover_thumbnail_url: str | None
    is_shared: bool
    auth_key: str | None


def parse_album_record(raw: Any) -> RawAlbum | None:
    """Album record as found in Z5xsfc pages and fDcn4b ``[0][19]`` (parser.ts L108-121)."""
    key = field(raw, 0)
    if not isinstance(key, str) or not key:
        return None
    meta = ext(raw, EXT_ALBUM)
    return RawAlbum(
        album_key=key,
        title=_str(field(meta, 1)),
        item_count=_int(field(meta, 3)),
        cover_thumbnail_url=_str(path(raw, 1, 0)),
        is_shared=field(meta, 4) is True,
        auth_key=_str(field(meta, 5)),
    )


def parse_albums_page(data: Any) -> tuple[list[RawAlbum], str | None]:
    """Z5xsfc: ``[albums, next_page_id, …]``."""
    if not isinstance(data, list):
        raise _bad("unexpected albums page shape")
    records = field(data, 0, default=[])
    albums = [album for album in (parse_album_record(r) for r in records) if album is not None]
    return albums, _token(field(data, 1))


@dataclass(frozen=True)
class RawAlbumPage:
    items: list[RawItem]
    next_token: str | None
    album_key: str | None
    title: str | None
    item_count: int | None
    auth_key: str | None


def parse_album_page(data: Any) -> RawAlbumPage:
    """snAcKc: ``[_, items, next_page_id, album_meta, …]`` (parser.py L425-453, parser.ts L197-213)."""
    if not isinstance(data, list):
        raise _bad("unexpected album page shape")
    meta = field(data, 3)
    return RawAlbumPage(
        items=parse_items(field(data, 1)),
        next_token=_token(field(data, 2)),
        album_key=_str(field(meta, 0)),
        title=_str(field(meta, 1)),
        item_count=_int(field(meta, 21)),
        auth_key=_str(field(meta, 19)),
    )


# --- batch media info (filenames) --------------------------------------------------------


@dataclass(frozen=True)
class BatchInfo:
    media_key: str
    filename: str | None
    description: str | None
    size_bytes: int | None
    taken_ts: int | None
    tz_offset: int | None
    upload_ts: int | None


def parse_batch_info(data: Any) -> dict[str, BatchInfo]:
    """EWgK9e: rows live at ``data[0][1]`` (parser.py L521-545, parser.ts L222-235)."""
    rows = path(data, 0, 1)
    out: dict[str, BatchInfo] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        key = field(row, 0)
        info = field(row, 1)
        if not isinstance(key, str) or not key:
            continue
        out[key] = BatchInfo(
            media_key=key,
            filename=_text(field(info, 3)),
            description=_text(field(info, 2)),
            size_bytes=_int(field(info, 9)),
            taken_ts=_int(field(info, 6)),
            tz_offset=_int(field(info, 7)),
            upload_ts=_int(field(info, 8)),
        )
    return out


# --- item info (fDcn4b) --------------------------------------------------------------------


@dataclass(frozen=True)
class ItemInfoExt:
    media_key: str | None
    dedup_key: str | None
    filename: str | None
    description: str | None
    taken_ts: int | None
    tz_offset: int | None
    size_bytes: int | None
    width: int | None
    height: int | None
    camera: Any
    albums: list[RawAlbum]
    source: list[str]
    lat: float | None
    lng: float | None
    location_name: str | None
    takes_up_space: bool | None
    space_used_bytes: int | None
    is_original_quality: bool | None


def parse_item_info_ext(data: Any) -> ItemInfoExt | None:
    """fDcn4b (parser.ts L237-292, parser.py L343-393). ``None`` when Google returned nothing."""
    item = field(data, 0)
    if not isinstance(item, list):
        return None
    coords = path(item, 9, 0) or path(item, 13, 0)
    space = field(item, 30)
    album_records = field(item, 19, default=[])
    albums = [a for a in (parse_album_record(r) for r in album_records) if a is not None]
    source = [name for name in (_SOURCE.get(path(item, 27, 0)), _SOURCE_OS.get(path(item, 27, 1, 2))) if name]
    return ItemInfoExt(
        media_key=_str(field(item, 0)),
        dedup_key=_str(field(item, 11)),
        filename=_text(field(item, 2)),
        description=_text(field(item, 1)),
        taken_ts=_int(field(item, 3)),
        tz_offset=_int(field(item, 4)),
        size_bytes=_int(field(item, 5)),
        width=_int(field(item, 6)),
        height=_int(field(item, 7)),
        camera=field(item, 23),
        albums=albums,
        source=source,
        lat=e7(field(coords, 0)),
        lng=e7(field(coords, 1)),
        location_name=_text(path(item, 13, 2, 0, 1, 0, 0)),
        takes_up_space=_flag(field(space, 0), 1),
        space_used_bytes=_int(field(space, 1)),
        is_original_quality=_flag(field(space, 2), 2),
    )


# --- item info (VrseUb) --------------------------------------------------------------------


@dataclass(frozen=True)
class ItemInfo:
    media_key: str | None
    dedup_key: str | None
    width: int | None
    height: int | None
    taken_ts: int | None
    tz_offset: int | None
    upload_ts: int | None
    is_archived: bool | None
    is_favorite: bool
    is_video: bool
    duration_ms: int | None
    thumbnail_url: str | None
    download_url: str | None
    download_original_url: str | None
    description: str | None
    camera: Any
    takes_up_space: bool | None
    space_used_bytes: int | None
    is_original_quality: bool | None


def _info_extensions(item: list[Any]) -> dict[str, Any]:
    """The extension dict sits at index 15 for library items and 9 for album items (parser.ts L295-300)."""
    for value in reversed(item):
        if isinstance(value, dict) and any(key in value for key in _ITEM_INFO_EXT_KEYS):
            return value
    return {}


def parse_item_info(data: Any) -> ItemInfo | None:
    """VrseUb (parser.ts L294-327, parser.py L191-219). ``None`` when Google returned nothing."""
    item = field(data, 0)
    if not isinstance(item, list):
        return None
    extensions = _info_extensions(item)
    video = extensions.get(EXT_VIDEO)
    space = path(extensions, EXT_SPACE, 0)
    return ItemInfo(
        media_key=_str(field(item, 0)),
        dedup_key=_str(field(item, 3)),
        width=_int(path(item, 1, 1)),
        height=_int(path(item, 1, 2)),
        taken_ts=_int(field(item, 2)),
        tz_offset=_int(field(item, 4)),
        upload_ts=_int(field(item, 5)),
        is_archived=_bool(field(item, 13)),
        is_favorite=path(extensions, EXT_FAVORITE, 0) is True,
        is_video=video is not None,
        duration_ms=_int(field(video, 0)),
        thumbnail_url=_str(field(data, 12)),
        download_url=_str(field(data, 1)),
        download_original_url=_str(field(data, 7)),
        description=_text(field(data, 10)),
        camera=path(item, 1, 8, 4),
        takes_up_space=_flag(field(space, 0), 1),
        space_used_bytes=_int(field(space, 1)),
        is_original_quality=_flag(field(space, 2), 2),
    )


# --- storage quota ---------------------------------------------------------------------------


def parse_storage_quota(data: Any) -> tuple[int | None, int | None, int | None]:
    """EzwWhf: ``data[6]`` = ``[used, available, _, used_by_photos, …]`` (parser.py L548-560)."""
    if not isinstance(data, list):
        raise _bad("unexpected storage quota shape")
    return _int(path(data, 6, 0)), _int(path(data, 6, 1)), _int(path(data, 6, 3))
