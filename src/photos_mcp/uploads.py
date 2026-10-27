"""Upload links (README "Uploading"): a page where a person adds photos and videos from their phone or computer, and
the same address takes one file by PUT from a client that has it on disk (an image an assistant made, for example).

``photos_upload_link`` makes a link for the CALLER's account: ``PHOTOS_PUBLIC_URL/uploads/<id>``, an unguessable id
that works for ``LINK_TTL`` and up to ``FILES_PER_LINK`` files, all into one album when the link names one. Each file
goes to Google as it arrives (``PhotosService.upload``) and the answer says what became of it. Nothing here is behind
the server's bearer: the id is the credential, and all a link can do is add files to that account's library (and its
album). Links live in memory: a restart ends them.

  GET /uploads/{id}   the page
  PUT /uploads/{id}   one file as the body (Content-Length required); X-File-Name (percent-encoded UTF-8) and
                      X-File-Modified (milliseconds since 1970) are optional
"""

from __future__ import annotations

import collections
import html
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import anyio.to_thread
from signin_window.relay import HEADERS, PAGE_HEADERS, Misses
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import accounts
from .client import PhotosService
from .config import Settings
from .errors import PhotosError, invalid
from .upload import SNIFF_BYTES, Gate, Staging, modified_ms, size_text

log = logging.getLogger(__name__)

LINK_TTL = 60 * 60
FILES_PER_LINK = 100
MAX_LINKS = 100  # open links on the server; the oldest goes first
LINKS_PER_HOUR = 30  # per account
GATE_WAIT = 300.0  # a file waits this long for one of the server's upload slots (upload.Gate), before it is read
BODY_IDLE = 60.0  # a body that sends nothing for this long is dropped
WRITE_BATCH = 1024**2  # received bytes go to the staging file in batches this large, off the event loop
NAME_MAX = 1000
LINK_ID = re.compile(r"[A-Za-z0-9_-]{32,64}\Z")

# What the page tells the person when a file did not go in (the agent-facing next_step is about tools).
SIGN_IN = "Google Photos needs you to sign in again: ask your assistant to connect Google Photos, then try again."
PAGE_TEXT = {
    "not_signed_in": SIGN_IN,
    "session_expired": SIGN_IN,
    "session_unavailable": SIGN_IN,
    "consent_required": SIGN_IN,
    "account_mismatch": "This server is connected to a different Google account. Ask the person who runs it.",
    "account_not_connected": "This Google Photos connection was removed. Ask the person who runs the server.",
    "budget_exhausted": "Today's limit for adding photos is reached. Please try again tomorrow.",
    "blocked": "Google asked this server to slow down. Please try again later.",
    "busy": "The server is busy with other uploads. Please try again in a minute.",
    "refused": "Google refused this file.",
    "item_unavailable": "The album is no longer there, so nothing was added. Ask your assistant for a new link.",
    "outcome_unknown": "The connection broke while adding it. Check Google Photos before trying it again.",
}
STATUS = {"invalid_argument": 400, "budget_exhausted": 429, "busy": 503, "blocked": 503, "item_unavailable": 404}


@dataclass
class Link:
    id: str
    account: str | None  # the account it adds to (None: a server of one account)
    # That account as it was when the link was made: one removed and added again (for someone else) is not it.
    identity: tuple[int, str | None] | None
    album: str | None
    album_title: str | None
    email: str | None  # masked, shown on the page
    created: float
    files: int = 0
    busy: bool = False
    revision: tuple[str, int] | None = None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _error(exc: PhotosError) -> JSONResponse:
    text = PAGE_TEXT.get(exc.code)
    if exc.code == "invalid_argument" and exc.detail:
        text = exc.detail[:1].upper() + exc.detail[1:] + "."
    body = {"error": {**exc.payload(), "message": text or "Something went wrong. Please try again in a minute."}}
    return JSONResponse(body, status_code=STATUS.get(exc.code, 502), headers=HEADERS)


class UploadLinks:
    def __init__(
        self,
        settings: Settings,
        service_for: Callable[[str | None], PhotosService],
        gate: Gate,
        *,
        clock=time.time,
        run_for=None,
        revision_for=None,
    ) -> None:
        assert settings.public_url
        self.public_url = settings.public_url.rstrip("/")
        self.max_bytes = settings.upload_max_bytes
        self.directory = settings.upload_dir
        self._service_for = service_for
        self._revision_for = revision_for or (lambda account: None)
        self._run_for = run_for or (lambda account, operation: operation(service_for(account)))
        self._clock = clock
        self._links: dict[str, Link] = {}
        self._made: dict[str | None, collections.deque] = {}
        self._misses = Misses()
        self.gate = gate
        self._lock = threading.Lock()

    # -- links --------------------------------------------------------------------------------------------------

    def create(self, svc: PhotosService, album_id: str | None = None) -> dict[str, Any]:
        """A link that adds to ``svc``'s account (the caller's), into ``album_id`` when given (checked now)."""
        album = svc.album(album_id) if album_id is not None else None
        account = svc.account.id if svc.account is not None else None
        now = self._clock()
        with self._lock:
            made = self._made.setdefault(account, collections.deque())
            while made and now - made[0] > 3600:
                made.popleft()
            if len(made) >= LINKS_PER_HOUR:
                raise PhotosError("upload_limited", log_detail="upload link rate")
            made.append(now)
            self._expire(now)
            while len(self._links) >= MAX_LINKS:
                self._links.pop(min(self._links.values(), key=lambda x: x.created).id)
            link = Link(
                secrets.token_urlsafe(32),
                account,
                _identity(svc),
                album["id"] if album else None,
                album["title"] if album else None,
                accounts.mask_email(svc.account.email if svc.account is not None else None),
                now,
                revision=self._revision_for(account),
            )
            self._links[link.id] = link
        log.info("uploads: a link for account %s", account or "-")
        url = f"{self.public_url}/uploads/{link.id}"
        into = f" and into the album {album['title']!r}" if album and album.get("title") else ""
        return {
            "url": url,
            "expires_at": _iso(now + LINK_TTL),
            "files_max": FILES_PER_LINK,
            "file_max_bytes": self.max_bytes,
            "album": {"id": album["id"], "url": album["url"], "title": album["title"]} if album else None,
            "put_example": f"curl -sS --fail-with-body -T <file> -H 'X-File-Name: <name>' {url}",
            "next_step": (
                "Give the person the url: on that page they pick photos and videos from their phone or computer, and "
                f"each goes straight into their Google Photos library{into}. A client with the file on disk (an image "
                "you made in a code sandbox, say) can PUT it to the same url instead, as put_example shows; the "
                "answer is the new item. The link works for "
                f"{LINK_TTL // 60} minutes and {FILES_PER_LINK} files, each up to {size_text(self.max_bytes)}; "
                "photos_list(source=recently_added) shows what arrived."
            ),
        }

    def _expire(self, now: float) -> None:
        for link in [x for x in self._links.values() if now - x.created > LINK_TTL and not x.busy]:
            self._links.pop(link.id, None)

    def find(self, link_id: str, address: str) -> Link | str:
        """The open link with this id, else "expired", or "limited" once this address asked for too many ids that do
        not exist (a real link always works)."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            link = self._links.get(link_id) if LINK_ID.match(link_id or "") else None
            if (
                link is not None
                and now - link.created <= LINK_TTL
                and self._revision_for(link.account) == link.revision
            ):
                return link
            allowed = self._misses.allowed(address, now)
            self._misses.record(address, now)
            return "expired" if allowed else "limited"

    def _claim(self, link: Link) -> PhotosError | None:
        with self._lock:
            if link.files >= FILES_PER_LINK:
                return invalid(f"this link has taken its {FILES_PER_LINK} files; ask your assistant for a new one")
            if link.busy:
                return PhotosError("busy", log_detail="this link is taking another file")
            link.busy = True
        return None

    def _release(self, link: Link, *, added: bool) -> None:
        with self._lock:
            link.busy = False
            if added:
                link.files += 1

    async def _slot(self) -> bool:
        """One of the server's upload slots, waited for before the body is read (the client's upload just pauses)."""
        waited = 0.0
        while not self.gate.try_acquire():
            if waited >= GATE_WAIT:
                return False
            await anyio.sleep(0.5)
            waited += 0.5
        return True

    # -- one file -----------------------------------------------------------------------------------------------

    async def receive(self, link: Link, request: Request) -> JSONResponse:
        raw = request.headers.get("content-length", "")
        if not raw.isdigit() or int(raw) <= 0:
            return _error(invalid("send the file as the body, with its Content-Length"))
        length = int(raw)
        if length > self.max_bytes:
            return _error(invalid(f"the file is larger than {size_text(self.max_bytes)}"))
        refused = self._claim(link)
        if refused is not None:
            return _error(refused)
        added = slot = False
        svc: PhotosService | None = None
        try:
            slot = await self._slot()
            if not slot:
                raise PhotosError("busy", log_detail="no upload slot")
            svc = await anyio.to_thread.run_sync(self._service_for, link.account)
            if _identity(svc) != link.identity or self._revision_for(link.account) != link.revision:
                raise PhotosError("account_not_connected", log_detail="the link's account was replaced")
            with Staging(self.directory, self.max_bytes) as staging:
                await self._read(request, staging, length)
                now = self._clock()
                name = unquote(request.headers.get("x-file-name") or "")[:NAME_MAX] or None
                staged = staging.finish(name, modified_ms(request.headers.get("x-file-modified"), now), now)

                def publish(current):
                    if _identity(current) != link.identity or self._revision_for(link.account) != link.revision:
                        raise PhotosError("account_not_connected", log_detail="the link's account was replaced")
                    return current.upload(staged, link.album, check_album=False)

                result = await anyio.to_thread.run_sync(lambda: self._run_for(link.account, publish))
            added = True
        except (ClientDisconnect, TimeoutError):
            return JSONResponse({"error": {"code": "invalid_argument"}}, status_code=400, headers=HEADERS)
        except PhotosError as exc:
            log.info("uploads: a file was not added (%s; %s)", exc.code, exc.log_detail or "-")
            if svc is not None:
                svc.record_error(exc.code)
            return _error(exc)
        except Exception as exc:  # never echo exception text
            log.error("uploads: a file crashed (%s)", type(exc).__name__)
            return _error(PhotosError("internal_error"))
        finally:
            if slot:
                self.gate.release()
            self._release(link, added=added)
        result.pop("freshness", None)
        result["files_left"] = max(0, FILES_PER_LINK - link.files)
        log.info("uploads: account %s added a %s (%s)", link.account or "-", result["item"]["type"],
                 "already there" if result["already_in_library"] else "new")  # fmt: skip
        return JSONResponse(result, status_code=201, headers=HEADERS)

    @staticmethod
    async def _read(request: Request, staging: Staging, length: int) -> None:
        """The body into ``staging``: refused once its head shows it is not media, dropped after ``BODY_IDLE`` without
        a byte; disk writes and hashing run in a worker thread, in batches."""
        chunks = request.stream().__aiter__()
        batch = bytearray()
        received = 0
        while True:
            try:
                with anyio.fail_after(BODY_IDLE):
                    chunk = await chunks.__anext__()
            except StopAsyncIteration:
                break
            received += len(chunk)
            if received > length:
                raise invalid("the body is longer than its Content-Length")
            batch += chunk
            if len(batch) >= (WRITE_BATCH if staging.size else SNIFF_BYTES):
                await anyio.to_thread.run_sync(staging.write, bytes(batch))
                batch.clear()
        if batch:
            await anyio.to_thread.run_sync(staging.write, bytes(batch))
        if received != length:
            raise invalid("the body is shorter than its Content-Length")

    # -- HTTP ---------------------------------------------------------------------------------------------------

    def app(self) -> Starlette:
        def address(request: Request) -> str:
            # As the sign-in links: the last X-Forwarded-For entry is the one the reverse proxy in front added.
            return (request.headers.get("x-forwarded-for") or "unknown").split(",")[-1].strip()[:64]

        async def page(request: Request) -> Response:
            link = self.find(request.path_params["id"], address(request))
            if not isinstance(link, Link):
                return HTMLResponse(_page(None), status_code=429 if link == "limited" else 404, headers=PAGE_HEADERS)
            return HTMLResponse(_page(link, self.max_bytes), headers=PAGE_HEADERS)

        async def put(request: Request) -> Response:
            link = self.find(request.path_params["id"], address(request))
            if not isinstance(link, Link):
                body = {"error": {"code": link, "message": "This link has expired. Ask your assistant for a new one."}}
                return JSONResponse(body, status_code=429 if link == "limited" else 404, headers=HEADERS)
            return await self.receive(link, request)

        return Starlette(routes=[Route("/{id}", page, methods=["GET"]), Route("/{id}", put, methods=["PUT"])])


def _identity(svc: PhotosService) -> tuple[int, str | None] | None:
    return (svc.account.created_at, svc.account.email) if svc.account is not None else None


def _fill(template: str, values: dict[str, str]) -> str:
    """Every ``{{NAME}}`` at once, so a value (an album title) is never filled in again."""
    return re.sub(r"\{\{([A-Z]+)\}\}", lambda match: values.get(match.group(1), match.group(0)), template)


def _page(link: Link | None, max_bytes: int = 0) -> str:
    if link is None:
        body = """<main class=msg><h1>This link has expired</h1>
<p>Upload links work for a short time. Ask your assistant for a new one.</p></main>"""
        return _fill(PAGE, {"BODY": body, "SCRIPT": ""})
    body = _fill(
        BODY,
        {
            "WHOSE": f" of <b>{html.escape(link.email)}</b>" if link.email else "",
            "INTO": f", into the album <b>{html.escape(link.album_title)}</b>" if link.album_title else "",
            "MAX": html.escape(size_text(max_bytes)),
            "FILES": str(FILES_PER_LINK - link.files),
        },
    )
    return _fill(PAGE, {"BODY": body, "SCRIPT": SCRIPT})


BODY = """<header><h1>Add to Google Photos</h1>
<p>Pick photos and videos from this phone or computer. Each one goes straight into the Google Photos
library{{WHOSE}}{{INTO}}; a file that is already there is not added twice. Up to {{FILES}} files, each up to
{{MAX}}.</p></header>
<main><label class=pick>Choose photos and videos<input type=file id=files multiple accept="image/*,video/*"></label>
<ul id=list></ul></main>"""

PAGE = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=referrer content=no-referrer>
<title>Add to Google Photos</title>
<style>
:root{color-scheme:light;font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:#141414}
html,body{margin:0;background:#f4f4f4}
header{padding:14px 16px 10px;background:#fff;border-bottom:1px solid #ddd}
h1{font-size:19px;margin:0 0 4px}
header p{margin:0;font-size:13px;color:#4b4b4b;max-width:62em}
main{padding:16px;max-width:40em;margin:0 auto}
.msg{margin:auto;padding:28px 20px;text-align:center;max-width:26em}
.pick{display:block;text-align:center;font-weight:600;padding:14px 18px;border-radius:8px;background:#141414;
 color:#fff;cursor:pointer}
.pick input{display:none}
ul{list-style:none;padding:0;margin:16px 0 0}
li{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 12px;margin-bottom:8px;font-size:14px}
li .name{font-weight:600;overflow-wrap:anywhere}
li .state{color:#4b4b4b;font-size:13px;margin-top:2px}
li.ok .state{color:#1a7f37}li.err .state{color:#b42318}
progress{width:100%;height:6px;margin-top:6px}
a{color:#0b57d0}
</style>
{{BODY}}
{{SCRIPT}}
</html>"""

SCRIPT = """<script>
const target = location.pathname;
const $ = id => document.getElementById(id);
const esc = t => String(t ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
function row(file) {
  const li = document.createElement("li");
  li.innerHTML = `<div class=name>${esc(file.name)}</div><div class=state>Waiting…</div>`;
  $("list").appendChild(li);
  return li;
}
function send(file, li) {
  return new Promise(done => {
    const state = li.querySelector(".state");
    const bar = document.createElement("progress");
    bar.max = 100; bar.value = 0; li.appendChild(bar);
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", target);
    xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
    if (file.lastModified) xhr.setRequestHeader("X-File-Modified", String(file.lastModified));
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      bar.value = Math.round(100 * e.loaded / e.total);
      state.textContent = e.loaded < e.total ? `Sending… ${bar.value}%` : "Adding to Google Photos…";
    };
    xhr.onload = () => {
      bar.remove();
      let answer = {};
      try { answer = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status === 201 && answer.item) {
        li.className = "ok";
        const what = answer.already_in_library ? "Already in your library" : "Added";
        const album = answer.album && answer.album.error ? " (but not to the album)" : "";
        state.innerHTML = `✓ ${what}${album} · <a href="${esc(answer.item.url)}" target=_blank rel=noopener>Open</a>`;
      } else {
        li.className = "err";
        state.textContent = (answer.error && answer.error.message) || `Not added (error ${xhr.status}).`;
      }
      done();
    };
    xhr.onerror = () => {
      bar.remove(); li.className = "err";
      state.textContent = "The connection broke. Check Google Photos before trying this one again.";
      done();
    };
    xhr.send(file);
  });
}
let queue = Promise.resolve();
$("files").addEventListener("change", e => {
  for (const file of e.target.files) {
    const li = row(file);
    queue = queue.then(() => send(file, li));
  }
  e.target.value = "";
});
</script>"""
