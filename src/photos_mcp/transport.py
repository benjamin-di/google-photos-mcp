"""Every byte to and from Google goes through here (README "Safety and Google posture").

* RPCs and the app page: one request in flight, at least 350 ms apart (jittered). Image
  fetches have their own pacer: up to 4 in flight, starts at least 100 ms apart. Callers
  queue for up to 30 s, then get ``busy``.
* Request shape: the snapshot's per-kind header template (``page``/``rpc``/``image``) plus
  the request-specific referer, accept-encoding and cookie, all in Chrome's wire order
  (``session.CANONICAL_ORDER``: those three last, before ``priority``; unknown names just
  before ``sec-fetch-site``);
  curl_cffi impersonates the newest ``chromeN`` profile with N ≤ the snapshot's Chromium
  major and adds no headers of its own (README "Session snapshot").
* Timeouts 10 s connect / 20 s read, and a body must be complete within 30 s
  (``BODY_DEADLINE``). At most 2 retries, only on 5xx and network errors.
* Liveness: only a redirect to accounts.google.com or google.com/photos/about, or an
  account mismatch, marks a generation dead; a bare 401 and a consent redirect are
  reported, not dead; anything else unproven is ``upstream_error``.
* RPC id allowlists and host allowlist (HTTPS only): ``rpc`` sends only the read RPCs; ``write``
  sends only the changes in ``WRITE_RPC_ALLOWLIST`` (album changes, making an uploaded item) and never
  retries a request that may have reached Google (``outcome_unknown``). ``upload_bytes`` sends a file
  to Google's upload endpoint (``upload.UPLOAD_PATH``) and nowhere else, one file at a time per slot of
  its own pacer (2 slots), so a long upload never holds the RPC slot. Redirects are followed by hand,
  at most 3, and only within the allowlist; a redirect elsewhere is classified, never followed.
* Streamed body caps: 8 MiB per RPC, page or upload answer, 15 MiB per image.
* Daily budgets and the persisted breaker (``state.StateStore``).
* Precise failure classification into the codes of ``errors.NEXT_STEP``.

Logs carry RPC ids, host, status, bytes and milliseconds only: never URLs (the query holds
``f.sid``), cookies, tokens or bodies.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Protocol
from urllib.parse import urljoin, urlsplit

from . import logs, upload
from .errors import PhotosError
from .session import CookieStore, SessionReader, Snapshot, Template, canonical_order
from .state import StateStore
from .vendor.gpwc import Client, parse_api_response, parse_envelopes, parse_main_page
from .vendor.gpwc.payloads import Payload

log = logging.getLogger(__name__)

# Reads, and swbisb: whether the library already holds a file (by its SHA-1), which an upload asks first.
RPC_ALLOWLIST = frozenset({"lcxiM", "EzkLib", "EWgK9e", "Z5xsfc", "snAcKc", "VrseUb", "fDcn4b", "EzwWhf", "swbisb"})
# Album changes: create an album (OXvT9d), add items to an album (E1Cajb) or a shared album (laUYf), remove items
# from an album (ycV3Nd) or a shared album (LjmOue); and make a media item from an upload (mdpdU). Nothing here
# deletes, trashes, archives, edits or shares media.
WRITE_RPC_ALLOWLIST = frozenset({"OXvT9d", "E1Cajb", "laUYf", "ycV3Nd", "LjmOue", "mdpdU"})
PAGE_HOST = "photos.google.com"
IMAGE_HOSTS = re.compile(r"^(photos\.fife\.usercontent\.google\.com|lh[0-9]\.googleusercontent\.com)$")
PHOTOS_URL = "https://photos.google.com/"

MIN_INTERVAL = 0.35
JITTER = 0.15
IMAGE_CONCURRENCY = 4
IMAGE_MIN_INTERVAL = 0.1
IMAGE_JITTER = 0.05
UPLOAD_CONCURRENCY = 2
QUEUE_WAIT = 30.0
CONNECT_TIMEOUT, READ_TIMEOUT = 10.0, 20.0
# curl's read timeout on a streamed body is a low-speed limit, not a total: a body that
# trickles in keeps the single RPC slot. Any response must be complete within this long.
BODY_DEADLINE = 30.0
MAX_RETRIES = 2
RETRY_BASE = 1.0
MAX_REDIRECTS = 3
RPC_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_BYTES = 15 * 1024 * 1024

FORM_TYPE = "application/x-www-form-urlencoded;charset=UTF-8"
# The web app's upload requests carry a form content type whatever the bytes are; the chunks in lower case, as recorded.
CHUNK_TYPE = "application/x-www-form-urlencoded;charset=utf-8"
UPLOAD_URL = f"https://photos.google.com{upload.UPLOAD_PATH}"
# Chrome's Accept-Encoding. Templates never carry it (README "Session snapshot"); the transport sets
# it at Chrome's position and curl decodes these encodings.
ACCEPT_ENCODING = "gzip, deflate, br, zstd"
REFERER = "https://photos.google.com/"
Headers = Sequence[tuple[str, str]]


def request_headers(
    kind: str,
    template: Template,
    *,
    cookie: str | None,
    referer: str | None = None,
    content_type: str | None = None,
) -> list[tuple[str, str]]:
    """The template plus the request-specific headers (``referer``, ``accept-encoding``,
    ``cookie``, a form ``content-type``), all in Chrome's wire order for ``kind``
    (``session.CANONICAL_ORDER``, recorded from a real Chrome).

    This is the order handed to curl. On HTTP/1.1 it is the wire order; on HTTP/2 curl adds
    ``content-length`` itself, so that one header's position is not Chrome's."""
    out = [(name, content_type if name == "content-type" and content_type else value) for name, value in template]
    if content_type and not any(name == "content-type" for name, _ in out):
        out.append(("content-type", content_type))
    if referer:
        out.append(("referer", referer))
    out.append(("accept-encoding", ACCEPT_ENCODING))
    if cookie:
        out.append(("cookie", cookie))
    return list(canonical_order(kind, out))


def headers_for(
    snapshot: Snapshot,
    kind: str,
    method: str,
    cookie: str | None,
    *,
    extra: Headers = (),
    content_type: str | None = None,
) -> list[tuple[str, str]]:
    """Every header of one request of ``kind`` (``page``, ``rpc``, ``image`` or ``upload``), in order.

    An upload request is the app's own XHR to the upload endpoint: the ``rpc`` template without ``x-same-domain``
    (which only batchexecute carries), its content type, and ``extra`` (the ``x-goog-upload-*`` headers, placed as
    Chrome places names it does not order itself)."""
    if kind == "upload":
        template = tuple((name, value) for name, value in snapshot.headers["rpc"] if name != "x-same-domain")
        return request_headers(
            "rpc", (*template, *extra), cookie=cookie, referer=REFERER, content_type=content_type or FORM_TYPE
        )
    return request_headers(
        kind,
        snapshot.headers[kind],
        cookie=cookie,
        referer=None if kind == "page" else REFERER,
        content_type=FORM_TYPE if method == "POST" else None,
    )


def chrome_profiles() -> list[tuple[int, str]]:
    """Desktop ``chromeN`` impersonation targets of the installed curl_cffi, oldest first."""
    from curl_cffi.requests import BrowserType

    found: dict[int, str] = {}
    for browser in BrowserType:
        match = re.fullmatch(r"chrome(\d+)(a?)", browser.value)
        if match:
            major = int(match.group(1))
            if major not in found or not match.group(2):  # prefer chromeN over chromeNa
                found[major] = browser.value
    return sorted(found.items())


def impersonate_target(chromium_major: int) -> str:
    """The newest ``chromeN`` with N ≤ ``chromium_major``; the nearest one when none is older."""
    profiles = chrome_profiles()
    older = [name for major, name in profiles if major <= chromium_major]
    if older:
        return older[-1]
    return min(profiles, key=lambda item: abs(item[0] - chromium_major))[1]


def host_allowed(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
        return False
    return host == PAGE_HOST or bool(IMAGE_HOSTS.match(host))


class NetworkError(Exception):
    """Connect/read failure or timeout (retryable)."""


class BodyTooLarge(Exception):
    """The response exceeded its streamed cap."""


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]  # lower-case names
    body: bytes
    set_cookies: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


class Sender(Protocol):
    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Headers,
        params: Mapping[str, str] | None = None,
        data: str | bytes | None = None,
        max_bytes: int,
    ) -> HttpResponse: ...


class CurlSender:
    """curl_cffi with the TLS/HTTP2 fingerprint of the snapshot's Chrome version, exactly the
    headers it is given (``default_headers=False``), no cookie jar (cookies come from our
    CookieStore per request) and no automatic redirects."""

    def __init__(self, snapshot: Snapshot) -> None:
        from curl_cffi import requests as curl_requests

        self._errors = (curl_requests.RequestsError,)
        self.impersonate = impersonate_target(snapshot.chromium_major)
        self._session = curl_requests.Session(
            impersonate=self.impersonate,  # type: ignore[arg-type]
            default_headers=False,
            allow_redirects=False,
            discard_cookies=True,
            max_redirects=0,
        )

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Headers,
        params: Mapping[str, str] | None = None,
        data: str | bytes | None = None,
        max_bytes: int,
    ) -> HttpResponse:
        try:
            response = self._session.request(
                method,  # type: ignore[arg-type]
                url,
                params=dict(params) if params else None,
                data=data,
                headers=list(headers),  # type: ignore[arg-type]
                # Enables decoding; the header itself comes from ``headers`` at Chrome's position.
                accept_encoding=ACCEPT_ENCODING,
                stream=True,
                allow_redirects=False,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except self._errors as exc:
            raise NetworkError(type(exc).__name__) from None
        try:
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise BodyTooLarge
            chunks: list[bytes] = []
            total = 0
            deadline = time.monotonic() + BODY_DEADLINE
            for chunk in response.iter_content():
                if time.monotonic() > deadline:
                    raise NetworkError("BodyDeadline")
                total += len(chunk)
                if total > max_bytes:
                    raise BodyTooLarge
                chunks.append(chunk)
        except self._errors as exc:
            raise NetworkError(type(exc).__name__) from None
        finally:
            response.close()
        headers_out = {str(k).lower(): str(v) for k, v in response.headers.items()}
        set_cookies = [str(v) for v in response.headers.get_list("set-cookie")]
        return HttpResponse(response.status_code, headers_out, b"".join(chunks), set_cookies)


@dataclass(frozen=True)
class RpcResult:
    rpcid: str
    payload_id: str
    data: Any  # decoded RPC result, or None when Google reported an error for that RPC


class Pacer:
    """At most ``concurrency`` requests in flight; callers wait up to ``queue_wait`` s for a
    slot, then ``busy``. With one slot the gap runs from the end of the previous request; with
    several, from the previous start."""

    def __init__(
        self,
        *,
        concurrency: int = 1,
        min_interval: float = MIN_INTERVAL,
        jitter: float = JITTER,
        queue_wait: float = QUEUE_WAIT,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self.queue_wait = queue_wait
        self.sleep = sleep
        self._monotonic = monotonic
        self._rand = rand
        self.concurrency = concurrency
        self._slots = threading.BoundedSemaphore(concurrency)
        self._gate = threading.Lock()
        self._last: float | None = None  # end of the previous request
        self._last_start: float | None = None  # (reserved) start of the previous request

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self._slots.acquire(timeout=self.queue_wait):
            raise PhotosError("busy", log_detail="queue wait exceeded")
        try:
            yield
        finally:
            self._slots.release()

    def space(self) -> None:
        """Called inside a slot before each HTTP request: wait out the gap to the previous one."""
        with self._gate:
            reference = self._last if self.concurrency == 1 else self._last_start
            now = self._monotonic()
            wait = 0.0
            if reference is not None:
                wait = max(0.0, reference + self.min_interval + self.jitter * self._rand() - now)
            self._last_start = now + wait
        if wait > 0:
            self.sleep(wait)

    def done(self) -> None:
        """Called when a request finished (successfully or not)."""
        with self._gate:
            self._last = self._monotonic()

    def backoff(self, attempt: int) -> None:
        self.sleep(RETRY_BASE * (2**attempt) * (0.5 + self._rand()))


@dataclass
class _Context:
    """Per-generation session state: HTTP client, in-memory cookies, cached page globals."""

    snapshot: Snapshot
    sender: Sender
    cookies: CookieStore
    client: Client | None = None


# google.com and Google's country domains (google.de, google.co.uk, google.com.au, ...).
_GOOGLE_HOST = re.compile(r"(?:^|\.)google\.(?:com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$")


def _redirect_code(location: str) -> str | None:
    """Classify a redirect target that must not be followed."""
    parts = urlsplit(location)
    host = (parts.hostname or "").lower()
    if host == "accounts.google.com" or host.endswith(".accounts.google.com"):
        return "session_expired"
    if host == "consent.google.com":
        return "consent_required"
    if _GOOGLE_HOST.search(host) and parts.path.startswith("/sorry"):
        return "blocked"  # Google's block page, on google.com or a country domain
    if host in ("google.com", "www.google.com") and parts.path.startswith("/photos/about"):
        return "session_expired"  # Google's signed-out landing page for photos.google.com
    return None


_CAPTCHA = re.compile(rb"g-recaptcha|/sorry/index|unusual traffic from your computer", re.I)


class GoogleTransport:
    """Session-aware, policy-enforcing access to photos.google.com and its image CDN."""

    def __init__(
        self,
        reader: SessionReader,
        state: StateStore,
        *,
        expected_email: str | None,
        sender_factory: Callable[[Snapshot], Sender] = CurlSender,
        pacer: Pacer | None = None,
        image_pacer: Pacer | None = None,
        upload_pacer: Pacer | None = None,
        clock: Callable[[], float] = time.time,
        on_dead: Callable[[int, str], None] | None = None,
    ) -> None:
        self.reader = reader
        # Hears (generation, code) when Google proved a generation dead: a keeper can then look at once.
        self._on_dead = on_dead
        self.state = state
        # None: a linked account no sign-in has bound to a Google address yet; it serves nothing.
        self.expected_email = expected_email.strip().lower() if expected_email else None
        self._sender_factory = sender_factory
        self.pacer = pacer or Pacer()
        self.image_pacer = image_pacer or Pacer(
            concurrency=IMAGE_CONCURRENCY, min_interval=IMAGE_MIN_INTERVAL, jitter=IMAGE_JITTER
        )
        self.upload_pacer = upload_pacer or Pacer(concurrency=UPLOAD_CONCURRENCY)
        self._clock = clock
        self._ctx: _Context | None = None
        # Image fetches run concurrently with each other and with RPCs: guard shared state.
        self._ctx_lock = threading.RLock()
        self._cookie_lock = threading.Lock()

    # -- public -------------------------------------------------------------------------

    def rpc(self, payloads: Sequence[Payload]) -> list[RpcResult]:
        """One batchexecute POST for ``payloads``; raw data per envelope (None = RPC failed)."""
        payloads = list(payloads)
        rpcids = [p.rpcid for p in payloads]
        if not payloads or any(r not in RPC_ALLOWLIST for r in rpcids):
            raise RuntimeError("RPC id not on the allowlist")
        with self.pacer.slot():
            ctx = self._context()
            self._ensure_globals(ctx)
            response = self._post(ctx, payloads)
            if response.status in (400, 403):
                # A stale at/f.sid token: rebuild the page globals once and retry once.
                self._event("stale_token", rpcids=",".join(rpcids), status=response.status)
                ctx.client = None
                self._ensure_globals(ctx)
                response = self._post(ctx, payloads)
                if response.status in (400, 403):
                    raise self._fail(ctx, "stale_token", f"HTTP {response.status} twice")
            return self._parse_rpc(ctx, response, payloads)

    def write(self, payloads: Sequence[Payload]) -> list[RpcResult]:
        """One batchexecute POST of changes (album changes, or making an uploaded item). A 400/403 (Google refused
        it, nothing applied) is retried once with fresh page globals, as for reads; a 5xx or a network failure is
        never retried: the change may have reached Google, so it is ``outcome_unknown`` and the caller reads back
        what happened."""
        payloads = list(payloads)
        rpcids = [p.rpcid for p in payloads]
        if not payloads or any(r not in WRITE_RPC_ALLOWLIST for r in rpcids):
            raise RuntimeError("RPC id not on the write allowlist")
        with self.pacer.slot():
            ctx = self._context()
            self._ensure_globals(ctx)
            response = self._post(ctx, payloads, retries=0, unknown="outcome_unknown")
            if response.status in (400, 403):
                self._event("stale_token", rpcids=",".join(rpcids), status=response.status)
                ctx.client = None
                self._ensure_globals(ctx)
                response = self._post(ctx, payloads, retries=0, unknown="outcome_unknown")
                if response.status in (400, 403):
                    raise PhotosError("refused", log_detail=f"write HTTP {response.status} twice")
            return self._parse_rpc(ctx, response, payloads)

    def upload_bytes(self, source: BinaryIO, *, size: int, name: str, sha1: str) -> str:
        """Send one file (``size`` bytes read from ``source``) with Google's resumable upload and return the upload
        token ``mdpdU`` makes the item from (``upload`` module). Nothing is in the library before that, so no failure
        here is ``outcome_unknown``: the start is retried like a read, a chunk never (the upload would need a status
        query first), and the caller may simply send the file again. The upload endpoint costs one unit of the RPC
        budget per file, whatever its size (its chunks are bytes, not requests the budget is for)."""
        if size <= 0:
            raise PhotosError("invalid_argument", "the file is empty")
        common = (("x-goog-hash", f"sha1={sha1}"), ("x-goog-upload-file-name", upload.header_filename(name)))
        with self.upload_pacer.slot():
            ctx = self._context()
            self.state.take("rpc")
            start = self._upload_post(
                ctx,
                UPLOAD_URL,
                params={"authuser": "0"},
                extra=(
                    common[0],
                    ("x-goog-upload-protocol", "resumable"),
                    common[1],
                    ("x-goog-upload-header-content-length", str(size)),
                    ("x-goog-upload-command", "start"),
                ),
                data=upload.start_body(size),
                content_type=FORM_TYPE,
                retries=MAX_RETRIES,
            )
            if start.headers.get("x-goog-upload-status") != "active":
                raise PhotosError("schema_changed", log_detail="upload start: no active upload")
            target = start.headers.get("x-goog-upload-url", "")
            parts = urlsplit(target)
            if (
                not host_allowed(target)
                or (parts.hostname or "").lower() != PAGE_HOST
                or parts.path != upload.UPLOAD_PATH
            ):
                raise PhotosError("schema_changed", log_detail="upload URL outside the upload endpoint")
            granularity = start.headers.get("x-goog-upload-chunk-granularity", "")
            step = (
                int(granularity)
                if granularity.isdigit() and 0 < int(granularity) <= upload.CHUNK
                else upload.GRANULARITY
            )
            chunk = max(step, upload.CHUNK // step * step)
            offset = 0
            while True:
                piece = source.read(min(chunk, size - offset))
                if not piece:
                    raise PhotosError("invalid_argument", "the file ended before its size")
                last = offset + len(piece) >= size
                answer = self._upload_post(
                    ctx,
                    target,
                    extra=(
                        *common,
                        ("x-goog-upload-offset", str(offset)),
                        ("x-goog-upload-command", "upload, finalize" if last else "upload"),
                    ),
                    data=piece,
                    content_type=CHUNK_TYPE,
                    retries=0,
                )
                if answer.headers.get("x-goog-upload-status") != ("final" if last else "active"):
                    raise PhotosError("schema_changed", log_detail="upload chunk: unexpected upload status")
                offset += len(piece)
                if last:
                    token = answer.body.decode("ascii", "replace").strip()
                    if not re.fullmatch(r"[A-Za-z0-9+/=_-]{16,8192}", token):
                        raise PhotosError("schema_changed", log_detail="upload finished without a token")
                    return token

    def fetch_image(self, url: str) -> bytes:
        """GET one thumbnail on the image CDN. Item-level failures don't affect the session."""
        if not host_allowed(url) or not IMAGE_HOSTS.match((urlsplit(url).hostname or "").lower()):
            raise PhotosError("item_unavailable", log_detail="thumbnail URL outside the image allowlist")
        with self.image_pacer.slot():
            ctx = self._context()
            self.state.take("thumb")
            response, final_host = self._get(ctx, url, kind="image", max_bytes=IMAGE_MAX_BYTES)
            status = response.status
            if status == 200:
                if response.content_type == "text/html" or response.body[:15].lstrip().lower().startswith(
                    (b"<!doctype", b"<html")
                ):
                    if _CAPTCHA.search(response.body[:65536]):
                        raise self._fail(ctx, "blocked", "captcha page")
                    raise PhotosError("media_session_required", log_detail=f"HTML from {final_host}")
                return response.body
            if status == 401:
                raise PhotosError("media_session_required", log_detail=f"401 from {final_host}")
            if status in (403, 404, 410):
                raise PhotosError("item_unavailable", log_detail=f"{status} from {final_host}")
            raise PhotosError("upstream_error", log_detail=f"{status} from {final_host}")

    def probe_page(self, *, with_cookies: bool) -> HttpResponse:
        """One GET of the app page, redirects not followed, for ``photos_mcp.capture``.

        ``with_cookies=False`` sends no cookie at all: Google's real signed-out answer at zero
        account risk. The caller must not keep the body of a signed-in answer (it holds
        tokens). Budgeted, paced and host-checked like any request; never classifies.
        """
        with self.pacer.slot():
            ctx = self._context()
            self.state.take("rpc")
            response, _ = self._get(
                ctx, PHOTOS_URL, kind="page", max_bytes=RPC_MAX_BYTES, follow=False, cookies=with_cookies
            )
            return response

    def current_snapshot(self) -> Snapshot | None:
        """The loaded snapshot without touching Google (status tool)."""
        try:
            return self.reader.current()
        except PhotosError:
            return None

    # -- session ------------------------------------------------------------------------

    def _context(self) -> _Context:
        """Refuse before any network use when the breaker is open, the snapshot is missing,
        belongs to another account, or its generation already died."""
        self.state.check_breaker()
        if self.expected_email is None:
            raise PhotosError("not_signed_in", log_detail="no Google account bound to this account yet")
        snapshot = self.reader.current()
        if snapshot.email != self.expected_email:
            raise PhotosError("account_mismatch", log_detail="snapshot email differs from the expected account")
        dead = self.state.dead_code(snapshot.generation)
        if dead:
            raise PhotosError(dead, log_detail=f"generation {snapshot.generation} already rejected")
        with self._ctx_lock:
            if self._ctx is None or self._ctx.snapshot.generation != snapshot.generation:
                self._ctx = _Context(snapshot, self._sender_factory(snapshot), snapshot.cookies.copy())
            return self._ctx

    def _ensure_globals(self, ctx: _Context) -> None:
        """GET photos.google.com once per generation (or after a stale token) for WIZ_global_data."""
        if ctx.client is not None:
            return
        self.state.take("rpc")
        response, _ = self._get(ctx, PHOTOS_URL, kind="page", max_bytes=RPC_MAX_BYTES)
        if response.status == 401:
            # Not proof of a signed-out session: report it, keep the generation (README "Liveness").
            raise PhotosError("session_expired", log_detail="401 on the app page")
        if response.status != 200:
            raise PhotosError("upstream_error", log_detail=f"app page HTTP {response.status}")
        if _CAPTCHA.search(response.body[:65536]) and b"WIZ_global_data" not in response.body:
            raise self._fail(ctx, "blocked", "captcha on the app page")
        globals_ = parse_main_page(response.body.decode("utf-8", "replace"))
        if globals_ is None:
            # Only a redirect to sign-in or /photos/about proves a signed-out session; a 200
            # page without _gd is an error page until it does (README "Liveness").
            raise PhotosError("upstream_error", log_detail="_gd missing on the app page")
        email = str(globals_.get("oPEP7c") or "").strip().lower()
        if email != self.expected_email:
            raise self._fail(ctx, "account_mismatch", "oPEP7c differs from the expected account")
        if not all(isinstance(globals_.get(k), str) and globals_.get(k) for k in ("FdrFJe", "cfb2h", "SNlM0e")):
            raise PhotosError("schema_changed", log_detail="app page globals lack FdrFJe/cfb2h/SNlM0e")
        ctx.client = Client(globals_)

    def _fail(self, ctx: _Context, code: str, why: str) -> PhotosError:
        """Session-level consequences of a classified failure."""
        if code == "blocked":
            self.state.trip_breaker()
        elif code in ("session_expired", "account_mismatch"):
            # Proof only (README "Liveness"): a signed-out redirect or another account. A consent
            # screen is reported and retried; it does not end the generation.
            self.state.mark_dead(ctx.snapshot.generation, code)
            with self._ctx_lock:
                if self._ctx is ctx:
                    self._ctx = None
            if self._on_dead is not None:
                try:
                    self._on_dead(ctx.snapshot.generation, code)
                except Exception as exc:  # a notification; never the call's outcome
                    log.warning("dead-generation notice failed (%s)", type(exc).__name__)
        return PhotosError(code, log_detail=why)

    # -- HTTP ---------------------------------------------------------------------------

    def _post(
        self, ctx: _Context, payloads: Sequence[Payload], *, retries: int = MAX_RETRIES, unknown: str = "upstream_error"
    ) -> HttpResponse:
        assert ctx.client is not None
        self.state.take("rpc", len(payloads))
        url, params, body = ctx.client.build_request(payloads)
        rpcids = ",".join(p.rpcid for p in payloads)
        response = self._send(
            ctx, "POST", url, params=params, data=body, kind="rpc", label=rpcids, retries=retries, unknown=unknown
        )
        if 300 <= response.status < 400:
            location = urljoin(url, response.headers.get("location", ""))
            code = _redirect_code(location)
            if code is None:
                raise PhotosError("upstream_error", log_detail="batchexecute redirected inside Google")
            raise self._fail(ctx, code, "batchexecute redirected")
        if response.status == 401:
            raise PhotosError("session_expired", log_detail="batchexecute 401")
        if response.status == 429:
            raise self._fail(ctx, "blocked", "batchexecute 429")
        return response

    def _upload_post(
        self,
        ctx: _Context,
        url: str,
        *,
        extra: Headers,
        data: bytes,
        content_type: str,
        retries: int,
        params: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """One request to the upload endpoint, classified: anything but a 200 raises."""
        response = self._send(
            ctx, "POST", url, kind="upload", label="upload", params=params, data=data, extra=extra,
            content_type=content_type, retries=retries,
        )  # fmt: skip
        if 300 <= response.status < 400:
            code = _redirect_code(urljoin(url, response.headers.get("location", "")))
            if code is None:
                raise PhotosError("upstream_error", log_detail="upload redirected inside Google")
            raise self._fail(ctx, code, "upload redirected")
        if response.status == 401:
            raise PhotosError("session_expired", log_detail="upload 401")
        if response.status == 429:
            raise self._fail(ctx, "blocked", "upload 429")
        if response.status == 413:
            raise PhotosError("invalid_argument", "Google takes no file this large")
        if response.status in (400, 403, 404):
            raise PhotosError("refused", log_detail=f"upload HTTP {response.status}")
        if response.status != 200:
            raise PhotosError("upstream_error", log_detail=f"upload HTTP {response.status}")
        return response

    def _get(
        self, ctx: _Context, url: str, *, kind: str, max_bytes: int, follow: bool = True, cookies: bool = True
    ) -> tuple[HttpResponse, str]:
        """GET with manual redirects inside the allowlist. Returns (response, final host).
        ``follow=False`` returns the first answer, a redirect included, unclassified."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = (urlsplit(current).hostname or "").lower()
            response = self._send(ctx, "GET", current, kind=kind, label=kind, max_bytes=max_bytes, cookies=cookies)
            if response.status == 429:
                raise self._fail(ctx, "blocked", f"429 from {host}")
            if not follow or not 300 <= response.status < 400:
                return response, host
            target = urljoin(current, response.headers.get("location", ""))
            code = _redirect_code(target)
            if code in ("session_expired", "consent_required") and kind == "image":
                # An image is one item: it never decides the session's fate.
                raise PhotosError("media_session_required", log_detail="image redirected to sign-in or consent")
            if code is not None:
                raise self._fail(ctx, code, f"{kind} redirected")
            if not host_allowed(target):
                raise PhotosError(
                    "item_unavailable" if kind == "image" else "upstream_error",
                    log_detail=f"redirect to a host outside the allowlist from {host}",
                )
            current = target
        raise PhotosError("upstream_error", log_detail="too many redirects")

    def _send(
        self,
        ctx: _Context,
        method: str,
        url: str,
        *,
        kind: str,
        label: str,
        params: Mapping[str, str] | None = None,
        data: str | bytes | None = None,
        max_bytes: int = RPC_MAX_BYTES,
        cookies: bool = True,
        retries: int = MAX_RETRIES,
        unknown: str = "upstream_error",
        extra: Headers = (),
        content_type: str | None = None,
    ) -> HttpResponse:
        """One logical request: host check, pacing, ≤``retries`` retries on 5xx/network, cookie merge. After the
        last attempt a 5xx or network failure is ``unknown`` (``outcome_unknown`` for a change)."""
        if not host_allowed(url):
            raise PhotosError("upstream_error", log_detail="request to a host outside the allowlist")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        pacer = {"image": self.image_pacer, "upload": self.upload_pacer}.get(kind, self.pacer)
        cookie: str | None = None
        if cookies:
            with self._cookie_lock:
                cookie = ctx.cookies.header_for(host, parts.path or "/", self._clock())
        headers = headers_for(ctx.snapshot, kind, method, cookie, extra=extra, content_type=content_type)
        attempt = 0
        while True:
            pacer.space()
            started = time.monotonic()
            try:
                response = ctx.sender.send(method, url, headers=headers, params=params, data=data, max_bytes=max_bytes)
            except BodyTooLarge:
                pacer.done()
                self._event("http", kind=kind, rpcids=label, host=host, status="too_large", ms=_ms(started))
                raise PhotosError(
                    "item_unavailable" if kind == "image" else "schema_changed",
                    log_detail=f"{kind} body over its cap",
                ) from None
            except NetworkError as exc:
                pacer.done()
                self._event("http", kind=kind, rpcids=label, host=host, status=f"network:{exc}", ms=_ms(started))
                if attempt < retries:
                    attempt += 1
                    pacer.backoff(attempt - 1)
                    continue
                raise PhotosError(unknown, log_detail="network error after retries") from None
            pacer.done()
            self._event(
                "http", kind=kind, rpcids=label, host=host, status=response.status, bytes=len(response.body),
                ms=_ms(started),
            )  # fmt: skip
            if response.set_cookies and cookies:
                with self._cookie_lock:
                    ctx.cookies.merge_set_cookie(host, response.set_cookies, self._clock())
            if response.status >= 500 and attempt < retries:
                attempt += 1
                pacer.backoff(attempt - 1)
                continue
            if response.status >= 500:
                raise PhotosError(unknown, log_detail=f"HTTP {response.status} after retries")
            return response

    # -- parsing ------------------------------------------------------------------------

    def _parse_rpc(self, ctx: _Context, response: HttpResponse, payloads: Sequence[Payload]) -> list[RpcResult]:
        rpcids = ",".join(p.rpcid for p in payloads)
        if response.status != 200:
            raise PhotosError("upstream_error", log_detail=f"batchexecute HTTP {response.status}")
        text = response.body.decode("utf-8", "replace")
        looks_html = response.content_type == "text/html" or text.lstrip()[:15].lower().startswith(
            ("<!doctype", "<html")
        )
        if looks_html:
            if _CAPTCHA.search(response.body[:65536]):
                raise self._fail(ctx, "blocked", "captcha instead of an RPC response")
            raise PhotosError("upstream_error", log_detail="HTML instead of an RPC response")
        entries = parse_envelopes(text)
        if not entries or all(entry[0] == "er" for entry in entries):
            raise PhotosError("schema_changed", log_detail=f"no wrb.fr envelope for {rpcids}")
        try:
            responses = parse_api_response(text, payloads)
        except (ValueError, TypeError, IndexError):
            raise PhotosError("schema_changed", log_detail=f"undecodable envelope for {rpcids}") from None
        return [RpcResult(r.rpcid, r.response_id, r.data if r.success else None) for r in responses]

    def _event(self, message: str, **fields: Any) -> None:
        logs.event(log, message, **fields)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
