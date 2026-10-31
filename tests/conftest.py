"""Shared test helpers: fixtures, a fake transport for the service, a scripted HTTP sender
for the transport. Nothing here touches the network."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from photos_mcp.cache import ThumbCache
from photos_mcp.client import PhotosService
from photos_mcp.cursors import CursorStore
from photos_mcp.errors import PhotosError
from photos_mcp.session import SessionReader, Snapshot, read_snapshot
from photos_mcp.state import StateStore
from photos_mcp.transport import GoogleTransport, HttpResponse, Pacer, RpcResult


@pytest.fixture(autouse=True)
def no_inherited_connect_identity(monkeypatch):
    # A developer shell may point op at a 1Password Connect server; fixtures use only what they set.
    monkeypatch.delenv("OP_CONNECT_HOST", raising=False)
    monkeypatch.delenv("OP_CONNECT_TOKEN", raising=False)


FIXTURES = Path(__file__).parent / "fixtures"
SESSION_FIXTURE = FIXTURES / "session"
EMAIL = "owner@example.com"
MISSING = object()


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def make_image(width: int, height: int, fmt: str = "PNG", mode: str = "RGB") -> bytes:
    image = Image.new(mode, (width, height), (200, 30, 30, 255) if mode == "RGBA" else (200, 30, 30))
    out = BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


class Clock:
    def __init__(self, now: float = 1_790_000_000.0) -> None:  # 2026-09-21
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


# -- service-level fake ----------------------------------------------------------------


class FakeTransport:
    """Answers RPCs from fixtures (or callables) and image GETs from bytes; records calls."""

    def __init__(self, state: StateStore, responses: dict[str, Any] | None = None, image: bytes | None = None):
        self.state = state
        self.expected_email = EMAIL
        self.responses: dict[str, Any] = responses or {}
        self.calls: list[list[tuple[str, Any]]] = []
        self.gets: list[str] = []
        self.image: bytes | Callable[[str], bytes] = image if image is not None else make_image(640, 480)
        self.snapshot: Snapshot | None = read_snapshot(SESSION_FIXTURE)

    def rpc(self, payloads):
        self.calls.append([(p.rpcid, p.data) for p in payloads])
        if len(payloads) > 1:
            for index, payload in enumerate(payloads, start=1):
                payload.payload_id = str(index)
        else:
            payloads[0].payload_id = "generic"
        results = []
        for payload in payloads:
            answer = self.responses.get(payload.rpcid, MISSING)
            if isinstance(answer, PhotosError):
                raise answer
            if callable(answer):
                answer = answer(payload)
            if answer is MISSING:
                continue
            results.append(RpcResult(payload.rpcid, payload.payload_id, answer))
        return results

    def fetch_image(self, url: str) -> bytes:
        self.gets.append(url)
        if callable(self.image):
            return self.image(url)
        return self.image

    def current_snapshot(self):
        return self.snapshot

    @property
    def rpcids(self) -> list[list[str]]:
        return [[rpcid for rpcid, _ in call] for call in self.calls]


def albums_pages(payload):
    return load("Z5xsfc") if payload.data[0] is None else [[], None]


def all_fixtures() -> dict[str, Any]:
    return {
        "lcxiM": load("lcxiM"),
        "EzkLib": load("EzkLib"),
        "Z5xsfc": albums_pages,
        "snAcKc": load("snAcKc"),
        "EWgK9e": load("EWgK9e"),
        "fDcn4b": load("fDcn4b"),
        "VrseUb": load("VrseUb"),
        "EzwWhf": load("EzwWhf"),
    }


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def state(tmp_path, clock) -> StateStore:
    return StateStore(tmp_path / "state", rpc_budget=3000, thumb_budget=2000, clock=clock)


@pytest.fixture
def transport(state) -> FakeTransport:
    return FakeTransport(state, all_fixtures())


@pytest.fixture
def service(transport, tmp_path) -> PhotosService:
    return PhotosService(transport, ThumbCache(tmp_path / "state" / "cache", 50 * 1024 * 1024), CursorStore())


# -- transport-level fake --------------------------------------------------------------

GLOBALS = {
    "oPEP7c": EMAIL,
    "FdrFJe": "-1234567890",
    "cfb2h": "boq_photosuiserver_20261010.00_p0",
    "SNlM0e": "AFake:at-token",
    "Im6cmf": "/_/PhotosUi",
}


def app_page(globals_: dict[str, Any] | None = None) -> bytes:
    data = json.dumps(globals_ or GLOBALS)
    return (
        '<!doctype html><html><head><script nonce="x">var a=1;</script>'
        f'<script data-id="_gd" nonce="x">window.WIZ_global_data = {data};</script>'
        "</head><body>app</body></html>"
    ).encode()


def rpc_body(*entries: list[Any]) -> bytes:
    """An rt=c batchexecute body: length-prefixed chunks, one JSON array per line."""
    chunk = json.dumps(list(entries))
    tail = json.dumps([["di", 42], ["af.httprm", 41, "-1", 7]])
    return f")]}}'\n\n{len(chunk)}\n{chunk}\n{len(tail)}\n{tail}\n".encode()


def wrb(rpcid: str, data: Any, payload_id: str = "generic") -> list[Any]:
    return ["wrb.fr", rpcid, None if data is None else json.dumps(data), None, None, None, payload_id]


def ok(body: bytes, content_type: str = "application/json; charset=utf-8", **extra: Any) -> HttpResponse:
    return HttpResponse(200, {"content-type": content_type}, body, **extra)


def redirect(location: str, status: int = 302) -> HttpResponse:
    return HttpResponse(status, {"location": location, "content-type": "text/html"}, b"")


def status(code: int, content_type: str = "text/html", body: bytes = b"") -> HttpResponse:
    return HttpResponse(code, {"content-type": content_type}, body)


class FakeSender:
    """Scripted HTTP: a handler per (method, host+path) or a queue of responses."""

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.requests: list[dict[str, Any]] = []

    def send(self, method, url, *, headers, params=None, data=None, max_bytes):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "params": params, "data": data})
        route = self.routes.get((method, f"{parts.hostname}{parts.path}"))
        if route is None:
            route = self.routes.get((method, parts.hostname))
        if route is None:
            raise AssertionError(f"unexpected request {method} {parts.hostname}{parts.path}")
        if isinstance(route, list):
            response = route.pop(0) if len(route) > 1 else route[0]
        elif callable(route):
            response = route(method, url, params, data)
        else:
            response = route
        if isinstance(response, Exception):
            raise response
        if len(response.body) > max_bytes:
            from photos_mcp.transport import BodyTooLarge

            raise BodyTooLarge
        return response

    def hosts(self) -> list[str]:
        from urllib.parse import urlsplit

        return [f"{r['method']} {urlsplit(r['url']).hostname}{urlsplit(r['url']).path}" for r in self.requests]


BATCH = ("POST", "photos.google.com/_/PhotosUi/data/batchexecute")
PAGE = ("GET", "photos.google.com/")


def session_dir(tmp_path: Path, generation: int = 7, email: str = EMAIL) -> Path:
    target = tmp_path / "session"
    target.mkdir(exist_ok=True)
    shutil.copy(SESSION_FIXTURE / "cookies.txt", target / "cookies.txt")
    write_meta(target, generation, email)
    return target


def write_meta(directory: Path, generation: int, email: str = EMAIL) -> None:
    meta = json.loads((SESSION_FIXTURE / "meta.json").read_text())
    meta.update(generation=generation, email=email)
    tmp = directory / "meta.json.tmp"
    tmp.write_text(json.dumps(meta))
    tmp.replace(directory / "meta.json")


def make_transport(tmp_path, routes, clock, *, rpc_budget=3000, thumb_budget=2000, queue_wait=30.0):
    sender = FakeSender(routes)
    state = StateStore(tmp_path / "state", rpc_budget=rpc_budget, thumb_budget=thumb_budget, clock=clock)
    pacer = Pacer(sleep=clock.sleep, monotonic=clock, rand=lambda: 0.5, queue_wait=queue_wait)
    image_pacer = Pacer(
        concurrency=4, min_interval=0.1, jitter=0.0, sleep=clock.sleep, monotonic=clock, queue_wait=queue_wait
    )
    transport = GoogleTransport(
        SessionReader(session_dir(tmp_path)),
        state,
        expected_email=EMAIL,
        sender_factory=lambda snapshot: sender,
        pacer=pacer,
        image_pacer=image_pacer,
        clock=clock,
    )
    return transport, sender
