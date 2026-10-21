"""Record anonymised real Google responses for the parser and classifier tests.

Run where the server runs (for example inside its container), against the live read-only
snapshot, through the same transport, allowlists, budgets and breaker as the server
(``scripts/capture-fixtures`` is the same entry point from a checkout)::

    docker exec photos-mcp python -m photos_mcp.capture /state/recorded --query beach

It writes one JSON file per recording into a new, private output directory:

* ``kind: "rpc"``: the decoded data of one RPC per allowlisted id: the library and archive
  pages (lcxiM), a search (EzkLib), the albums page (Z5xsfc), the first album's page
  (snAcKc), filenames (EWgK9e), item details (fDcn4b + VrseUb) of a photo, a video and an
  archived item, and the storage quota (EzwWhf). Lists are trimmed to ``--max-items``.
* ``kind: "http"``: the first answer of ``GET photos.google.com`` sent with **no cookie**
  (Google's real signed-out answer, zero account risk) and, when the signed-in app page
  redirects to consent, that redirect. Only status, content type, the redirect target
  without its query, and whether ``_gd`` was present are kept; never a body.

Every string value is replaced by a stable placeholder of the same kind (media / album key,
URL on the same host, e-mail, filename with its media extension (any other dotted text is
free text: ``ANON_n``), digits, other text: names, places, titles, descriptions, tokens).
No number about the account survives either:

* epoch-millisecond timestamps (taken, uploaded, created) become synthetic instants in
  January 2020 (``SYNTH_EPOCH_MS`` + n minutes), in one order across all the recordings of
  a run;
* time-zone offsets at their known positions become ``SYNTH_OFFSET_MS`` (+01:00), so a known
  offset stays known without telling where the photo was taken;
* numbers that could be coordinates (non-integral floats, integers of E7 magnitude) are
  zeroed wherever they sit, as is every number inside the geo extension; fDcn4b's known
  coordinate slots get a fixed point;
* every other integer above ``KEEP_INT`` (sizes, dimensions, counts, quota) becomes a stable
  synthetic ``SYNTH_INT_BASE`` + n, whatever its size (a real number never lands there: that
  range is zeroed as coordinate-like); smaller ones (flags, enums) are kept.

Structure and types are kept. ``--rescrub DIR`` re-applies these rules to recordings in
place. Review the output before checking any of it in under ``tests/fixtures/recorded/``
(``tests/test_fixture_privacy.py`` checks the rules), then delete the output directory. Use
``--query videos`` when the library page holds no video.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import rpc
from .client import PhotosService
from .errors import PhotosError
from .transport import IMAGE_HOSTS, PAGE_HOST, HttpResponse
from .vendor.gpwc import payloads
from .vendor.gpwc.payloads import Payload

log = logging.getLogger(__name__)

FORMAT = 1
MAX_ITEMS = 10
# Synthetic numbers (see the module docstring).
SYNTH_EPOCH_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z
SYNTH_STEP_MS = 60_000
SYNTH_OFFSET_MS = 3_600_000
SYNTH_INT_BASE = 7_000_000
KEEP_INT = 64
TIMESTAMP_MS = range(10**12, 2 * 10**12)
# Expected classification of each HTTP recording (README "Liveness").
HTTP_EXPECT = {"signed_out": "session_expired", "consent": "consent_required"}
FIXED_E7 = (515_000_000, -1_000_000)  # a fixed point replacing recorded coordinates

_KEY = re.compile(r"AF1Qip[A-Za-z0-9_-]+")
_URL = re.compile(r"https?://\S+")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_FILENAME = re.compile(r"[^/\\\s][^/\\]{0,200}\.([A-Za-z0-9]{2,5})")
# Only a known media extension survives; any other dotted tail is free text ("Jane.Doe").
MEDIA_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "avif", "dng", "raw", "cr2", "nef", "arw", "tif", "tiff",
     "bmp", "mp4", "mov", "m4v", "3gp", "avi", "mkv", "webm", "mts", "mpg", "mpeg", "wmv"}
)  # fmt: skip
_DIGITS = re.compile(r"\d+")


class Anonymiser:
    """Stable placeholders: the same input always maps to the same output within one run."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._numbers: dict[int, int] = {}
        self._counts: Counter[str] = Counter()

    def _next(self, kind: str) -> int:
        self._counts[kind] += 1
        return self._counts[kind]

    def string(self, value: str) -> str:
        if not value:
            return value
        if value in self._map:
            return self._map[value]
        if _KEY.fullmatch(value):
            out = f"AF1QipREC{self._next('key'):06d}"
        elif _URL.fullmatch(value):
            host = (urlsplit(value).hostname or "").lower()
            if host != PAGE_HOST and not IMAGE_HOSTS.match(host):
                host = "example.invalid"
            out = "https://" + host + f"/ANON_{self._next('url')}"
        elif _EMAIL.fullmatch(value):
            out = f"user{self._next('email')}@example.com"
        elif (match := _FILENAME.fullmatch(value)) and match.group(1).lower() in MEDIA_EXTENSIONS:
            out = f"file_{self._next('file')}.{match.group(1).lower()}"
        elif _DIGITS.fullmatch(value):
            out = "8" + str(self._next("digits")).zfill(max(len(value) - 1, 1))
        else:
            out = f"ANON_{self._next('text')}"
        self._map[value] = out
        return out

    def seed_timestamps(self, data: Any) -> None:
        """Give this value's unseen timestamps synthetic instants in their own order."""
        for ts in sorted(set(_timestamps(data)) - set(self._numbers)):
            self._numbers[ts] = SYNTH_EPOCH_MS + self._next("timestamp") * SYNTH_STEP_MS

    def number(self, value: int | float) -> int | float:
        if isinstance(value, float):
            return value if value.is_integer() and abs(value) <= KEEP_INT else 0.0
        if abs(value) <= KEEP_INT:
            return value
        if looks_like_coordinate(value):
            return 0
        if value not in self._numbers:
            if value in TIMESTAMP_MS:
                self._numbers[value] = SYNTH_EPOCH_MS + self._next("timestamp") * SYNTH_STEP_MS
            else:
                synthetic = SYNTH_INT_BASE + self._next("int")
                self._numbers[value] = synthetic if value > 0 else -synthetic
        return self._numbers[value]

    def value(self, value: Any, *, scrub_numbers: bool = False) -> Any:
        """Anonymise a decoded JSON value. Dict keys (extension field numbers) are kept."""
        if isinstance(value, str):
            return self.string(value)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int | float):
            return type(value)(0) if scrub_numbers else self.number(value)
        if isinstance(value, list):
            return [self.value(v, scrub_numbers=scrub_numbers) for v in value]
        if isinstance(value, dict):
            return {k: self.value(v, scrub_numbers=scrub_numbers or k == rpc.EXT_GEO) for k, v in value.items()}
        return value


def looks_like_coordinate(value: int | float) -> bool:
    """A degree value (non-integral float within ±180) or an E7 integer (0.1° to 180°)."""
    if isinstance(value, float) and not value.is_integer():
        return abs(value) <= 180
    return 1_000_000 <= abs(value) <= 1_800_000_000


def _timestamps(value: Any) -> list[int]:
    if isinstance(value, list):
        return [ts for v in value for ts in _timestamps(v)]
    if isinstance(value, dict):
        return [ts for v in value.values() for ts in _timestamps(v)]
    if isinstance(value, int) and not isinstance(value, bool) and value in TIMESTAMP_MS:
        return [value]
    return []


def _offset_slots(rpcid: str, data: Any) -> list[tuple[list, int]]:
    """Where timezone offsets (ms) sit: E7-sized, so zeroed first and marked afterwards."""
    records: list[Any] = []
    index = 4
    if rpcid in ("lcxiM", "EzkLib"):
        records = rpc.field(data, 0, default=[])
    elif rpcid == "snAcKc":
        records = rpc.field(data, 1, default=[])
    elif rpcid in ("fDcn4b", "VrseUb"):
        records = [rpc.field(data, 0)]
    elif rpcid == "EWgK9e":
        records = [rpc.field(row, 1) for row in rpc.path(data, 0, 1, default=[]) or []]
        index = 7
    return [(r, index) for r in records if isinstance(r, list) and len(r) > index and not isinstance(r[index], dict)]


def _mark_offsets(rpcid: str, original: Any, anonymised: Any) -> None:
    """A known offset stays known (SYNTH_OFFSET_MS), never its real value."""
    for (source, index), (target, _) in zip(
        _offset_slots(rpcid, original), _offset_slots(rpcid, anonymised), strict=True
    ):
        if isinstance(source[index], int) and not isinstance(source[index], bool):
            target[index] = SYNTH_OFFSET_MS


def _scrub_coordinates(rpcid: str, data: Any) -> None:
    """fDcn4b keeps E7 coordinates outside the geo extension: ``[0][9][0]``, ``[0][13][0]``."""
    if rpcid != "fDcn4b":
        return
    item = rpc.field(data, 0)
    for index in (9, 13):
        pair = rpc.path(item, index, 0)
        if isinstance(pair, list) and len(pair) >= 2 and all(isinstance(v, int | float) for v in pair[:2]):
            pair[0], pair[1] = FIXED_E7


def _trim(rpcid: str, data: Any, max_items: int) -> Any:
    """Keep at most ``max_items`` records of a list answer (structure unchanged)."""
    holder, index = None, None
    if rpcid in ("lcxiM", "EzkLib", "Z5xsfc"):
        holder, index = data, 0
    elif rpcid == "snAcKc":
        holder, index = data, 1
    elif rpcid == "EWgK9e":
        holder, index = rpc.field(data, 0), 1
    if isinstance(holder, list) and index < len(holder) and isinstance(holder[index], list):
        holder[index] = holder[index][:max_items]
    return data


def rpc_recording(anonymiser: Anonymiser, rpcid: str, label: str, data: Any, max_items: int = MAX_ITEMS) -> dict:
    data = _trim(rpcid, json.loads(json.dumps(data)), max_items)
    anonymiser.seed_timestamps(data)
    anonymised = anonymiser.value(data)
    _mark_offsets(rpcid, data, anonymised)
    _scrub_coordinates(rpcid, anonymised)
    return {"format": FORMAT, "kind": "rpc", "rpcid": rpcid, "label": label, "data": anonymised}


def http_recording(label: str, response: HttpResponse, *, cookies: bool) -> dict:
    location = response.headers.get("location")
    if location:
        parts = urlsplit(location)
        location = f"{parts.scheme}://{parts.netloc}{parts.path}"  # no query: continue URLs, ids
    body = response.body[:1_000_000]
    return {
        "format": FORMAT,
        "kind": "http",
        "label": label,
        "request": "page",
        "cookies": cookies,
        "status": response.status,
        "content_type": response.content_type,
        "location": location,
        "has_gd": b'data-id="_gd"' in body or b"WIZ_global_data" in body,
        "expect": HTTP_EXPECT[label],
    }


def capture(
    service: PhotosService,
    probe: Callable[[bool], HttpResponse],
    out_dir: Path,
    *,
    query: str,
    max_items: int = MAX_ITEMS,
) -> tuple[list[str], dict[str, str]]:
    """Record everything that works; returns (files written, failed step → error code).

    A session-level failure (expired, blocked, mismatch) stops the run at once."""
    anonymiser = Anonymiser()
    written: list[str] = []
    failed: dict[str, str] = {}

    def save(name: str, record: dict) -> None:
        record["captured_at"] = datetime.now(UTC).date().isoformat()
        path = out_dir / f"{name}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        written.append(path.name)

    def step(name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except PhotosError as exc:
            failed[name] = exc.code
            log.warning("capture step %s failed: %s", name, exc.code)
            if exc.code in ("session_expired", "blocked", "account_mismatch", "session_unavailable"):
                raise
            return None

    def one(payload: Payload) -> Any:
        return service._one(payload, required=True)

    # RPC answers are anonymised together at the end, so their timestamps share one order.
    pending: list[tuple[str, str, str, Any]] = []

    def record(name: str, payload: Payload) -> Any:
        data = step(name, lambda: one(payload))
        if data is not None:
            pending.append((name, payload.rpcid, name.split(".", 1)[1], data))
        return data

    try:
        library = record("lcxiM.library", payloads.GetLibraryPageByTakenDate(source="library", page_size=100))
        archive = record("lcxiM.archive", payloads.GetLibraryPageByTakenDate(source="archive", page_size=100))
        search = record("EzkLib.search", payloads.GetSearchPage(query))
        albums = record("Z5xsfc.albums", payloads.GetAlbumsPage(page_size=100))
        if albums is not None:
            parsed_albums, _ = rpc.parse_albums_page(albums)
            if parsed_albums:
                first = parsed_albums[0]
                record("snAcKc.album", payloads.GetAlbumPage(first.album_key, auth_key=first.auth_key))
        library_items = rpc.parse_library_page(library).items if library is not None else []
        archive_items = rpc.parse_library_page(archive).items if archive is not None else []
        if library_items:
            record("EWgK9e.filenames", payloads.GetBatchMediaInfo([r.media_key for r in library_items[:5]]))
        search_items = rpc.parse_library_page(search).items if search is not None else []
        # A recent library page may hold no video (recorded live); the search often does.
        candidates = library_items + search_items
        picks = {
            "photo": next((r for r in library_items if not r.is_video), None),
            "video": next((r for r in candidates if r.is_video), None),
            "archived": archive_items[0] if archive_items else None,
        }
        for label, item in picks.items():
            if item is None:
                failed[f"item.{label}"] = "not_found"
                continue
            results = step(
                f"item.{label}",
                lambda key=item.media_key: service._rpc(payloads.GetItemInfoExt(key), payloads.GetItemInfo(key)),
            )
            if results is None:
                continue
            for rpcid, data in zip(("fDcn4b", "VrseUb"), results, strict=True):
                if data is not None:
                    pending.append((f"{rpcid}.{label}", rpcid, label, data))
        record("EzwWhf.quota", payloads.GetStorageQuota())

        signed_out = step("http.signed_out", lambda: probe(False))
        if signed_out is not None:
            save("http.signed_out", http_recording("signed_out", signed_out, cookies=False))
        page = step("http.page", lambda: probe(True))
        if page is not None and 300 <= page.status < 400:
            target = (urlsplit(page.headers.get("location", "")).hostname or "").lower()
            if target == "consent.google.com":
                save("http.consent", http_recording("consent", page, cookies=True))
    finally:
        trimmed = [
            (name, rpcid, label, _trim(rpcid, json.loads(json.dumps(data)), max_items))
            for name, rpcid, label, data in pending
        ]
        anonymiser.seed_timestamps([d for *_, d in trimmed])
        for name, rpcid, label, data in trimmed:
            save(name, rpc_recording(anonymiser, rpcid, label, data, max_items))
    return written, failed


def rescrub(directory: Path) -> list[str]:
    """Re-apply the anonymiser to recordings in place (one anonymiser for all, in name order,
    so keys and timestamps stay consistent across files). Returns the files rewritten."""
    anonymiser = Anonymiser()
    records = [(path, json.loads(path.read_text())) for path in sorted(directory.glob("*.json"))]
    records = [(path, record) for path, record in records if record.get("kind") == "rpc"]
    anonymiser.seed_timestamps([record["data"] for _, record in records])  # one order across files
    rewritten = []
    for path, record in records:
        fresh = rpc_recording(anonymiser, record["rpcid"], record["label"], record["data"], max_items=10**6)
        record["data"] = fresh["data"]
        path.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        rewritten.append(path.name)
    return rewritten


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capture-fixtures", description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir", type=Path, help="new directory for the recordings (created, mode 0700)")
    parser.add_argument("--query", default="2024", help="search query to record (default: 2024)")
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS, help="records kept per list (default 10)")
    parser.add_argument(
        "--rescrub", action="store_true", help="re-anonymise the recordings already in out_dir, offline"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.rescrub:
        print(json.dumps({"rewritten": rescrub(args.out_dir)}, indent=2))
        return 0

    from .cache import ThumbCache
    from .config import Settings
    from .session import SessionReader
    from .state import StateStore
    from .transport import GoogleTransport

    settings = Settings.from_env()
    out_dir: Path = args.out_dir
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        print(f"{out_dir} is not empty", file=sys.stderr)
        return 2
    state = StateStore(
        settings.state_dir,
        rpc_budget=settings.daily_rpc_budget,
        thumb_budget=settings.daily_thumb_budget,
        budget_tz=settings.budget_tz,
    )
    transport = GoogleTransport(SessionReader(settings.session_dir), state, expected_email=settings.expected_email)
    service = PhotosService(transport, ThumbCache(settings.cache_dir, 0))
    try:
        written, failed = capture(
            service,
            lambda cookies: transport.probe_page(with_cookies=cookies),
            out_dir,
            query=args.query,
            max_items=args.max_items,
        )
    except PhotosError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 1
    print(json.dumps({"written": written, "failed": failed}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
