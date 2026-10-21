"""The tools' logic: plain arguments in, compact JSON-ready dicts out.

Requests are built with the vendored gpwc payloads and sent through ``GoogleTransport``;
responses are parsed by :mod:`photos_mcp.rpc`. Nothing here formats exception text.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from . import __version__, rpc
from .cache import ThumbCache
from .cursors import CursorState, CursorStore, scope_of
from .errors import PhotosError, invalid
from .images import MAX_PX, TILE_MAX, Tile, contact_sheet, to_jpeg
from .session import Snapshot
from .state import StateStore
from .transport import RpcResult
from .vendor.gpwc import payloads
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


class Transport(Protocol):
    state: StateStore

    def rpc(self, payloads: Sequence[Payload]) -> list[RpcResult]: ...

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
) -> dict[str, Any]:
    """The one item shape of list, search, album and get: every key always present, null
    when unknown. ``taken_at`` is local time with its offset when ``tz_known``, else UTC (Z)."""
    return {
        "id": key,
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
    ) -> None:
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
        self._canary_lock = threading.Lock()
        # (checked_at, generation checked, ok, code)
        self._canary: tuple[float, int | None, bool, str | None] | None = None

    @property
    def state(self) -> StateStore:
        return self._t.state

    # -- tools --------------------------------------------------------------------------

    def search(self, query: str, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        limit = check_limit(limit)
        query = (query or "").strip() if isinstance(query, str) else ""
        if not query or len(query) > 200:
            raise invalid("query must be 1 to 200 characters")

        def fetch(token: str | None) -> _Page:
            data = self._one(payloads.GetSearchPage(query, page_id=token), required=True)
            page = rpc.parse_library_page(data)
            return _Page(page.items, page.next_token)

        items, next_cursor, coverage = self._paged(scope_of("search", query.casefold()), cursor, limit, fetch)
        coverage["archive_included"] = True  # Google's search finds archived items too
        return self._listing(items, next_cursor, coverage)

    def list(
        self, source: str = "library", album_id: str | None = None, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        limit = check_limit(limit)
        if source not in ("library", "archive", "album"):
            raise invalid("source must be library, archive or album")
        if (source == "album") != (album_id is not None):
            raise invalid("album_id is required for source=album and only then")
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

            items, next_cursor, coverage = self._paged(scope_of("album", key), cursor, limit, fetch_album)
            coverage["archive_included"] = True
            result = self._listing(items, next_cursor, coverage)
            album = self._last_extra(scope_of("album", key))
            if album:
                result["album"] = _compact(album)
            return result

        lcxim_source = "archive" if source == "archive" else "library"

        def fetch(token: str | None) -> _Page:
            payload = payloads.GetLibraryPageByTakenDate(
                page_id=token, source=lcxim_source, page_size=LIBRARY_UPSTREAM_PAGE
            )
            page = rpc.parse_library_page(self._one(payload, required=True))
            return _Page(page.items, page.next_token)

        items, next_cursor, coverage = self._paged(scope_of(source), cursor, limit, fetch)
        coverage["archive_included"] = source == "archive"
        # Recorded live: lcxiM library records end before the archived flag (index 13), but the
        # source itself says it: library pages hold only unarchived items, archive pages only archived.
        return self._listing(items, next_cursor, coverage, archived_default=source == "archive")

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
            return _Page(albums, next_token)

        albums, next_cursor, coverage = self._paged(scope_of("albums"), cursor, limit, fetch)
        coverage.pop("shared_partner", None)
        return {
            "albums": [
                _compact({"id": a.album_key, "title": a.title, "count": a.item_count, "shared": a.is_shared})
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
        session: dict[str, Any]
        if snapshot is None:
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
        return {
            "service": "photos-mcp",
            "version": __version__,
            "read_only": True,
            "session": session,
            "breaker": state["breaker"],
            "budgets": state["budgets"],
            "last_error": state["last_error"],
            "cache": {"bytes": self._cache.size(), "max_bytes": self._cache.max_bytes},
        }

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
    ) -> dict[str, Any]:
        self._remember(raw_items)
        names = self._filenames([r.media_key for r in raw_items])
        items = [
            _item(
                r.media_key,
                type_="video" if r.is_video else "photo",
                taken_ts=r.taken_ts,
                tz_offset=r.tz_offset,
                filename=names.get(r.media_key),
                description=r.description,
                place=r.place,
                favourite=r.is_favorite,
                archived=r.is_archived if r.is_archived is not None else archived_default,
            )
            for r in raw_items
        ]
        return {"items": items, "next_cursor": next_cursor, "coverage": coverage, "freshness": self._freshness()}

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
            albums=[_compact({"id": a.album_key, "title": a.title}) for a in (ext.albums if ext else [])],
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
