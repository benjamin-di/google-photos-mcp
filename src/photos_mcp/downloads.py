"""Short-lived, single-download JPEG copies for clients that import files by URL.

The token is the credential; no session, cookies or Google CDN URL leaves this server.
Copies live in bounded memory and a restart revokes them. HEAD does not consume a link.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .client import PhotosService, check_ids
from .errors import PhotosError, invalid
from .uploads import _identity

TTL = 600
MAX_LINKS = 128
MAX_BYTES = 64 * 1024**2
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


@dataclass(frozen=True)
class Copy:
    account: str | None
    identity: tuple[int, str | None] | None
    jpeg: bytes
    filename: str
    expires: float


class DownloadLinks:
    def __init__(self, public_url: str, service_for: Callable[[str | None], PhotosService], *, clock=time.time):
        self.public_url = public_url.rstrip("/")
        self.service_for = service_for
        self.clock = clock
        self.links: dict[str, Copy] = {}
        self.lock = threading.Lock()

    def create(self, svc: PhotosService, ids: list[str], max_px: int) -> dict[str, Any]:
        keys = check_ids(ids, 10)
        if isinstance(max_px, bool) or not isinstance(max_px, int) or not 256 <= max_px <= 4096:
            raise invalid("max_px must be between 256 and 4096")
        items = []
        for key in keys:
            try:
                # Resolve against the caller's current library, including for a cached image.
                meta = svc._resolve(key)
                if meta.is_video:
                    raise invalid("photos_download exports photos only; a video poster is not a video download")
                jpeg, info, _ = svc._thumbnail(key, max_px)
                now = self.clock()
                filename = f"photo-{len(items) + 1}.jpg"
                copy = Copy(svc.account.id if svc.account else None, _identity(svc), jpeg, filename, now + TTL)
                token = secrets.token_urlsafe(32)
                with self.lock:
                    self.links = {t: c for t, c in self.links.items() if c.expires > now}
                    if (
                        len(self.links) >= MAX_LINKS
                        or sum(len(c.jpeg) for c in self.links.values()) + len(jpeg) > MAX_BYTES
                    ):
                        raise PhotosError("busy", log_detail="download copies full")
                    self.links[token] = copy
                items.append(
                    {
                        "id": key,
                        "url": f"{self.public_url}/downloads/{token}",
                        "filename": filename,
                        "expires_at": datetime.fromtimestamp(copy.expires, UTC).isoformat(timespec="seconds"),
                        "mime_type": "image/jpeg",
                        "size_bytes": len(jpeg),
                        "width": info["w"],
                        "height": info["h"],
                        "original": False,
                    }
                )
            except PhotosError as exc:
                # Keep successful copies usable even when another item cannot be exported.
                items.append({"id": key, "error": exc.payload()})
        return {
            "items": items,
            "next_step": (
                "Pass each url directly to a tool that imports files by URL (WhatsApp send_file: source_url). "
                "Do not open or preview it first: one GET consumes it. Links expire in 10 minutes. "
                "These are resized JPEG copies without original metadata, not original files. "
                "If sending fails before delivery, request a fresh link before retrying; "
                "never repeat an uncertain send."
            ),
        }

    async def download(self, request: Request) -> Response:
        token = request.path_params["token"]
        if request.url.query or not TOKEN.fullmatch(token):
            return Response(status_code=404, headers=HEADERS)
        with self.lock:
            copy = self.links.get(token)
            if copy and (request.method == "GET" or copy.expires <= self.clock()):
                self.links.pop(token, None)
        if copy is None or copy.expires <= self.clock():
            return Response(status_code=404, headers=HEADERS)
        try:
            svc = self.service_for(copy.account)
            if _identity(svc) != copy.identity:
                return Response(status_code=404, headers=HEADERS)
        except PhotosError:
            return Response(status_code=404, headers=HEADERS)
        return Response(
            b"" if request.method == "HEAD" else copy.jpeg,
            media_type="image/jpeg",
            headers={
                **HEADERS,
                "Content-Disposition": f'attachment; filename="{copy.filename}"',
                "Content-Length": str(len(copy.jpeg)),
            },
        )

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/{token}", self.download, methods=["GET", "HEAD"])])
