"""photos-mcp: FastMCP server over Google Photos libraries, read-only.

It reads a Google session snapshot (``PHOTOS_SESSION_DIR``) and serves MCP over streamable
HTTP (bearer required) or stdio. Safety boundary (enforced here, in ``transport`` and by
``tests/test_safety.py``): no write RPC exists in this code base, every RPC id and host is
allowlisted, Google calls are paced and budgeted, and tool results never carry exception
text.

Each request acts for one account (README "Accounts"): the default one, or the one the
operator's authenticating proxy names in the trusted account header (only on requests with the
proxy's bearer, the first in ``MCP_BEARER_TOKEN``); an account that is not configured is
refused, never replaced by another.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
import threading
from collections.abc import Callable
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
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount

from . import __version__, accounts, logs
from .accounts import Account
from .auth import PROXY_CLIENT, AuthError, bearer_auth_from_env
from .cache import ThumbCache
from .client import AGENT_INPUT_CODES, PhotosService
from .config import ConfigError, Settings
from .connect import ConnectFlow
from .errors import NEXT_STEP, PhotosError
from .login_client import LoginClient, LoginUnavailable
from .session import SessionReader
from .state import StateStore
from .transport import IMAGE_CONCURRENCY, IMAGE_JITTER, IMAGE_MIN_INTERVAL, CurlSender, GoogleTransport, Pacer

log = logging.getLogger("photos")
T = TypeVar("T")

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
LOCAL = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
# photos_connect makes a link (state on this server), and changes nothing at Google.
LINK = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}

HTTP_OPTIONS: dict[str, Any] = {
    "stateless_http": True,
    "json_response": True,
    "host_origin_protection": False,
}

Limit = Annotated[int, Field(ge=1, le=50, description="Items per page (1-50)")]
Cursor = Annotated[str | None, Field(max_length=200, description="next_cursor from the previous page")]

_ERRORS = "\n".join(f"- {code}: {text}" for code, text in NEXT_STEP.items())

INSTRUCTIONS = f"""\
Read-only access to one Google Photos library (the calling person's own account), through the
same private web API photos.google.com uses. Nothing here can change the library.

Flow: photos_search -> photos_contact_sheet -> photos_view.
1. photos_search(query) uses Google's own search: people (a name the library has
   labelled), places ("Paris"), things ("dog", "receipt", "whiteboard"), text in images,
   dates ("July 2023", "2019"), and combinations ("beach 2022"). photos_list walks the library (newest
   first by taken date), the archive, or one album (photos_albums lists albums).
2. photos_contact_sheet(ids) shows up to 24 candidates as one numbered grid; the legend
   maps each number to its id and date. Use it to pick, instead of viewing one by one.
3. photos_view(ids, max_px) returns up to 4 images (videos: the poster frame).
   photos_get(ids) gives details: place, coordinates, camera, albums, description.

Items have one shape everywhere (search, list, album, get), every key present, null when
unknown: {{id, type, taken_at, tz_known, filename, description, place, favourite,
archived}}. taken_at is local time with its UTC offset when tz_known is true; otherwise
the same instant in UTC ("Z") and the local time is unknown. Search results carry no
place (Google's search response has none): use photos_get for it. photos_get adds
uploaded_at (UTC), lat/lon, camera, width/height, size_bytes, duration_s and albums.

Images: at most 1568 px on the longest edge; originals and downloads are not available.
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
        )
        transport = GoogleTransport(
            SessionReader(account.session_dir, missing_code="not_signed_in"),
            state,
            expected_email=account.email,
            sender_factory=self._sender_factory,
            pacer=self.pacer,
            image_pacer=self.image_pacer,
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
    ) -> dict[str, Any]:
        """Search the library with Google Photos' own search (people, places, things, text
        in images, dates; archived items included). Returns items {id, type, taken_at,
        tz_known, filename, description, place, favourite, archived}, next_cursor, coverage
        and freshness. place is always null here: Google's search response has no place
        (photos_get has it)."""
        return await call(PhotosService.search, query, limit, cursor)

    @mcp.tool(annotations=READ)
    async def photos_list(
        source: Annotated[Literal["library", "archive", "album"], "library (default), archive, or album"] = "library",
        album_id: Annotated[
            str | None, Field(max_length=128, description="Album id; required iff source=album")
        ] = None,
        limit: Limit = 20,
        cursor: Cursor = None,
    ) -> dict[str, Any]:
        """List the library newest-first by taken date (archive excluded), the archive, or
        one album (archived items included). Same item shape as photos_search; place is set
        where Google's listing carries it."""
        return await call(PhotosService.list, source, album_id, limit, cursor)

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
    parser = argparse.ArgumentParser(
        prog="photos-mcp", description="Read-only MCP server for one Google Photos library."
    )
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
