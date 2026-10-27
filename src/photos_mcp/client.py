"""The tools' logic: plain arguments in, compact JSON-ready dicts out.

Requests are built with the vendored gpwc payloads and sent through ``GoogleTransport``;
responses are parsed by :mod:`photos_mcp.rpc`. Nothing here formats exception text.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import IO, Any, Protocol, TypeVar

from . import __version__, accounts, rpc
from .accounts import Account
from .cache import ThumbCache
from .cursors import CursorState, CursorStore, scope_of
from .errors import PhotosError, invalid
from .images import MAX_PX, TILE_MAX, Tile, contact_sheet, to_jpeg
from .session import Snapshot
from .state import StateStore
from .transport import RpcResult
from .upload import CommitUpload, Staged, parse_created, parse_matches
from .vendor.gpwc import album_payloads, payloads, trash_payloads
from .vendor.gpwc.payloads import Payload

log = logging.getLogger(__name__)
T = TypeVar("T")

# Google's page sizes are fixed upstream; the cursor slices them to ``limit``. Z5xsfc ignores
# small sizes, and a fixed library page keeps cursor recovery (≤ MAX_RECOVERY_PAGES upstream
# pages) good for 500 items whatever the agent's limit.
ALBUMS_UPSTREAM_PAGE = 100
LIBRARY_UPSTREAM_PAGE = 100

ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")
MAX_LIMIT = 50
BATCH_INFO_CHUNK = 50
PAGE_CACHE = 16
INFO_CACHE = 4096
MAX_RECOVERY_PAGES = 5
MAX_CHAIN_PAGES = 1000
SHEET_PX = TILE_MAX
MIN_VIEW_PX = 256
# Errors confined to one item of a multi-item call; anything else aborts the call.
ITEM_CODES = frozenset({"item_unavailable", "media_session_required", "schema_changed", "upstream_error"})
# Errors about the session itself: an optional enrichment must not hide them.
SESSION_CODES = frozenset({"session_unavailable", "session_expired", "account_mismatch", "blocked", "consent_required"})
# The agent's own mistakes: not worth photos_status.last_error.
AGENT_INPUT_CODES = frozenset({"invalid_argument", "invalid_cursor"})
THUMB_WORKERS = 4
# photos_canary: at most one real Google check per CANARY_TTL; stale beyond CANARY_MAX_AGE.
CANARY_TTL = 55 * 60
# A transient failure is re-checked sooner, so one blip does not page for an hour.
CANARY_RETRY = 5 * 60
CANARY_TRANSIENT = frozenset({"upstream_error", "busy", "internal_error", "stale_token", "item_unavailable"})
CANARY_MAX_AGE = 3 * 3600
CANARY_MAX_FUTURE = 5 * 60  # a verified_at further ahead than this is not trusted
CANARY_PX = 256
# Links that open an item or album on photos.google.com (the person's own browser signs them in).
PHOTO_URL = "https://photos.google.com/photo/{}"
ALBUM_URL = "https://photos.google.com/album/{}"
ALBUM_PHOTO_URL = "https://photos.google.com/album/{}/photo/{}"
LIST_SOURCES = ("library", "archive", "album", "favourites", "recently_added", "trash")
MEDIA = ("all", "photos", "videos")
# A filtered listing (media type, date range) reads up to this many Google pages per call to find matches.
FILTER_TARGET = 100
FILTER_MAX_PAGES = 5
# Local times differ from UTC by at most 14 hours: a date window is widened by this much upstream.
DAY_MS = 86_400_000
TZ_SLACK_MS = 14 * 3_600_000
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
MAX_YEARS = 20
MAX_PER_YEAR = 20
# Album changes: at most this many items per call (Google's own limit), a title this long, and this many pages of an
# album searched for the items to remove.
ALBUM_ITEMS_MAX = 50
ALBUM_TITLE_MAX = 200
ALBUM_SCAN_PAGES = 20
# Deleting: at most this many items per call, looked up this many to a request, and this many trash pages searched for
# them afterwards (and for the items to restore).
TRASH_MAX = 50
INFO_CHUNK = 25
TRASH_SCAN_PAGES = 10
SHOW_MAX = 12
SHOW_PX = 320
DETAILS_MAX = 20


class Transport(Protocol):
    state: StateStore

    def rpc(self, payloads: Sequence[Payload]) -> list[RpcResult]: ...

    def write(self, payloads: Sequence[Payload]) -> list[RpcResult]: ...

    def upload_bytes(self, source: IO[bytes], *, size: int, name: str, sha1: str) -> str: ...

    def fetch_image(self, url: str) -> bytes: ...

    def current_snapshot(self) -> Snapshot | None: ...


@dataclass(frozen=True)
class ItemMeta:
    thumb_url: str | None
    is_video: bool
    taken_at: str | None


@dataclass(frozen=True)
class _Page:
    items: list[Any]
    next_token: str | None
    extra: Any = None


def sized_url(base: str, px: int) -> str:
    """GPTK parser.ts L30-34: ``=w{px}-h{px}-k-no`` sizes the image and drops the video overlay."""
    return re.sub(r"=[^/]*$", "", base) + f"=w{px}-h{px}-k-no"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def check_ids(ids: Any, maximum: int) -> list[str]:
    if not isinstance(ids, list) or not ids:
        raise invalid("ids must be a non-empty list")
    if len(ids) > maximum:
        raise invalid(f"at most {maximum} ids per call")
    out: list[str] = []
    for value in ids:
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            raise invalid("every id must be an item id returned by this server")
        if value not in out:
            out.append(value)
    return out


def check_media(media: Any) -> str:
    if media not in MEDIA:
        raise invalid("media must be all, photos or videos")
    return media


def check_date(value: Any, name: str) -> date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise invalid(f"{name} must be a date like 2024-03-18")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise invalid(f"{name} must be a real date like 2024-03-18") from None
    if parsed.year < 1900:
        raise invalid(f"{name} must be 1900 or later")
    return parsed


def local_date(taken_ts: Any, tz_offset: Any) -> date | None:
    """The calendar date an item was taken on: its own local date when its offset is known, else UTC's."""
    if not isinstance(taken_ts, int) or isinstance(taken_ts, bool):
        return None
    shift = tz_offset if rpc.tz_known(tz_offset) else 0
    try:
        return datetime.fromtimestamp((taken_ts + shift) / 1000, UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def day_start_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def check_limit(limit: Any) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise invalid(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def _camera(raw: Any) -> str | None:
    """fDcn4b [0][23] has no documented layout: keep its readable strings."""
    strings: list[str] = []

    def walk(value: Any, depth: int) -> None:
        if depth > 4 or len(strings) >= 6:
            return
        if isinstance(value, str) and value.strip() and len(value) <= 80:
            strings.append(value.strip())
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
        elif isinstance(value, dict):
            for child in value.values():
                walk(child, depth + 1)

    walk(raw, 0)
    return " ".join(dict.fromkeys(strings)) or None


def _album_key(album_id: Any) -> str:
    if not isinstance(album_id, str) or not ID_RE.fullmatch(album_id):
        raise invalid("album_id must be an id from photos_albums")
    return album_id


def _difference(larger: Any, smaller: Any) -> int | None:
    """How many items an album gained or lost, from its counts before and after (None when Google gave none)."""
    if isinstance(larger, int) and isinstance(smaller, int):
        return max(0, larger - smaller)
    return None


def _media_filter(media: str) -> Callable[[rpc.RawItem], bool]:
    if media == "photos":
        return lambda item: not item.is_video
    if media == "videos":
        return lambda item: item.is_video
    return lambda item: True


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != [] and v is not False}


def _item(
    key: str,
    *,
    type_: str | None,
    taken_ts: Any,
    tz_offset: Any,
    filename: str | None,
    description: str | None,
    place: str | None,
    favourite: bool | None,
    archived: bool | None,
    album_id: str | None = None,
) -> dict[str, Any]:
    """The one item shape of list, search, album and get: every key always present, null
    when unknown. ``taken_at`` is local time with its offset when ``tz_known``, else UTC (Z).
    ``url`` opens the item on photos.google.com (inside its album when listed from one)."""
    return {
        "id": key,
        "url": ALBUM_PHOTO_URL.format(album_id, key) if album_id else PHOTO_URL.format(key),
        "type": type_,
        "taken_at": rpc.iso_time(taken_ts, tz_offset),
        "tz_known": rpc.tz_known(tz_offset),
        "filename": filename,
        "description": description,
        "place": place,
        "favourite": favourite,
        "archived": archived,
    }


class PhotosService:
    def __init__(
        self,
        transport: Transport,
        cache: ThumbCache,
        cursors: CursorStore | None = None,
        *,
        clock: Callable[[], float] = time.time,
        account: Account | None = None,
        sign_in_links: bool = False,
    ) -> None:
        # The account this service acts for (None: a single-account server built without the registry). Everything
        # below (cursors, page and item caches, the thumbnail cache, budgets, the canary) is this account's alone.
        self.account = account
        self.sign_in_links = sign_in_links
        self._t = transport
        self._cache = cache
        self._cursors = cursors or CursorStore()
        self._clock = clock
        self._lock = threading.Lock()
        self._pages: OrderedDict[tuple[str, str | None], _Page] = OrderedDict()
        self._meta: OrderedDict[str, ItemMeta] = OrderedDict()
        self._names: OrderedDict[str, rpc.BatchInfo] = OrderedDict()
        self._album_auth: dict[str, str | None] = {}
        self._extras: dict[str, Any] = {}  # per scope: the latest page's extra (album title/count)
        self._album_shared: dict[str, bool] = {}
        self._canary_lock = threading.Lock()
        # (checked_at, generation checked, ok, code)
        self._canary: tuple[float, int | None, bool, str | None] | None = None

    @property
    def state(self) -> StateStore:
        return self._t.state

    # -- tools --------------------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 20,
        cursor: str | None = None,
        media: str = "all",
        details: bool = False,
    ) -> dict[str, Any]:
        limit = check_limit(limit)
        media = check_media(media)
        self._check_details(details, limit)
        query = (query or "").strip() if isinstance(query, str) else ""
        if not query or len(query) > 200:
            raise invalid("query must be 1 to 200 characters")

        def fetch_raw(token: str | None) -> _Page:
            data = self._one(payloads.GetSearchPage(query, page_id=token), required=True)
            page = rpc.parse_library_page(data)
            return _Page(page.items, page.next_token)

        fetch = self._filtered(fetch_raw, _media_filter(media)) if media != "all" else fetch_raw
        scope = scope_of("search", f"{query.casefold()}\x00{media}")
        items, next_cursor, coverage = self._paged(scope, cursor, limit, fetch)
        coverage["archive_included"] = True  # Google's search finds archived items too
        return self._with_details(self._listing(items, next_cursor, coverage), details)

    def list(
        self,
        source: str = "library",
        album_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        media: str = "all",
        details: bool = False,
    ) -> dict[str, Any]:
        limit = check_limit(limit)
        media = check_media(media)
        self._check_details(details, limit)
        if source not in LIST_SOURCES:
            raise invalid("source must be library, archive, album, favourites, recently_added or trash")
        if (source == "album") != (album_id is not None):
            raise invalid("album_id is required for source=album and only then")
        dated = from_date is not None or to_date is not None
        if dated and source not in ("library", "archive"):
            raise invalid("from_date and to_date work with source=library or archive")
        first = check_date(from_date, "from_date") if from_date is not None else None
        last = check_date(to_date, "to_date") if to_date is not None else None
        if first and last and first > last:
            raise invalid("from_date must not be after to_date")
        keep = _media_filter(media)
        if source == "album":
            if not isinstance(album_id, str) or not ID_RE.fullmatch(album_id):
                raise invalid("album_id must be an id from photos_albums")
            key = album_id

            def fetch_album(token: str | None) -> _Page:
                payload = payloads.GetAlbumPage(key, page_id=token, auth_key=self._album_auth.get(key))
                data = self._one(payload, required=False)
                if data is None:
                    raise PhotosError("item_unavailable", log_detail="album page missing")
                page = rpc.parse_album_page(data)
                if page.auth_key:
                    self._album_auth.setdefault(key, page.auth_key)
                return _Page(page.items, page.next_token, {"id": key, "title": page.title, "count": page.item_count})

            fetch = self._filtered(fetch_album, keep) if media != "all" else fetch_album
            items, next_cursor, coverage = self._paged(scope_of("album", f"{key}\x00{media}"), cursor, limit, fetch)
            coverage["archive_included"] = True
            result = self._listing(items, next_cursor, coverage, album_id=key)
            album = self._last_extra(scope_of("album", f"{key}\x00{media}"))
            if album:
                result["album"] = _compact({**album, "url": ALBUM_URL.format(key)})
            return self._with_details(result, details)

        if source == "trash":

            def fetch_trash(token: str | None) -> _Page:
                page = rpc.parse_library_page(self._one(payloads.GetTrashPage(page_id=token), required=True))
                return _Page(page.items, page.next_token)

            fetch = self._filtered(fetch_trash, keep) if media != "all" else fetch_trash
            items, next_cursor, coverage = self._paged(scope_of(source, media), cursor, limit, fetch)
            coverage["archive_included"] = True
            return self._with_details(self._listing(items, next_cursor, coverage), details)

        if source in ("favourites", "recently_added"):
            builder = payloads.GetFavoritesPage if source == "favourites" else payloads.GetLibraryPageByUploadedDate

            def fetch_filtered_view(token: str | None) -> _Page:
                page = rpc.parse_library_page(self._one(builder(page_id=token), required=True))
                return _Page(page.items, page.next_token)

            fetch = self._filtered(fetch_filtered_view, keep) if media != "all" else fetch_filtered_view
            items, next_cursor, coverage = self._paged(scope_of(source, media), cursor, limit, fetch)
            coverage["archive_included"] = True  # these views list archived items too
            return self._with_details(self._listing(items, next_cursor, coverage), details)

        lcxim_source = "archive" if source == "archive" else "library"
        # A date range starts Google's newest-first timeline at the end of the range (widened by the largest
        # time-zone offset) and stops once it passes the range's start; each item's own local date decides.
        anchor = day_start_ms(last + timedelta(days=1)) + TZ_SLACK_MS if last else None
        floor = day_start_ms(first) - TZ_SLACK_MS if first else None

        def fetch_raw(token: str | None) -> _Page:
            payload = payloads.GetLibraryPageByTakenDate(
                timestamp=anchor if token is None else None,
                page_id=token,
                source=lcxim_source,
                page_size=LIBRARY_UPSTREAM_PAGE,
            )
            page = rpc.parse_library_page(self._one(payload, required=True))
            return _Page(page.items, page.next_token)

        def in_range(item: rpc.RawItem) -> bool:
            day = local_date(item.taken_ts, item.tz_offset)
            if day is None:
                return False
            return (first is None or day >= first) and (last is None or day <= last)

        def past(item: rpc.RawItem) -> bool:
            return floor is not None and isinstance(item.taken_ts, int) and item.taken_ts < floor

        if dated or media != "all":
            fetch = self._filtered(fetch_raw, lambda i: keep(i) and (not dated or in_range(i)), stop=past)
        else:
            fetch = fetch_raw
        scope = scope_of(source, f"{from_date}\x00{to_date}\x00{media}")
        items, next_cursor, coverage = self._paged(scope, cursor, limit, fetch)
        coverage["archive_included"] = source == "archive"
        # Recorded live: lcxiM library records end before the archived flag (index 13), but the
        # source itself says it: library pages hold only unarchived items, archive pages only archived.
        result = self._listing(items, next_cursor, coverage, archived_default=source == "archive")
        return self._with_details(result, details)

    def on_this_day(self, day: str | None = None, years: int = 10, per_year: int = 6) -> dict[str, Any]:
        """Items taken on this calendar day in each of the previous ``years`` years, newest year first: one
        library page per year (up to 100 items of that day), and one filename lookup for all of them."""
        if not isinstance(years, int) or isinstance(years, bool) or not 1 <= years <= MAX_YEARS:
            raise invalid(f"years must be between 1 and {MAX_YEARS}")
        if not isinstance(per_year, int) or isinstance(per_year, bool) or not 1 <= per_year <= MAX_PER_YEAR:
            raise invalid(f"per_year must be between 1 and {MAX_PER_YEAR}")
        today = (
            check_date(day, "date")
            if day is not None
            else datetime.fromtimestamp(self._clock(), self.state.budget_tz).date()
        )
        found: list[tuple[int, list[rpc.RawItem], bool]] = []
        checked = 0
        for back in range(1, years + 1):
            year = today.year - back
            if year < 1900:
                break
            try:
                target = today.replace(year=year)
            except ValueError:  # 29 February in a year without one
                target = today.replace(year=year, day=28)
            checked += 1
            payload = payloads.GetLibraryPageByTakenDate(
                timestamp=day_start_ms(target + timedelta(days=1)) + TZ_SLACK_MS,
                source="library",
                page_size=LIBRARY_UPSTREAM_PAGE,
            )
            page = rpc.parse_library_page(self._one(payload, required=True))
            if not page.items:
                break  # nothing this old: the library starts later
            same_day = [r for r in page.items if local_date(r.taken_ts, r.tz_offset) == target]
            oldest = local_date(page.items[-1].taken_ts, page.items[-1].tz_offset)
            more = len(same_day) > per_year or (oldest is not None and oldest >= target and page.next_token)
            if same_day:
                found.append((year, same_day[:per_year], bool(more)))
        chosen = [r for _, items, _ in found for r in items]
        self._remember(chosen)
        names = self._filenames([r.media_key for r in chosen])
        return {
            "date": today.strftime("%m-%d"),
            "years": [
                {"year": year, "items": [self._raw_item(r, names, archived_default=False) for r in items], "more": more}
                for year, items, more in found
            ],
            "years_checked": checked,
            "freshness": self._freshness(),
        }

    def show(self, picks: list[dict[str, Any]], title: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """Photo cards: what the person sees (ids, dates, links and notes for the model too) and, apart, the
        thumbnails the card draws (``SHOW_PX``, from the same paced and cached image path as photos_view)."""
        if not isinstance(picks, list) or not 1 <= len(picks) <= SHOW_MAX:
            raise invalid(f"items must hold 1 to {SHOW_MAX} photos")
        keys = check_ids([p.get("id") if isinstance(p, dict) else None for p in picks], SHOW_MAX)
        if len(keys) != len(picks):
            raise invalid("every id may appear once")
        notes = {p["id"]: (p.get("note") or None) for p in picks}
        shown: list[dict[str, Any]] = []
        thumbs: dict[str, str] = {}
        for number, (key, result) in enumerate(zip(keys, self._thumbnails(keys, SHOW_PX), strict=True), start=1):
            if isinstance(result, PhotosError):
                shown.append({"n": number, "id": key, "url": PHOTO_URL.format(key), "error": result.payload()})
                continue
            jpeg, meta, _ = result
            thumbs[key] = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
            shown.append(
                _compact(
                    {
                        "n": number,
                        "id": key,
                        "url": PHOTO_URL.format(key),
                        "taken_at": meta.get("taken_at"),
                        "type": "video" if meta.get("video") else "photo",
                        "note": notes.get(key),
                    }
                )
            )
        result = _compact({"kind": "photos", "title": title, "items": shown, "freshness": self._freshness()})
        return result, {"thumbs": thumbs}

    def albums(self, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        # Z5xsfc does not honour small page sizes (live: page_size 5 returned one of five
        # albums). Always fetch Google's page of 100; the cursor slices it to ``limit``.
        limit = check_limit(limit)

        def fetch(token: str | None) -> _Page:
            albums, next_token = rpc.parse_albums_page(
                self._one(payloads.GetAlbumsPage(page_id=token, page_size=ALBUMS_UPSTREAM_PAGE), required=True)
            )
            with self._lock:
                for album in albums:
                    self._album_auth[album.album_key] = album.auth_key
                    self._album_shared[album.album_key] = album.is_shared
            return _Page(albums, next_token)

        albums, next_cursor, coverage = self._paged(scope_of("albums"), cursor, limit, fetch)
        coverage.pop("shared_partner", None)
        return {
            "albums": [
                _compact(
                    {
                        "id": a.album_key,
                        "url": ALBUM_URL.format(a.album_key),
                        "title": a.title,
                        "count": a.item_count,
                        "shared": a.is_shared,
                    }
                )
                for a in albums
            ],
            "next_cursor": next_cursor,
            "coverage": coverage,
            "freshness": self._freshness(),
        }

    def get(self, ids: list[str]) -> dict[str, Any]:
        keys = check_ids(ids, 10)
        requests: list[Payload] = []
        for key in keys:
            requests += [payloads.GetItemInfoExt(key), payloads.GetItemInfo(key)]
        try:
            data = self._rpc(*requests)  # every id in one batchexecute
        except PhotosError as exc:
            if exc.code not in ITEM_CODES:
                raise
            self._log_item(exc)
            return {"items": [{"id": key, "error": exc.payload()} for key in keys], "freshness": self._freshness()}
        out = []
        for index, key in enumerate(keys):
            try:
                out.append(self._info(key, data[2 * index], data[2 * index + 1]))
            except PhotosError as exc:
                if exc.code not in ITEM_CODES:
                    raise
                self._log_item(exc)
                out.append({"id": key, "error": exc.payload()})
        return {"items": out, "freshness": self._freshness()}

    # -- album changes -------------------------------------------------------------------------------------------

    def album_create(self, title: str, ids: list[str] | None = None) -> dict[str, Any]:
        """A new album, with up to 50 items in it. Never repeated on its own: a failure after sending is
        outcome_unknown (photos_albums tells whether it exists)."""
        name = title.strip() if isinstance(title, str) else ""
        if not name or len(name) > ALBUM_TITLE_MAX:
            raise invalid(f"title must be 1 to {ALBUM_TITLE_MAX} characters")
        keys = check_ids(ids, ALBUM_ITEMS_MAX) if ids else []
        self.state.take("write", 1 + len(keys))
        (created,) = self._write([album_payloads.CreateAlbum(name)], action="create", album=None, items=0)
        key = rpc.path(created, 0, 0)
        if not isinstance(key, str) or not ID_RE.fullmatch(key):
            raise PhotosError("refused", log_detail="OXvT9d returned no album key")
        with self._lock:
            self._album_shared[key] = False
        self.state.audit({"action": "created", "album": key})
        added = 0
        if keys:
            self._write([album_payloads.AddItemsToExistingAlbum(keys, key)], action="add", album=key, items=len(keys))
        after = self._album_state(key)
        if keys and after.get("count") is not None:
            added = after["count"]
        return {"album": after, "created": True, "requested": len(keys), "added": added, "freshness": self._freshness()}

    def album_add(self, album_id: str, ids: list[str]) -> dict[str, Any]:
        """Add up to 50 items (ids from search or list) to an album; Google keeps one copy of an item already in it.
        The outcome is read back from the album."""
        key = _album_key(album_id)
        keys = check_ids(ids, ALBUM_ITEMS_MAX)
        before = self._album_state(key)
        self.state.take("write", len(keys))
        payload = (
            album_payloads.AddItemsToExistingSharedAlbum(keys, key)
            if before["shared"]
            else album_payloads.AddItemsToExistingAlbum(keys, key)
        )
        self._write([payload], action="add", album=key, items=len(keys))
        after = self._album_state(key)
        return {
            "album": after,
            "requested": len(keys),
            "added": _difference(after.get("count"), before.get("count")),
            "freshness": self._freshness(),
        }

    def album_remove(self, album_id: str, ids: list[str]) -> dict[str, Any]:
        """Remove up to 50 items from an album (they stay in the library). Only the ids its own listing holds are
        removed (Google's remove RPC names no album, so a key from another album would act there); the outcome is
        read back from the album. The client asks the person before this runs (it is on the registry's ask list)."""
        key = _album_key(album_id)
        keys = check_ids(ids, ALBUM_ITEMS_MAX)
        before = self._album_state(key)
        found = list(self._find_in_album(key, keys))
        if not found:
            raise invalid("none of these ids is in this album: use the ids photos_list(source=album) gives")
        self.state.take("write", len(found))
        payload = (
            album_payloads.RemoveItemsFromSharedAlbum(key, found)
            if before["shared"]
            else album_payloads.RemoveItemsFromAlbum(found)
        )
        self._write([payload], action="remove", album=key, items=len(found))
        after = self._album_state(key)
        return {
            "album": after,
            "requested": len(keys),
            "removed": _difference(before.get("count"), after.get("count")),
            "not_in_album": [k for k in keys if k not in found],
            "freshness": self._freshness(),
        }

    def album_delete(self, album_id: str) -> dict[str, Any]:
        """Delete one of this account's own albums (its photos stay in the library), read back: the album no longer
        opens. A shared album is left to the app (deleting it changes it for everyone in it)."""
        key = _album_key(album_id)
        state = self._album_state(key)
        if state["shared"]:
            raise invalid(
                "this is a shared album: delete or leave it in the Google Photos app (it changes the album for "
                "everyone in it)"
            )
        self.state.take("write", 1)
        self._write([album_payloads.DeleteAlbum([key])], action="delete_album", album=key, items=0)
        gone = self._one(payloads.GetAlbumPage(key, auth_key=self._album_auth.get(key)), required=False) is None
        with self._lock:
            self._album_shared.pop(key, None)
            self._album_auth.pop(key, None)
        album = {"id": key, "title": state.get("title"), "count": state.get("count")}
        return {"album": album, "deleted": gone, "freshness": self._freshness()}

    # -- the trash -----------------------------------------------------------------------------------------------

    def trash(self, ids: list[str]) -> dict[str, Any]:
        """Move up to 50 items (ids from search or list) to the trash, where Google keeps them 60 days; read back from
        the trash. Ids that are not items of this library are left alone (not_found)."""
        keys = check_ids(ids, TRASH_MAX)
        found = self._dedup_keys(keys)
        if not found:
            raise invalid("none of these ids is an item of this library: use ids from photos_search or photos_list")
        dedups = {k: dedup for k, (_, dedup) in found.items()}
        self.state.take("write", len(dedups))
        self._write([trash_payloads.MoveToTrash(list(dedups.values()))], action="trash", album=None, items=len(dedups))
        in_trash = self._find_in_trash(list(dedups))
        return {
            "requested": len(keys),
            "trashed": len(in_trash),
            "not_found": [k for k in keys if k not in found],
            "items": [{"id": k, "url": PHOTO_URL.format(k), "in_trash": k in in_trash} for k in dedups],
            "freshness": self._freshness(),
        }

    def restore(self, ids: list[str]) -> dict[str, Any]:
        """Bring up to 50 items back from the trash (ids as photos_list(source=trash) gives them)."""
        keys = check_ids(ids, TRASH_MAX)
        found = self._find_in_trash(keys)
        dedups = {k: r.dedup_key for k, r in found.items() if r.dedup_key}
        if not dedups:
            raise invalid("none of these ids is in the trash: photos_list(source=trash) lists it")
        self.state.take("write", len(dedups))
        payload = trash_payloads.RestoreFromTrash(list(dedups.values()))
        self._write([payload], action="restore", album=None, items=len(dedups))
        still = self._find_in_trash(list(dedups))
        return {
            "requested": len(keys),
            "restored": len(dedups) - len(still),
            "not_in_trash": [k for k in keys if k not in dedups],
            "items": [{"id": k, "url": PHOTO_URL.format(k), "restored": k not in still} for k in dedups],
            "freshness": self._freshness(),
        }

    def _dedup_keys(self, keys: list[str]) -> dict[str, tuple[rpc.ItemInfo, str]]:
        """The items of this library among ``keys``, with the dedup keys the trash RPC takes (VrseUb, INFO_CHUNK to a
        request), in the order asked."""
        found: dict[str, tuple[rpc.ItemInfo, str]] = {}
        for start in range(0, len(keys), INFO_CHUNK):
            chunk = keys[start : start + INFO_CHUNK]
            data = self._rpc(*[payloads.GetItemInfo(k) for k in chunk])
            for k, raw in zip(chunk, data, strict=True):
                info = rpc.parse_item_info(raw) if raw is not None else None
                if info is not None and info.dedup_key and info.media_key in (None, k):
                    found[k] = (info, info.dedup_key)
        return {k: found[k] for k in keys if k in found}

    def _find_in_trash(self, keys: list[str]) -> dict[str, rpc.RawItem]:
        """The ``keys`` the trash holds (TRASH_SCAN_PAGES pages at most, most recently deleted first)."""
        remaining = set(keys)
        found: dict[str, rpc.RawItem] = {}
        token: str | None = None
        for _ in range(TRASH_SCAN_PAGES):
            page = rpc.parse_library_page(self._one(payloads.GetTrashPage(page_id=token), required=True))
            for item in page.items:
                if item.media_key in remaining:
                    found[item.media_key] = item
                    remaining.discard(item.media_key)
            token = page.next_token
            if not remaining or not token:
                break
        return {k: found[k] for k in keys if k in found}

    # -- uploads -------------------------------------------------------------------------------------------------

    def album(self, album_id: str) -> dict[str, Any]:
        """One album as Google shows it now: {id, url, title, count, shared}."""
        return self._album_state(_album_key(album_id))

    def upload(self, staged: Staged, album_id: str | None = None, *, check_album: bool = True) -> dict[str, Any]:
        """Add one photo or video (``staged``, see ``upload.Staging``) to the library, and to an album when one is
        given, as the web app's uploader does: a file the library already holds (the same bytes) is not sent again.

        With ``check_album`` the album is read first (a wrong one fails before anything is sent) and again afterwards
        (``added``); an upload link checked its album when it was made and skips both reads for each file."""
        key = _album_key(album_id) if album_id is not None else None
        before = self._album_state(key) if key and check_album else None
        self.state.take("write", 2 if key else 1)
        matches = parse_matches(self._one(payloads.GetRemoteMatchesByHash([staged.sha1]), required=False))
        existing = matches[0] if matches else None
        if existing is None:
            token = self._t.upload_bytes(staged.file, size=staged.size, name=staged.name, sha1=staged.sha1)
            (created,) = self._write(
                [CommitUpload(token, staged.name, staged.modified_ms)], action="upload", album=None, items=1
            )
            stored = parse_created(created)
            name: str | None = staged.name
        else:
            stored = existing
            name = self._filenames([stored.media_key]).get(stored.media_key)
        album = self._add_uploaded(key, stored.media_key, before) if key else None
        return {
            "item": {
                "id": stored.media_key,
                "url": PHOTO_URL.format(stored.media_key),
                "type": staged.media.type,
                "filename": name,
                "taken_at": rpc.iso_time(stored.taken_ts, stored.tz_offset),
                "tz_known": rpc.tz_known(stored.tz_offset),
                "width": stored.width,
                "height": stored.height,
                "uploaded_at": rpc.utc_time(stored.uploaded_ts),
            },
            "already_in_library": existing is not None,
            "size_bytes": staged.size,
            "album": album,
            "freshness": self._freshness(),
        }

    def _add_uploaded(self, key: str, media_key: str, before: dict[str, Any] | None) -> dict[str, Any]:
        """Put a just-uploaded item in its album. The item exists whatever happens here, so a failure is the album's
        own error in the result, never the upload's."""
        try:
            shared = before["shared"] if before is not None else self._is_shared(key)
            payload = (
                album_payloads.AddItemsToExistingSharedAlbum([media_key], key)
                if shared
                else album_payloads.AddItemsToExistingAlbum([media_key], key)
            )
            (data,) = self._write([payload], action="add", album=key, items=1)
            if data is None:
                raise PhotosError("refused", log_detail=f"{payload.rpcid} returned no data")
            if before is None:
                return {"id": key, "url": ALBUM_URL.format(key), "added": None}
            after = self._album_state(key)
            return {**after, "added": _difference(after.get("count"), before.get("count"))}
        except PhotosError as exc:
            log.info("upload: the item is in the library, the album step failed (%s)", exc.code)
            return {"id": key, "url": ALBUM_URL.format(key), "added": 0, "error": exc.payload()}

    def _write(self, requests: list[Payload], *, action: str, album: str | None, items: int) -> list[Any]:
        """Send changes; every outcome goes to the audit log. Cached pages are dropped (they no longer hold)."""
        try:
            results = self._t.write(requests)
        except PhotosError as exc:
            self.state.audit({"action": action, "album": album, "items": items, "outcome": exc.code})
            if exc.code == "outcome_unknown":
                self._forget_pages()
            raise
        by_id = {r.payload_id: r for r in results}
        data = [by_id[p.payload_id].data if p.payload_id in by_id else None for p in requests]
        self._forget_pages()
        if any(d is None for d in data) and action in ("create", "upload", "trash", "restore", "delete_album"):
            self.state.audit({"action": action, "album": album, "items": items, "outcome": "refused"})
            raise PhotosError("refused", log_detail=f"{requests[0].rpcid} returned no data")
        self.state.audit({"action": action, "album": album, "items": items, "outcome": "sent"})
        return data

    def _forget_pages(self) -> None:
        with self._lock:
            self._pages.clear()
            self._extras.clear()

    def _album_state(self, key: str) -> dict[str, Any]:
        """The album as Google shows it now (its first page, never from the cache)."""
        data = self._one(payloads.GetAlbumPage(key, auth_key=self._album_auth.get(key)), required=False)
        if data is None:
            raise PhotosError("item_unavailable", log_detail="album page missing")
        page = rpc.parse_album_page(data)
        if page.auth_key:
            with self._lock:
                self._album_auth.setdefault(key, page.auth_key)
        shared = self._is_shared(key)
        return {
            "id": key,
            "url": ALBUM_URL.format(key),
            "title": page.title,
            "count": page.item_count,
            "shared": shared,
        }

    def _is_shared(self, key: str) -> bool:
        """Whether an album is shared, as the albums list says (an album page does not tell: live, private albums
        have members and an auth key too). Looked up once per album, then kept; an album not found in the list is
        this account's own."""
        with self._lock:
            known = self._album_shared.get(key)
        if known is not None:
            return known
        token: str | None = None
        for _ in range(ALBUM_SCAN_PAGES):
            albums, token = rpc.parse_albums_page(
                self._one(payloads.GetAlbumsPage(page_id=token, page_size=ALBUMS_UPSTREAM_PAGE), required=True)
            )
            with self._lock:
                for album in albums:
                    self._album_shared[album.album_key] = album.is_shared
                    self._album_auth.setdefault(album.album_key, album.auth_key)
                if key in self._album_shared:
                    return self._album_shared[key]
            if not token:
                break
        with self._lock:
            self._album_shared[key] = False
        return False

    def _find_in_album(self, key: str, wanted: list[str]) -> dict[str, rpc.RawItem]:
        """The wanted ids that the album's own listing holds, in the order asked (ALBUM_SCAN_PAGES pages at most)."""
        remaining = set(wanted)
        found: dict[str, rpc.RawItem] = {}
        token: str | None = None
        for _ in range(ALBUM_SCAN_PAGES):
            payload = payloads.GetAlbumPage(key, page_id=token, auth_key=self._album_auth.get(key))
            data = self._one(payload, required=False)
            if data is None:
                break
            page = rpc.parse_album_page(data)
            for item in page.items:
                if item.media_key in remaining:
                    found[item.media_key] = item
                    remaining.discard(item.media_key)
            token = page.next_token
            if not remaining or not token:
                break
        return {k: found[k] for k in wanted if k in found}

    def view(self, ids: list[str], max_px: int = 1024) -> list[tuple[dict[str, Any], bytes | None]]:
        keys = check_ids(ids, 4)
        if not isinstance(max_px, int) or isinstance(max_px, bool) or not MIN_VIEW_PX <= max_px <= MAX_PX:
            raise invalid(f"max_px must be between {MIN_VIEW_PX} and {MAX_PX}")
        out: list[tuple[dict[str, Any], bytes | None]] = []
        for number, (key, result) in enumerate(zip(keys, self._thumbnails(keys, max_px), strict=True), start=1):
            if isinstance(result, PhotosError):
                out.append(({"n": number, "id": key, "error": result.payload()}, None))
                continue
            jpeg, meta, cached = result
            label = _compact(
                {
                    "n": number,
                    "id": key,
                    "taken_at": meta.get("taken_at"),
                    "type": "video" if meta.get("video") else "photo",
                    "width": meta.get("w"),
                    "height": meta.get("h"),
                    "video_poster": bool(meta.get("video")),
                    "from_cache": cached,
                }
            )
            out.append((label, jpeg))
        return out

    def contact_sheet(self, ids: list[str]) -> tuple[dict[str, Any], bytes]:
        keys = check_ids(ids, 24)
        tiles: list[Tile] = []
        legend: list[dict[str, Any]] = []
        for number, (key, result) in enumerate(zip(keys, self._thumbnails(keys, SHEET_PX), strict=True), start=1):
            if isinstance(result, PhotosError):
                tiles.append(Tile(number, None))
                legend.append({"n": number, "id": key, "error": result.code})
                continue
            jpeg, meta, _ = result
            tiles.append(Tile(number, jpeg, bool(meta.get("video"))))
            taken = meta.get("taken_at")
            legend.append(
                _compact({"n": number, "id": key, "date": taken[:10] if taken else None, "video": meta.get("video")})
            )
        sheet, width, height = contact_sheet(tiles)
        return {"legend": legend, "width": width, "height": height, "freshness": self._freshness()}, sheet

    def status(self) -> dict[str, Any]:
        snapshot = self._t.current_snapshot()
        state = self.state.describe()
        linked = self.account is not None and self.account.linked
        session: dict[str, Any]
        if linked and (snapshot is None or getattr(self._t, "expected_email", None) is None):
            session = {"state": "not_signed_in"}
        elif snapshot is None:
            session = {"state": "session_unavailable"}
        else:
            session = snapshot.describe(self._clock())
            expected = getattr(self._t, "expected_email", None)
            dead = self.state.dead_code(snapshot.generation)
            if expected and snapshot.email != expected:
                session["state"] = "account_mismatch"
            elif dead:
                session["state"] = dead
            else:
                session["state"] = "ok"
        if state["breaker"]["open"]:
            session["state"] = "blocked" if session["state"] == "ok" else session["state"]
        out = {
            "service": "photos-mcp",
            "version": __version__,
            # The only changes this server makes: albums (create, add, remove, delete), uploads and the trash (move
            # there and back). Nothing empties the trash or edits media.
            "changes": ["albums", "uploads", "trash"],
            "session": session,
            "breaker": state["breaker"],
            "budgets": state["budgets"],
            "last_error": state["last_error"],
            "cache": {"bytes": self._cache.size(), "max_bytes": self._cache.max_bytes},
        }
        if self.account is not None:
            out["account_id"] = self.account.id
            # The same owner entry point covers native-default and further-account profiles.
            out["sign_in_links"] = self.sign_in_links
            if self.sign_in_links:
                out["keeper"] = accounts.keeper_status(self.account)
        return out

    def record_error(self, code: str) -> None:
        """Remember a real failure for photos_status; the agent's own input mistakes are not."""
        if code not in AGENT_INPUT_CODES:
            self.state.record_error(code)

    def canary(self) -> dict[str, Any]:
        """One real check of the path agents use (a library page of 1 item and its thumbnail).
        The verdict is cached CANARY_TTL, or CANARY_RETRY after a transient failure. Freshness
        of the snapshot is judged on every call (``snapshot_stale``). Never raises; never logs
        ids."""
        with self._canary_lock:
            now = self._clock()
            snapshot = self._t.current_snapshot()
            current = snapshot.generation if snapshot is not None else None
            cached = False
            if self._canary is not None:
                last_at, last_generation, _, last_code = self._canary
                ttl = CANARY_RETRY if last_code in CANARY_TRANSIENT else CANARY_TTL
                # A verdict belongs to the generation it checked: a new one is checked anew.
                cached = now - last_at < ttl and last_generation == current
            if not cached:
                ok, code = self._canary_check()
                self._canary = (now, current, ok, code)
            assert self._canary is not None
            checked_at, _, ok, code = self._canary
        age = generation = None
        if snapshot is not None:
            generation = snapshot.generation
            age = int(self._clock() - snapshot.verified_at)  # negative: verified_at lies ahead
        if ok:
            if age is None:
                ok, code = False, "session_unavailable"
            elif age > CANARY_MAX_AGE or age < -CANARY_MAX_FUTURE:
                ok, code = False, "snapshot_stale"
        return {
            "ok": ok,
            "code": code,
            "checked_at": datetime.fromtimestamp(checked_at, UTC).isoformat(timespec="seconds"),
            "cached": cached,
            "snapshot_age_s": age,
            "generation": generation,
        }

    def _canary_check(self) -> tuple[bool, str | None]:
        try:
            # An upstream page of 1 (README "Tools", photos_canary), through the same RPC, parser and thumbnail path.
            payload = payloads.GetLibraryPageByTakenDate(source="library", page_size=1)
            items = rpc.parse_library_page(self._one(payload, required=True)).items[:1]
            if not items:
                raise PhotosError("item_unavailable", log_detail="canary: the library page was empty")
            self._remember(items)
            self._thumbnail(items[0].media_key, CANARY_PX, use_cache=False)
        except PhotosError as exc:
            log.warning("canary failed: %s", exc.code)
            self.record_error(exc.code)
            return False, exc.code
        except Exception as exc:  # the canary reports; it never raises
            log.error("canary crashed: %s", type(exc).__name__)
            self.record_error("internal_error")
            return False, "internal_error"
        log.info("canary ok")
        return True, None

    # -- internals: RPCs ----------------------------------------------------------------

    def _rpc(self, *requests: Payload) -> list[Any]:
        """One batchexecute POST; data per payload in order, None where that RPC failed."""
        results = self._t.rpc(list(requests))
        by_id = {r.payload_id: r for r in results}
        return [by_id[p.payload_id].data if p.payload_id in by_id else None for p in requests]

    def _one(self, payload: Payload, *, required: bool) -> Any:
        (data,) = self._rpc(payload)
        if data is None and required:
            raise PhotosError("schema_changed", log_detail=f"{payload.rpcid} returned no data")
        return data

    # -- internals: paging --------------------------------------------------------------

    def _page(self, scope: str, token: str | None, fetch: Callable[[str | None], _Page], use_cache: bool) -> _Page:
        key = (scope, token)
        if use_cache:
            with self._lock:
                cached = self._pages.get(key)
            if cached is not None:
                return cached
        page = fetch(token)
        with self._lock:
            self._pages[key] = page
            self._pages.move_to_end(key)
            while len(self._pages) > PAGE_CACHE:
                self._pages.popitem(last=False)
            self._extras[scope] = page.extra
        return page

    def _last_extra(self, scope: str) -> Any:
        with self._lock:
            return self._extras.get(scope)

    def _paged(
        self, scope: str, cursor: str | None, limit: int, fetch: Callable[[str | None], _Page]
    ) -> tuple[list[Any], str | None, dict[str, Any]]:
        recovered = False
        skip = 0
        if cursor is None:
            state = CursorState(scope, None, 0, 0)
        else:
            if not isinstance(cursor, str):
                raise PhotosError("invalid_cursor")
            found, delivered = self._cursors.resolve(cursor, scope)
            if found is None:
                recovered, skip = True, delivered
                state = CursorState(scope, None, 0, delivered)
            else:
                state = found
        token, offset, seen = state.server_token, state.offset, list(state.seen)
        walked = 0
        loop = False
        while True:
            page = self._page(scope, token, fetch, use_cache=offset > 0)
            available = len(page.items) - offset
            if skip and skip >= available and page.next_token:
                skip -= max(available, 0)
                walked += 1
                if walked > MAX_RECOVERY_PAGES:
                    raise PhotosError("cursor_expired", log_detail="recovery would walk too many pages")
                if page.next_token == token or page.next_token in seen:
                    loop = True
                    break
                seen.append(token or "")
                token, offset = page.next_token, 0
                continue
            offset = min(len(page.items), offset + skip)
            skip = 0
            break
        chosen = [] if loop else page.items[offset : offset + limit]
        end = offset + len(chosen)
        next_state: tuple[str | None, int] | None = None
        if not loop:
            if end < len(page.items):
                next_state = (token, end)
            elif page.next_token:
                if page.next_token == token or page.next_token in seen or len(seen) >= MAX_CHAIN_PAGES:
                    loop = True
                else:
                    next_state = (page.next_token, 0)
        next_cursor = None
        if next_state is not None:
            chain = seen + [token or ""] if next_state[0] != token else seen
            next_cursor = self._cursors.issue(
                CursorState(
                    scope, next_state[0], next_state[1], state.delivered + len(chosen), tuple(chain[-MAX_CHAIN_PAGES:])
                )
            )
        coverage: dict[str, Any] = {
            "pagination_complete": next_cursor is None and not loop,
            "shared_partner": "not_covered",
        }
        if recovered:
            coverage["cursor_recovered"] = True
            coverage["dedupe_required"] = True
        if loop:
            coverage["loop_detected"] = True
        return chosen, next_cursor, coverage

    def _listing(
        self,
        raw_items: list[rpc.RawItem],
        next_cursor: str | None,
        coverage: dict[str, Any],
        *,
        archived_default: bool | None = None,
        album_id: str | None = None,
    ) -> dict[str, Any]:
        self._remember(raw_items)
        names = self._filenames([r.media_key for r in raw_items])
        items = [self._raw_item(r, names, archived_default=archived_default, album_id=album_id) for r in raw_items]
        return {"items": items, "next_cursor": next_cursor, "coverage": coverage, "freshness": self._freshness()}

    @staticmethod
    def _raw_item(
        r: rpc.RawItem, names: dict[str, str], *, archived_default: bool | None, album_id: str | None = None
    ) -> dict[str, Any]:
        return _item(
            r.media_key,
            type_="video" if r.is_video else "photo",
            taken_ts=r.taken_ts,
            tz_offset=r.tz_offset,
            filename=names.get(r.media_key),
            description=r.description,
            place=r.place,
            favourite=r.is_favorite,
            archived=r.is_archived if r.is_archived is not None else archived_default,
            album_id=album_id,
        )

    def _filtered(
        self,
        fetch_raw: Callable[[str | None], _Page],
        keep: Callable[[rpc.RawItem], bool],
        *,
        stop: Callable[[rpc.RawItem], bool] | None = None,
    ) -> Callable[[str | None], _Page]:
        """A page of matching items: reads Google's pages from ``token`` until it has FILTER_TARGET matches, read
        FILTER_MAX_PAGES pages, or ``stop`` says the listing has passed what can still match (then it ends)."""

        def fetch(token: str | None) -> _Page:
            matched: list[rpc.RawItem] = []
            current, pages, extra = token, 0, None
            while True:
                page = fetch_raw(current)
                pages += 1
                extra = page.extra if page.extra is not None else extra
                matched += [item for item in page.items if keep(item)]
                ended = stop is not None and any(stop(item) for item in page.items)
                current = None if ended else page.next_token
                if current is None or len(matched) >= FILTER_TARGET or pages >= FILTER_MAX_PAGES:
                    return _Page(matched, current, extra)

        return fetch

    @staticmethod
    def _check_details(details: Any, limit: int) -> None:
        if not isinstance(details, bool):
            raise invalid("details must be true or false")
        if details and limit > DETAILS_MAX:
            raise invalid(f"details=true works with limit up to {DETAILS_MAX}")

    def _with_details(self, result: dict[str, Any], details: bool) -> dict[str, Any]:
        """details=true: each item also gets lat, lon and camera, and place where the listing had none (one
        item-info RPC per item, ten to a request). An item whose details failed keeps them null."""
        if not details:
            return result
        items = result["items"]
        found: dict[str, Any] = {}
        for start in range(0, len(items), 10):
            chunk = [item["id"] for item in items[start : start + 10]]
            try:
                data = self._rpc(*[payloads.GetItemInfoExt(key) for key in chunk])
            except PhotosError as exc:
                if exc.code in SESSION_CODES:
                    raise
                self._log_item(exc)
                continue
            for key, raw in zip(chunk, data, strict=True):
                found[key] = rpc.parse_item_info_ext(raw)
        for item in items:
            ext = found.get(item["id"])
            item["lat"] = round(ext.lat, 6) if ext and ext.lat is not None else None
            item["lon"] = round(ext.lng, 6) if ext and ext.lng is not None else None
            item["camera"] = _camera(ext.camera) if ext else None
            if item["place"] is None and ext and ext.location_name:
                item["place"] = ext.location_name
        return result

    def _remember(self, raw_items: Sequence[rpc.RawItem]) -> None:
        with self._lock:
            for r in raw_items:
                self._meta[r.media_key] = ItemMeta(r.thumbnail_url, r.is_video, rpc.iso_time(r.taken_ts, r.tz_offset))
                self._meta.move_to_end(r.media_key)
            while len(self._meta) > INFO_CACHE:
                self._meta.popitem(last=False)

    def _filenames(self, keys: list[str]) -> dict[str, str]:
        """List RPCs carry no filename; EWgK9e does (one extra RPC per page, cached per key).

        Optional enrichment: any failure that is not about the session itself is logged and
        the items go out without filenames."""
        with self._lock:
            missing = [k for k in dict.fromkeys(keys) if k not in self._names]
        for start in range(0, len(missing), BATCH_INFO_CHUNK):
            chunk = missing[start : start + BATCH_INFO_CHUNK]
            try:
                data = self._one(payloads.GetBatchMediaInfo(chunk), required=False)
                parsed = rpc.parse_batch_info(data) if data is not None else None
            except PhotosError as exc:
                if exc.code in SESSION_CODES:
                    raise
                log.warning("EWgK9e failed (%s); filenames unavailable for %d item(s)", exc.code, len(chunk))
                break
            if parsed is None:
                log.warning("EWgK9e returned no data; filenames unavailable for %d item(s)", len(chunk))
                break
            with self._lock:
                for key, info in parsed.items():
                    self._names[key] = info
                    self._names.move_to_end(key)
                while len(self._names) > INFO_CACHE:
                    self._names.popitem(last=False)
        with self._lock:
            return {k: self._names[k].filename for k in keys if k in self._names and self._names[k].filename}  # type: ignore[misc]

    # -- internals: items ---------------------------------------------------------------

    def _info(self, key: str, ext_data: Any, info_data: Any) -> dict[str, Any]:
        ext = rpc.parse_item_info_ext(ext_data)
        info = rpc.parse_item_info(info_data)
        if ext is None and info is None:
            raise PhotosError("item_unavailable", log_detail="fDcn4b and VrseUb returned nothing")
        if info is not None:
            with self._lock:
                self._meta[key] = ItemMeta(
                    info.thumbnail_url, info.is_video, rpc.iso_time(info.taken_ts, info.tz_offset)
                )
        for album in ext.albums if ext else []:
            self._album_auth.setdefault(album.album_key, album.auth_key)

        def first(*values: Any) -> Any:
            return next((v for v in values if v is not None), None)

        taken_ts = first(ext and ext.taken_ts, info and info.taken_ts)
        tz_offset = first(ext and ext.tz_offset, info and info.tz_offset)
        duration = info.duration_ms if info else None
        item = _item(
            key,
            # Only VrseUb knows the media type; without it the type is unknown, not "photo".
            type_=("video" if info.is_video else "photo") if info else None,
            taken_ts=taken_ts,
            tz_offset=tz_offset,
            filename=ext.filename if ext else None,
            description=first(ext and ext.description, info and info.description),
            place=ext.location_name if ext else None,
            favourite=info.is_favorite if info else None,
            archived=info.is_archived if info else None,
        )
        item.update(
            uploaded_at=rpc.utc_time(info.upload_ts) if info else None,
            lat=round(ext.lat, 6) if ext and ext.lat is not None else None,
            lon=round(ext.lng, 6) if ext and ext.lng is not None else None,
            camera=_camera(first(ext and ext.camera, info and info.camera)),
            width=first(ext and ext.width, info and info.width),
            height=first(ext and ext.height, info and info.height),
            size_bytes=ext.size_bytes if ext else None,
            duration_s=round(duration / 1000, 1) if duration else None,
            albums=[
                _compact({"id": a.album_key, "title": a.title, "url": ALBUM_URL.format(a.album_key)})
                for a in (ext.albums if ext else [])
            ],
            source=ext.source if ext and ext.source else None,
            original_quality=first(ext and ext.is_original_quality, info and info.is_original_quality),
        )
        return item

    def _resolve(self, key: str) -> ItemMeta:
        (data,) = self._rpc(payloads.GetItemInfo(key))
        info = rpc.parse_item_info(data)
        if info is None or not info.thumbnail_url:
            raise PhotosError("item_unavailable", log_detail="VrseUb gave no thumbnail")
        meta = ItemMeta(info.thumbnail_url, info.is_video, rpc.iso_time(info.taken_ts, info.tz_offset))
        with self._lock:
            self._meta[key] = meta
            self._meta.move_to_end(key)
        return meta

    def _thumbnails(self, keys: list[str], px: int) -> list[tuple[bytes, dict[str, Any], bool] | PhotosError]:
        """Thumbnails for ``keys`` in order, up to THUMB_WORKERS image fetches at once (the
        transport paces images separately from RPCs). Item-level failures come back in place;
        any other failure aborts the call."""

        def one(key: str) -> tuple[bytes, dict[str, Any], bool] | PhotosError:
            try:
                return self._thumbnail(key, px)
            except PhotosError as exc:
                return exc

        if len(keys) == 1:
            results = [one(keys[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(THUMB_WORKERS, len(keys)), thread_name_prefix="thumb") as pool:
                results = list(pool.map(one, keys))
        for result in results:
            if isinstance(result, PhotosError) and result.code not in ITEM_CODES:
                raise result
        for result in results:
            if isinstance(result, PhotosError):
                self._log_item(result)
        return results

    def _thumbnail(self, key: str, px: int, *, use_cache: bool = True) -> tuple[bytes, dict[str, Any], bool]:
        cached = self._cache.get(key, px) if use_cache else None
        if cached is not None:
            return cached[0], cached[1], True
        with self._lock:
            meta = self._meta.get(key)
        from_listing = meta is not None and bool(meta.thumb_url)
        if not from_listing:
            meta = self._resolve(key)
        assert meta is not None and meta.thumb_url
        try:
            raw = self._t.fetch_image(sized_url(meta.thumb_url, px))
        except PhotosError as exc:
            # A listing URL may have gone stale: ask VrseUb for a fresh one, once.
            if exc.code != "item_unavailable" or not from_listing:
                raise
            fresh = self._resolve(key)
            if fresh.thumb_url == meta.thumb_url:
                raise
            meta = fresh
            assert meta.thumb_url
            raw = self._t.fetch_image(sized_url(meta.thumb_url, px))
        jpeg, width, height = to_jpeg(raw, px)
        info = {"w": width, "h": height, "video": meta.is_video, "taken_at": meta.taken_at}
        self._cache.put(key, px, jpeg, info)
        return jpeg, info, False

    def _freshness(self) -> dict[str, Any]:
        snapshot = self._t.current_snapshot()
        out: dict[str, Any] = {"served_at": _now_iso()}
        if snapshot is not None:
            described = snapshot.describe(self._clock())
            out.update(generation=described["generation"], session_verified_at=described["verified_at"])
        return out

    def _log_item(self, exc: PhotosError) -> None:
        """An item-level failure: logged and kept as photos_status.last_error."""
        log.info("item failed: %s (%s)", exc.code, exc.log_detail or "-")
        self.record_error(exc.code)
