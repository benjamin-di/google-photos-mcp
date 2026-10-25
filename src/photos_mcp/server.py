"""photos-mcp: FastMCP server over Google Photos libraries: find and look at photos, add photos and videos, move them
to the trash and back, and organise albums.

It reads a Google session snapshot (``PHOTOS_SESSION_DIR``) and serves MCP over streamable
HTTP (bearer required) or stdio. Safety boundary (enforced here, in ``transport`` and by
``tests/test_safety.py``): the only changes are album changes, uploads and the trash, every RPC id and host
is allowlisted, Google calls are paced and budgeted, and tool results never carry exception
text.

Each request acts for one account (README "Accounts"): the default one, or the one the
operator's authenticating proxy names in the trusted account header (only on requests with the
proxy's bearer, the first in ``MCP_BEARER_TOKEN``); an account that is not configured is
refused, never replaced by another.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import functools
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from importlib import resources
from typing import Annotated, Any, Literal, TypeVar

import anyio.to_thread
import mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as ArgumentError
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount

from . import __version__, accounts, fetch, logs
from .accounts import Account
from .auth import PROXY_CLIENT, AuthError, bearer_auth_from_env
from .cache import ThumbCache
from .client import AGENT_INPUT_CODES, PhotosService
from .config import ConfigError, Settings
from .connect import ConnectFlow
from .downloads import DownloadLinks
from .errors import NEXT_STEP, PhotosError, invalid
from .login_client import LoginClient, LoginUnavailable
from .session import SessionReader
from .state import StateStore
from .transport import (
    IMAGE_CONCURRENCY,
    IMAGE_JITTER,
    IMAGE_MIN_INTERVAL,
    UPLOAD_CONCURRENCY,
    CurlSender,
    GoogleTransport,
    Pacer,
)
from .upload import Gate, Staging
from .uploads import UploadLinks

log = logging.getLogger("photos")
T = TypeVar("T")

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
# Download copies read the library but issue a fresh single-use capability each time.
DOWNLOAD = {**READ, "idempotentHint": False}
LOCAL = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
# photos_connect makes a link (state on this server), and changes nothing at Google.
LINK = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
# Album changes: creating one is not idempotent; adding is (Google keeps one copy); removing is destructive.
CREATE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
ADD = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
REMOVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}
# Uploading the same file again adds no copy (Google keeps one); restoring from the trash is the same.
UPLOAD = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
# Inline upload data (base64): at most this many bytes once decoded. Larger files go through an upload link.
DATA_MAX_BYTES = 10 * 1024**2
# photos_upload waits this long for one of the server's upload slots (upload.Gate), then answers busy.
UPLOAD_WAIT = 60.0
Ids50 = Annotated[list[str], Field(min_length=1, max_length=50, description="Item ids (max 50)")]

HTTP_OPTIONS: dict[str, Any] = {
    "stateless_http": True,
    "json_response": True,
    "host_origin_protection": False,
}

Limit = Annotated[int, Field(ge=1, le=50, description="Items per page (1-50)")]
Cursor = Annotated[str | None, Field(max_length=200, description="next_cursor from the previous page")]
Media = Annotated[Literal["all", "photos", "videos"], Field(description="Only photos, only videos, or all (default)")]
Details = Annotated[
    bool,
    Field(description="Also lat, lon and camera per item, and place where missing (limit up to 20; one more request)"),
]
Day = Annotated[str | None, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="A date like 2024-03-18")]

# Photo cards (MCP Apps): claude.ai and ChatGPT draw photos_show with this page in a sandboxed frame. The thumbnails
# ride inline in the result's _meta (data: URLs: Google's image links need the person's Google session), the page
# loads nothing else and opens photos.google.com links through the host.
VIEW_URI = "ui://photos/view.html"
VIEW_META = "photos/view"
VIEW_HTML = (
    (resources.files("photos_mcp") / "view.html").read_text(encoding="utf-8").replace("__VERSION__", __version__)
)


class Pick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(min_length=8, max_length=128, description="An item id from this server")]
    note: Annotated[str | None, Field(max_length=200, description="Your short caption for this photo")] = None


_ERRORS = "\n".join(f"- {code}: {text}" for code, text in NEXT_STEP.items())

INSTRUCTIONS = f"""\
Google Photos for the calling person's own account, through the same private web API
photos.google.com uses: find and look at photos and videos, add photos and videos, move them to
the trash and back, and organise albums. Nothing here empties the trash, archives or edits
photos, or changes Google Photos sharing settings.

Flow: photos_search -> photos_contact_sheet -> photos_view, then photos_show for the person.
1. photos_search(query) uses Google's own search: people (a name the library has
   labelled), places ("Paris"), things ("dog", "receipt", "whiteboard"), text in images,
   dates ("July 2023", "2019"), and combinations ("beach 2022"). photos_list walks the library (newest
   first by taken date; from_date/to_date for an exact range), the archive, one album
   (photos_albums lists albums), the favourites, or what was uploaded most recently (recently_added).
   Both take media=photos|videos. photos_on_this_day gives this day in earlier years.
2. photos_contact_sheet(ids) shows up to 24 candidates as one numbered grid; the legend
   maps each number to its id and date. Use it to pick, instead of viewing one by one.
3. photos_view(ids, max_px) returns up to 4 images (videos: the poster frame).
   photos_get(ids) gives details: place, coordinates, camera, albums, description, source.
4. Presenting: whenever you show the person photos you picked or found, call photos_show with
   them and a short note each: claude.ai and ChatGPT draw a gallery with links to Google Photos.
   Do it without being asked, then add only what the gallery does not show.

Items have one shape everywhere (search, list, album, get), every key present, null when
unknown: {{id, url, type, taken_at, tz_known, filename, description, place, favourite,
archived}}. url opens the item on photos.google.com (the person's own browser signs in):
link with it. taken_at is local time with its UTC offset when tz_known is true; otherwise
the same instant in UTC ("Z") and the local time is unknown. Search results carry no
place (Google's search response has none): pass details=true (adds place, lat, lon and
camera; limit up to 20) or use photos_get. photos_get adds uploaded_at (UTC), lat/lon,
camera, width/height, size_bytes, duration_s, albums, source (how it reached the library:
mobile, web, shared, partnerShared, drive, pc, gmail; android, ios) and original_quality.

Albums: photos_album_create(title, ids) makes an album (with up to 50 items);
photos_album_add(album_id, ids) adds up to 50 items (ids from search or list); photos_album_remove
takes them out again (they stay in the library; its ids are the ones photos_list(source=album)
gives for that album). Each change is read back from the album (added / removed counts) and
counts against a daily write budget (photos_status: budgets).

Adding photos and videos: photos_upload_link(album_id) gives a one-time page where the person
picks photos and videos from their phone or computer; each goes straight into their library (and
the album). Give them the url. The same url takes one file per PUT from a client that has the file
on disk, such as an image you made in a code sandbox (put_example). photos_upload(url=...) adds
the photo or video at a public https link; photos_upload(data=...) one you have as base64 (up to
10 MiB). A file the library already holds is not added twice (already_in_library). Each upload
counts against the write budget; photos_list(source=recently_added) shows what arrived.

Deleting: photos_delete(ids) moves up to 50 photos or videos (ids from search or list) to the
trash, where Google keeps them 60 days; photos_restore(ids) brings them back, and
photos_list(source=trash) lists the trash. photos_album_delete(album_id) deletes one of the
person's own albums (its photos stay in the library; the album cannot be brought back).
photos_delete, photos_album_delete and photos_album_remove take things away: ask the person
before calling them, showing exactly what goes (photos_show for photos, the album's title and
count for an album); their client also asks them to approve each call.

Images for viewing: at most 1568 px on the longest edge. When photos_download is offered,
it makes one-time JPEG download links for transferring photos to another tool: for example,
pass each returned url to WhatsApp send_file(source_url=..., recipient=...). Do not open or
preview the link first: one GET consumes it. Links expire in 10 minutes. Copies default to
2048 px (up to 4096), strip original metadata and are not original files. Videos are refused,
never replaced by a poster. Original files (originals) are not available here.
photos_view's width/height describe the returned JPEG, not the original (photos_get has
the original's dimensions).

Paging: pass next_cursor back as cursor with the same query/source/album. A cursor
lives 1 h. coverage.pagination_complete says whether you saw everything; if
coverage.dedupe_required is set, the cursor was recovered and items may repeat.
Shared and partner libraries are not covered. Archived items: photos_list(source=library)
leaves them out; search, albums and source=archive include them (coverage.archive_included);
each item's archived flag says which are archived (null where Google does not say).

Requests to Google are paced and budgeted per day: keep limits modest, avoid loops.
photos_status shows the session, budgets, breaker and last error without calling Google.
photos_canary is for monitoring: one real check per 55 min (5 min after a transient
failure, or sooner when a new session generation appears), cached in between.

Signing in: when a tool says not_signed_in, photos_connect (where this server offers it) gives a
one-time link where the person signs in to Google themselves; give them the link.

Errors come as {{"error": {{"code", "next_step", "retryable"}}}}:
{_ERRORS}
"""


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _argument_detail(error: PydanticValidationError) -> str:
    """Name each bad argument and the rule it broke; never echo the input value."""
    parts = []
    for item in error.errors()[:3]:
        where = ".".join(str(step) for step in item.get("loc", ()) if step != "arguments") or "arguments"
        parts.append(f"{where}: {item.get('msg', 'invalid')}")
    return "; ".join(parts)[:300] or "invalid arguments"


class TypedArgumentErrors(Middleware):
    """Argument-validation failures (pydantic, before the tool runs) as the typed
    ``invalid_argument`` error every other failure uses, not pydantic's raw text."""

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        try:
            return await call_next(context)
        except ArgumentError as exc:
            cause = exc.__cause__
            detail = _argument_detail(cause) if isinstance(cause, PydanticValidationError) else "invalid arguments"
            error = PhotosError("invalid_argument", detail)
            raise ToolError(_json({"error": error.payload()}), log_level=logging.INFO) from None


class Accounts:
    """The default account's service, and one per further account (``accounts``), built on first use and rebuilt
    when the operator changes the account or its first sign-in binds it. All share the default account's pacers and
    breaker; the default account's budgets are what the further accounts' shares leave."""

    def __init__(
        self,
        default: PhotosService,
        settings: Settings | None = None,
        *,
        sender_factory: Callable[..., Any] = CurlSender,
        login: LoginClient | None = None,
    ) -> None:
        self.default = default
        self.settings = settings  # None: one account only (a server built around one service)
        self.login = login
        self._sender_factory = sender_factory
        transport = getattr(default, "_t", None)
        self.pacer = getattr(transport, "pacer", None) or Pacer()
        self.image_pacer = getattr(transport, "image_pacer", None) or Pacer(
            concurrency=IMAGE_CONCURRENCY, min_interval=IMAGE_MIN_INTERVAL, jitter=IMAGE_JITTER
        )
        self.upload_pacer = getattr(transport, "upload_pacer", None) or Pacer(concurrency=UPLOAD_CONCURRENCY)
        self._others: dict[str, tuple[Account, PhotosService]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(
        cls, settings: Settings, *, login: LoginClient | None = None, sender_factory: Callable[..., Any] = CurlSender
    ) -> Accounts:
        if login is None and settings.login_url and settings.login_token:
            login = LoginClient(settings.login_url, settings.login_token)
        account = accounts.default(settings)
        state = StateStore(
            settings.state_dir,
            rpc_budget=account.rpc_budget,
            thumb_budget=account.thumb_budget,
            budget_tz=settings.budget_tz,
            write_budget=settings.daily_write_budget,
        )
        transport = GoogleTransport(
            SessionReader(settings.session_dir),
            state,
            expected_email=settings.expected_email,
            sender_factory=sender_factory,
        )
        service = PhotosService(
            transport, ThumbCache(settings.cache_dir, settings.cache_max_bytes), account=account, sign_in_links=False
        )
        return cls(service, settings, sender_factory=sender_factory, login=login)

    def get(self, account_id: str | None) -> PhotosService:
        """The service of ``account_id`` (None: the default account). Unknown accounts are account_not_connected."""
        settings = self.settings
        if settings is None:
            if account_id is not None:
                raise PhotosError("account_not_connected", log_detail="a one-account server")
            return self.default
        shares = accounts.shares(settings)
        rest = accounts.default(settings, shares)
        self.default.state.set_budgets(rpc_budget=rest.rpc_budget, thumb_budget=rest.thumb_budget)
        if account_id is None or account_id == settings.default_account:
            return self.default
        try:
            account = accounts.load(settings, account_id)  # account_not_connected when unknown
            if account.email is None and accounts.bind_from_keeper(settings, account):
                log.info("account %s bound to the address its sign-in proved", account_id)
                account = accounts.load(settings, account_id)
        except PhotosError:
            with self._lock:
                self._others.pop(account_id, None)  # removed: a later account of that name starts afresh
            raise
        with self._lock:
            cached = self._others.get(account_id)
            if cached is not None and (cached[0].email, cached[0].created_at) == (account.email, account.created_at):
                cached[1].state.set_budgets(rpc_budget=account.rpc_budget, thumb_budget=account.thumb_budget)
                return cached[1]
            service = self._build(settings, account)
            self._others[account_id] = (account, service)
            return service

    def _build(self, settings: Settings, account: Account) -> PhotosService:
        state = StateStore(
            account.state_dir,
            rpc_budget=account.rpc_budget,
            thumb_budget=account.thumb_budget,
            budget_tz=settings.budget_tz,
            breaker=self.default.state,
            write_budget=settings.daily_write_budget,
        )
        transport = GoogleTransport(
            SessionReader(account.session_dir, missing_code="not_signed_in"),
            state,
            expected_email=account.email,
            sender_factory=self._sender_factory,
            pacer=self.pacer,
            image_pacer=self.image_pacer,
            upload_pacer=self.upload_pacer,
            on_dead=lambda _generation, _code: self._ask_keeper(account.id),
        )
        cache = ThumbCache(account.state_dir / "cache", settings.cache_max_bytes)
        return PhotosService(transport, cache, account=account, sign_in_links=settings.connect_enabled)

    def _ask_keeper(self, account_id: str) -> None:
        """Google ended a linked account's generation: ask the login service to look at its browser now, so the
        person hears not_signed_in (and gets a link) soon instead of after the next hourly refresh."""
        login = self.login
        if login is None:
            return

        def ask() -> None:
            try:
                login.refresh(account_id)
            except LoginUnavailable:
                log.warning("the login service did not take a refresh for account %s", account_id)

        threading.Thread(target=ask, daemon=True).start()


def caller_account(header: str | None) -> str | None:
    """The account id the trusted header names on this request, or None for the default account.

    Read only on HTTP requests authenticated with the proxy's bearer (the first in ``MCP_BEARER_TOKEN``); there it is
    required, so a request the proxy forgot to name never falls back to the default account. Any other bearer is the
    default account's, and a header with it is refused (over stdio there is no header). A repeated or malformed value
    is refused rather than ignored."""
    if not header:
        return None
    try:
        request = get_http_request()
    except RuntimeError:
        return None  # stdio, or in process
    values = request.headers.getlist(header)
    token = get_access_token()
    if token is None or token.client_id != PROXY_CLIENT:
        if values:
            raise PhotosError("account_not_connected", log_detail="account header without the proxy's bearer")
        return None
    if len(values) != 1 or not accounts.valid_id(values[0]):
        raise PhotosError("account_not_connected", log_detail="missing or malformed account header")
    return values[0]


def _decode(data: str) -> bytes:
    """Base64 (standard or URL-safe, padding optional, a data: URL prefix allowed) to bytes."""
    text = data.strip()
    if text.startswith("data:") and "," in text[:200]:
        text = text.split(",", 1)[1]
    text = "".join(text.split()).replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise invalid("data must be the file as base64") from None


def build_service(settings: Settings) -> PhotosService:
    """The default account's service alone (kept for callers that want one account)."""
    return Accounts.from_settings(settings).default


def create_server(
    service: PhotosService | Accounts | None = None, *, auth: Any = None, login: LoginClient | None = None
) -> FastMCP:
    """Build the FastMCP server. ``service``/``auth``/``login`` are injectable for tests."""
    if isinstance(service, Accounts):
        registry = service
    elif service is not None:
        registry = Accounts(service)
    else:
        registry = Accounts.from_settings(Settings.from_env(), login=login)
    if login is not None:
        registry.login = login  # an injected client wins over the one built from the settings
    settings = registry.settings
    header = settings.trusted_account_header if settings is not None else None
    mcp = FastMCP("photos", instructions=INSTRUCTIONS, version=__version__, auth=auth, mask_error_details=True)
    mcp.add_middleware(logs.ToolCallLog())
    mcp.add_middleware(TypedArgumentErrors())

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        # Liveness only: never reads the session or calls Google.
        return JSONResponse({"status": "healthy"})

    flow: ConnectFlow | None = None
    if settings is not None and settings.connect_enabled and registry.login is not None:
        # Sign-in links (connect.py): pages people open in a browser, so outside the bearer; each needs its link id.
        flow = ConnectFlow(settings, registry.login)
        mcp._additional_http_routes.append(Mount("/connect", app=flow.app()))  # as fastmcp.contrib does
    gate = Gate(UPLOAD_CONCURRENCY)  # uploads at once on this server, by links and photos_upload alike
    links: UploadLinks | None = None
    downloads: DownloadLinks | None = None
    if settings is not None and settings.public_url:
        # Upload links (uploads.py): a page and a PUT address, outside the bearer like the sign-in links.
        links = UploadLinks(settings, registry.get, gate)
        mcp._additional_http_routes.append(Mount("/uploads", app=links.app()))
        downloads = DownloadLinks(settings.public_url, registry.get)
        mcp._additional_http_routes.append(Mount("/downloads", app=downloads.app()))

    async def call(fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn`` (a PhotosService method) for the caller's own account."""
        try:
            svc = registry.get(caller_account(header))
        except PhotosError as exc:
            log.info("tool error %s (%s)", exc.code, exc.log_detail or "-")
            raise ToolError(_json({"error": exc.payload()}), log_level=logging.WARNING) from None
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, svc, *args))
        except PhotosError as exc:
            log.info("tool error %s (%s)", exc.code, exc.log_detail or "-")
            svc.record_error(exc.code)
            # The caller's own mistakes are routine (INFO); Google-side failures are worth a
            # WARNING line. FastMCP and ToolCallLog log at this level, without a traceback.
            level = logging.INFO if exc.code in AGENT_INPUT_CODES else logging.WARNING
            raise ToolError(_json({"error": exc.payload()}), log_level=level) from None
        except Exception as exc:  # never echo exception text to the model
            log.error("tool crashed: %s", type(exc).__name__)
            svc.record_error("internal_error")
            raise ToolError(_json({"error": PhotosError("internal_error").payload()})) from None

    @mcp.tool(annotations=READ)
    async def photos_search(
        query: Annotated[
            str, Field(min_length=1, max_length=200, description="What you'd type into Google Photos search")
        ],
        limit: Limit = 20,
        cursor: Cursor = None,
        media: Media = "all",
        details: Details = False,
    ) -> dict[str, Any]:
        """Search the library with Google Photos' own search (people, places, things, text
        in images, dates; archived items included). Returns items {id, url, type, taken_at,
        tz_known, filename, description, place, favourite, archived}, next_cursor, coverage
        and freshness. place is null here (Google's search response has none) unless
        details=true, which also adds lat, lon and camera."""
        return await call(PhotosService.search, query, limit, cursor, media, details)

    @mcp.tool(annotations=READ)
    async def photos_list(
        source: Annotated[
            Literal["library", "archive", "album", "favourites", "recently_added", "trash"],
            Field(
                description="library (default), archive, album, favourites, recently_added (newest upload first), or "
                "trash (most recently deleted first)"
            ),
        ] = "library",
        album_id: Annotated[
            str | None, Field(max_length=128, description="Album id; required iff source=album")
        ] = None,
        from_date: Annotated[Day, Field(description="library/archive: taken on or after this date")] = None,
        to_date: Annotated[Day, Field(description="library/archive: taken on or before this date")] = None,
        media: Media = "all",
        details: Details = False,
        limit: Limit = 20,
        cursor: Cursor = None,
    ) -> dict[str, Any]:
        """List the library newest-first by taken date (archive excluded; from_date/to_date for
        an exact range of the items' own local dates), the archive, one album, the favourites,
        or what was uploaded most recently. Same item shape as photos_search; place is set
        where Google's listing carries it (details=true adds it, with lat, lon and camera)."""

        def run(svc: PhotosService) -> dict[str, Any]:
            return svc.list(
                source, album_id, limit, cursor, from_date=from_date, to_date=to_date, media=media, details=details
            )

        return await call(run)

    @mcp.tool(annotations=READ)
    async def photos_albums(limit: Limit = 20, cursor: Cursor = None) -> dict[str, Any]:
        """List albums: {id, title, count, shared}. Use an id with photos_list(source="album")."""
        return await call(PhotosService.albums, limit, cursor)

    @mcp.tool(annotations=READ)
    async def photos_get(
        ids: Annotated[list[str], Field(min_length=1, max_length=10, description="Item ids (max 10)")],
    ) -> dict[str, Any]:
        """Details per item, all ids in one Google request: the item shape of photos_search
        plus uploaded_at (UTC), lat/lon, camera, width/height (original), size_bytes,
        duration_s and albums. Failed items carry their own error."""
        return await call(PhotosService.get, ids)

    if downloads is not None:

        @mcp.tool(annotations=DOWNLOAD)
        async def photos_download(
            ids: Annotated[list[str], Field(min_length=1, max_length=10, description="Photo ids (max 10)")],
            max_px: Annotated[int, Field(ge=256, le=4096, description="JPEG longest edge in pixels")] = 2048,
        ) -> dict[str, Any]:
            """Make one-time download links for up to 10 photos, suitable for another tool's
            file import (WhatsApp send_file: source_url). JPEG copies, at most max_px; not originals.
            Returns items {id, url, filename, expires_at, mime_type, size_bytes, width, height, original:false}.
            One GET per link, valid 10 minutes; HEAD does not consume it. Videos return an item error.
            Pass links straight to the importing tool, without opening them first."""
            return await call(lambda svc: downloads.create(svc, ids, max_px))

    @mcp.tool(annotations=READ)
    async def photos_contact_sheet(
        ids: Annotated[list[str], Field(min_length=1, max_length=24, description="Item ids (max 24)")],
    ) -> ToolResult:
        """One JPEG grid (at most 1568 px) of up to 24 items with numbered tiles and a play
        badge on videos, plus a legend mapping each number to its id and date."""
        legend, sheet = await call(PhotosService.contact_sheet, ids)
        return ToolResult(
            content=[
                mcp_types.TextContent(type="text", text=_json(legend)),
                Image(data=sheet, format="jpeg").to_image_content(),
            ]
        )

    @mcp.tool(annotations=READ)
    async def photos_view(
        ids: Annotated[list[str], Field(min_length=1, max_length=4, description="Item ids (max 4)")],
        max_px: Annotated[int, Field(ge=256, le=1568, description="Longest edge in pixels")] = 1024,
    ) -> ToolResult:
        """Look at up to 4 items: per id a label (n, id, taken_at, type, width/height of the
        returned JPEG) followed by the JPEG, at most max_px (≤1568) on the longest edge; no
        originals. A video gives its poster frame (video_poster: true)."""
        views = await call(PhotosService.view, ids, max_px)
        content: list[Any] = []
        for label, jpeg in views:
            content.append(mcp_types.TextContent(type="text", text=_json(label)))
            if jpeg is not None:
                content.append(Image(data=jpeg, format="jpeg").to_image_content())
        return ToolResult(content=content)

    @mcp.tool(annotations=READ)
    async def photos_on_this_day(
        date: Annotated[Day, Field(description="The day to look back from (default: today)")] = None,
        years: Annotated[int, Field(ge=1, le=20, description="How many earlier years to look at")] = 10,
        per_year: Annotated[int, Field(ge=1, le=20, description="At most this many items per year")] = 6,
    ) -> dict[str, Any]:
        """Photos and videos taken on this calendar day in earlier years ("on this day"), newest
        year first: {date, years: [{year, items, more}], years_checked}. more: that day holds more
        than shown (photos_list with from_date=to_date=that day lists them all). One Google
        request per year, so keep years modest."""
        return await call(PhotosService.on_this_day, date, years, per_year)

    @mcp.resource(
        VIEW_URI,
        name="photos-view",
        title="Google Photos cards",
        description="A gallery of photos (thumbnail, date, your note, link to Google Photos) for photos_show.",
        mime_type="text/html;profile=mcp-app",
        app={"prefersBorder": False},
    )
    def photos_view_card() -> str:
        return VIEW_HTML

    @mcp.tool(
        title="Show photos",
        annotations=READ,
        app={"resourceUri": VIEW_URI, "visibility": ["model", "app"]},
    )
    async def photos_show(
        items: Annotated[list[Pick], Field(min_length=1, max_length=12, description="Photos to show, in order")],
        title: Annotated[str | None, Field(max_length=80, description="Heading above the gallery")] = None,
    ) -> ToolResult:
        """Show photos to the person as a gallery: thumbnail, date, your note and a link that opens
        each one in Google Photos (claude.ai and ChatGPT draw it; other clients get the same
        data as JSON). Use it whenever you present photos you picked or found."""

        def run(svc: PhotosService) -> tuple[dict[str, Any], dict[str, Any]]:
            return svc.show([pick.model_dump() for pick in items], title)

        shown, view = await call(run)
        return ToolResult(structured_content=shown, meta={VIEW_META: view})

    @mcp.tool(annotations=CREATE)
    async def photos_album_create(
        title: Annotated[str, Field(min_length=1, max_length=200, description="The new album's title")],
        ids: Annotated[
            list[str] | None, Field(max_length=50, description="Items to put in it (max 50), ids from search or list")
        ] = None,
    ) -> dict[str, Any]:
        """Create an album, with up to 50 items in it. Returns the album {id, url, title, count,
        shared} and how many items it holds. Do not repeat a call that failed with outcome_unknown:
        photos_albums shows whether the album exists."""
        return await call(PhotosService.album_create, title, ids)

    @mcp.tool(annotations=ADD)
    async def photos_album_add(
        album_id: Annotated[str, Field(min_length=8, max_length=128, description="An album id from photos_albums")],
        ids: Ids50,
    ) -> dict[str, Any]:
        """Add up to 50 items (ids from search or list) to an album, also a shared one this account may
        add to. Items already in it stay once. Returns the album as Google shows it afterwards and
        added (from its counts before and after)."""
        return await call(PhotosService.album_add, album_id, ids)

    @mcp.tool(annotations=REMOVE)
    async def photos_album_remove(
        album_id: Annotated[str, Field(min_length=8, max_length=128, description="An album id from photos_albums")],
        ids: Annotated[
            list[str],
            Field(min_length=1, max_length=50, description="Item ids as photos_list(source=album) gives them (max 50)"),
        ],
    ) -> dict[str, Any]:
        """Take up to 50 items out of an album; they stay in the library. Only ids the album's own listing holds are
        removed (not_in_album: the others). Ask the person first, showing which photos leave the album. Returns the
        album afterwards and removed."""
        return await call(PhotosService.album_remove, album_id, ids)

    @mcp.tool(annotations=REMOVE)
    async def photos_album_delete(
        album_id: Annotated[str, Field(min_length=8, max_length=128, description="An album id from photos_albums")],
    ) -> dict[str, Any]:
        """Delete one of the person's own albums; its photos stay in the library, but the album cannot be brought
        back. Ask the person first, naming the album and how many photos it holds. Returns the album and deleted
        (read back: the album no longer opens). Shared albums are deleted or left in the app."""
        return await call(PhotosService.album_delete, album_id)

    @mcp.tool(annotations=REMOVE)
    async def photos_delete(
        ids: Annotated[
            list[str], Field(min_length=1, max_length=50, description="Item ids from search or list (max 50)")
        ],
    ) -> dict[str, Any]:
        """Move up to 50 photos or videos to the trash, where Google keeps them 60 days (photos_restore brings them
        back). Ask the person first, showing them the photos (photos_show). Returns trashed, not_found (ids that are
        not items of this library) and, per id, in_trash (read back from the trash)."""
        return await call(PhotosService.trash, ids)

    @mcp.tool(annotations=UPLOAD)
    async def photos_restore(
        ids: Annotated[
            list[str],
            Field(min_length=1, max_length=50, description="Item ids as photos_list(source=trash) gives them (max 50)"),
        ],
    ) -> dict[str, Any]:
        """Bring up to 50 photos or videos back from the trash into the library. Returns restored, not_in_trash and,
        per id, restored (read back from the trash)."""
        return await call(PhotosService.restore, ids)

    @mcp.tool(annotations=UPLOAD)
    async def photos_upload(
        url: Annotated[
            str | None, Field(max_length=fetch.URL_MAX, description="A public https link to the photo or video")
        ] = None,
        data: Annotated[
            str | None,
            Field(max_length=DATA_MAX_BYTES * 4 // 3 + 128, description="Or the file itself, base64 (up to 10 MiB)"),
        ] = None,
        filename: Annotated[
            str | None, Field(max_length=200, description="Its name in the library (default: from the link or date)")
        ] = None,
        album_id: Annotated[
            str | None, Field(min_length=8, max_length=128, description="Also put it in this album (photos_albums)")
        ] = None,
    ) -> dict[str, Any]:
        """Add one photo or video to the library (and to an album): the file at a public https link (url), or one you
        have as base64 (data). A file the library already holds is not added again (already_in_library). Returns the
        item {id, url, type, filename, taken_at, tz_known, width, height, uploaded_at} and the album afterwards. For the
        person's own files, or a file on disk, use photos_upload_link."""

        def run(svc: PhotosService) -> dict[str, Any]:
            if (url is None) == (data is None):
                raise invalid("give either url or data")
            directory = settings.upload_dir if settings is not None else svc.state.path.parent / "uploads"
            limit = settings.upload_max_bytes if settings is not None else DATA_MAX_BYTES
            if not gate.acquire(UPLOAD_WAIT):
                raise PhotosError("busy", log_detail="no upload slot")
            try:
                with Staging(directory, limit if url is not None else min(limit, DATA_MAX_BYTES)) as staging:
                    name = filename
                    if url is not None:
                        hint = fetch.fetch(url, staging.write)
                        name = filename or hint
                    else:
                        staging.write(_decode(data or ""))
                    now = time.time()
                    return svc.upload(staging.finish(name, None, now), album_id)
            finally:
                gate.release()

        return await call(run)

    if links is not None:
        upload_links = links

        @mcp.tool(annotations=LINK)
        async def photos_upload_link(
            album_id: Annotated[
                str | None, Field(min_length=8, max_length=128, description="Put every file in this album too")
            ] = None,
        ) -> dict[str, Any]:
            """A one-time page where the person adds photos and videos from their phone or computer straight into
            their own Google Photos library (and the album): for the calling person's account only, 60 minutes and
            100 files. The same url takes one file per PUT from a client with the file on disk (put_example), answering
            with the new item. Give the person the url."""
            return await call(lambda svc: upload_links.create(svc, album_id))

    @mcp.tool(annotations=LOCAL)
    async def photos_status() -> dict[str, Any]:
        """Session account and generation, snapshot age, breaker, daily budgets left, cache
        size and the last error. Never calls Google."""
        return await call(PhotosService.status)

    @mcp.tool(annotations=READ)
    async def photos_canary() -> dict[str, Any]:
        """Monitoring: one real check of the path agents use (a library page of 1 item and
        its thumbnail); the verdict is cached 55 min (transient failures: 5 min) and calls in
        between return it (cached: true). {ok, code, checked_at, cached, snapshot_age_s,
        generation}; ok is false on any error code, or with code snapshot_stale when the
        snapshot is older than 3 h or dated in the future. Never fails."""
        return await call(PhotosService.canary)

    if flow is not None:
        connect_flow = flow

        def make_link(svc: PhotosService) -> dict[str, Any]:
            signed_in = svc.status()["session"]["state"] == "ok"
            return connect_flow.create(svc.account, signed_in=signed_in)

        @mcp.tool(annotations=LINK)
        async def photos_connect() -> dict[str, Any]:
            """A one-time link where the person signs in to their own Google account (on Google's own page) so this
            server can show them their photos: for the calling person's account only, valid for 20 minutes and one
            sign-in; a newer link replaces it. Give the person the url and nothing else. Use it when a tool says
            not_signed_in, or when the person asks to connect Google Photos."""
            return await call(make_link)

    return mcp


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["admin"]:
        from . import admin

        raise SystemExit(admin.main(argv[1:]))
    parser = argparse.ArgumentParser(prog="photos-mcp", description="MCP server for Google Photos libraries.")
    parser.add_argument(
        "--transport",
        choices=("http", "stdio"),
        default="http",
        help="http: streamable HTTP on /mcp, bearer required (default); stdio: for a local MCP client",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="HTTP listen address (default 127.0.0.1; the image uses 0.0.0.0)"
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    args = parser.parse_args(argv)
    logs.setup("photos")
    try:
        if args.transport == "stdio":
            create_server().run(transport="stdio", show_banner=False)
            return
        # Fail closed: /mcp answers 401 without a configured bearer.
        auth = bearer_auth_from_env()
        server = create_server(auth=auth)
    except (AuthError, ConfigError) as exc:
        # Our own startup message (configuration), never a tool result.
        print("photos-mcp:", *exc.args, file=sys.stderr)
        raise SystemExit(1) from None
    server.run(transport="streamable-http", host=args.host, port=args.port, show_banner=False, **HTTP_OPTIONS)


if __name__ == "__main__":
    main()
