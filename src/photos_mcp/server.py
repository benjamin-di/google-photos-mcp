"""photos-mcp: FastMCP server over one Google Photos library, read-only.

It reads a Google session snapshot (``PHOTOS_SESSION_DIR``) and serves MCP over streamable
HTTP (bearer required) or stdio. Safety boundary (enforced here, in ``transport`` and by
``tests/test_safety.py``): no write RPC exists in this code base, every RPC id and host is
allowlisted, Google calls are paced and budgeted, and tool results never carry exception
text.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

import anyio.to_thread
import mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as ArgumentError
from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, logs
from .auth import AuthError, bearer_auth_from_env
from .cache import ThumbCache
from .client import AGENT_INPUT_CODES, PhotosService
from .config import ConfigError, Settings
from .errors import NEXT_STEP, PhotosError
from .session import SessionReader
from .state import StateStore
from .transport import GoogleTransport

log = logging.getLogger("photos")
T = TypeVar("T")

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
LOCAL = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

HTTP_OPTIONS: dict[str, Any] = {
    "stateless_http": True,
    "json_response": True,
    "host_origin_protection": False,
}

Limit = Annotated[int, Field(ge=1, le=50, description="Items per page (1-50)")]
Cursor = Annotated[str | None, Field(max_length=200, description="next_cursor from the previous page")]

_ERRORS = "\n".join(f"- {code}: {text}" for code, text in NEXT_STEP.items())

INSTRUCTIONS = f"""\
Read-only access to one Google Photos library (one account), through the same private
web API photos.google.com uses. Nothing here can change the library.

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


def build_service(settings: Settings) -> PhotosService:
    state = StateStore(
        settings.state_dir,
        rpc_budget=settings.daily_rpc_budget,
        thumb_budget=settings.daily_thumb_budget,
        budget_tz=settings.budget_tz,
    )
    transport = GoogleTransport(SessionReader(settings.session_dir), state, expected_email=settings.expected_email)
    return PhotosService(transport, ThumbCache(settings.cache_dir, settings.cache_max_bytes))


def create_server(service: PhotosService | None = None, *, auth: Any = None) -> FastMCP:
    """Build the FastMCP server. ``service``/``auth`` are injectable for tests."""
    svc = service or build_service(Settings.from_env())
    mcp = FastMCP("photos", instructions=INSTRUCTIONS, version=__version__, auth=auth, mask_error_details=True)
    mcp.add_middleware(logs.ToolCallLog())
    mcp.add_middleware(TypedArgumentErrors())

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        # Liveness only: never reads the session or calls Google.
        return JSONResponse({"status": "healthy"})

    async def call(fn: Callable[..., T], *args: Any) -> T:
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args))
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
        return await call(svc.search, query, limit, cursor)

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
        return await call(svc.list, source, album_id, limit, cursor)

    @mcp.tool(annotations=READ)
    async def photos_albums(limit: Limit = 20, cursor: Cursor = None) -> dict[str, Any]:
        """List albums: {id, title, count, shared}. Use an id with photos_list(source="album")."""
        return await call(svc.albums, limit, cursor)

    @mcp.tool(annotations=READ)
    async def photos_get(
        ids: Annotated[list[str], Field(min_length=1, max_length=10, description="Item ids (max 10)")],
    ) -> dict[str, Any]:
        """Details per item, all ids in one Google request: the item shape of photos_search
        plus uploaded_at (UTC), lat/lon, camera, width/height (original), size_bytes,
        duration_s and albums. Failed items carry their own error."""
        return await call(svc.get, ids)

    @mcp.tool(annotations=READ)
    async def photos_contact_sheet(
        ids: Annotated[list[str], Field(min_length=1, max_length=24, description="Item ids (max 24)")],
    ) -> ToolResult:
        """One JPEG grid (at most 1568 px) of up to 24 items with numbered tiles and a play
        badge on videos, plus a legend mapping each number to its id and date."""
        legend, sheet = await call(svc.contact_sheet, ids)
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
        views = await call(svc.view, ids, max_px)
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
        return await call(svc.status)

    @mcp.tool(annotations=READ)
    async def photos_canary() -> dict[str, Any]:
        """Monitoring: one real check of the path agents use (a library page of 1 item and
        its thumbnail); the verdict is cached 55 min (transient failures: 5 min) and calls in
        between return it (cached: true). {ok, code, checked_at, cached, snapshot_age_s,
        generation}; ok is false on any error code, or with code snapshot_stale when the
        snapshot is older than 3 h or dated in the future. Never fails."""
        return await call(svc.canary)

    return mcp


def main(argv: list[str] | None = None) -> None:
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
