"""Uploads: the web app's protocol as recorded (hash check, resumable upload, mdpdU), the transport sending bytes
only to the upload endpoint and never treating a failure there as a change, the service adding to the library (and an
album) without sending a file it already holds, the guarded link fetch, and upload links (a page and a PUT address)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from typing import Any

import pytest
from conftest import BATCH, PAGE, Clock, app_page, make_image, make_transport, ok, rpc_body, status, wrb
from starlette.testclient import TestClient
from test_accounts import HEADER, PROXY, make_settings, write_meta_default
from test_albums import ALBUM, SHARED, Albums, audit

from photos_mcp import fetch, server, upload
from photos_mcp.auth import bearer_auth_from_env
from photos_mcp.cache import ThumbCache
from photos_mcp.client import PhotosService
from photos_mcp.cursors import CursorStore
from photos_mcp.errors import PhotosError
from photos_mcp.transport import HttpResponse, NetworkError, RpcResult

PNG = make_image(40, 30, "PNG")
JPEG = make_image(40, 30, "JPEG")
NEW = "AF1QipFAKEUPLOADED" + "0" * 26
UPLOAD = ("POST", "photos.google.com/_/upload/uploadmedia/interactive")
SESSION_URL = (
    "https://photos.google.com/_/upload/uploadmedia/interactive?authuser=0&upload_id=fake-id&upload_protocol=resumable"
)


def b64sha1(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


def stored(media_key: str, tag: str = "fake-token") -> list:
    return [tag, [media_key, ["https://photos.fife.usercontent.google.com/pw/FAKE", 40, 30], 1_792_000_000_000,
                  "fake-dedup", 28_800_000, 1_792_000_100_000, None, None, 2], 0]  # fmt: skip


def staged(tmp_path, data: bytes = PNG, name: str | None = "pic.png") -> upload.Staged:
    staging = upload.Staging(tmp_path / "uploads", 10 * 1024**2)
    staging.write(data)
    return staging.finish(name, 1_700_000_000_000, 1_792_000_000.0)


# -- the protocol's pure parts ------------------------------------------------------------------------------------


def test_the_start_body_is_the_recorded_protobuf_with_the_size():
    # Recorded from the web app for a 468-byte file: fields 1-4 and 8, and field 7 the size as a varint.
    assert upload.start_body(468) == b"\x08\x02\x10\x01\x18\x01 \x018\xd4\x03@\x01"
    assert upload.start_body(1) == b"\x08\x02\x10\x01\x18\x01 \x018\x01@\x01"
    assert upload.start_body(2**31) == b"\x08\x02\x10\x01\x18\x01 \x018\x80\x80\x80\x80\x08@\x01"


def test_commit_carries_token_name_and_time_as_the_web_app_does():
    payload = upload.CommitUpload("fake-token", "pic.png", 1_792_711_406_000)
    assert payload.rpcid == "mdpdU" and payload.data == [[["fake-token", "pic.png", 1_792_711_406_000, 2, 2]]]


@pytest.mark.parametrize(
    ("head", "media"),
    [
        (JPEG, ("photo", "jpg")),
        (PNG, ("photo", "png")),
        (b"GIF89a" + b"\0" * 600, ("photo", "gif")),
        (b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 600, ("photo", "webp")),
        (b"\0\0\0\x18ftypheic\0\0\0\0" + b"\0" * 600, ("photo", "heic")),
        (b"\0\0\0\x1cftypavif\0\0\0\0" + b"\0" * 600, ("photo", "avif")),
        (b"II*\0" + b"\0" * 600, ("photo", "tif")),
        (b"\0\0\0\x20ftypisom\0\0\0\0" + b"\0" * 600, ("video", "mp4")),
        (b"\0\0\0\x14ftypqt  \0\0\0\0" + b"\0" * 600, ("video", "mov")),
        (b"\x1a\x45\xdf\xa3\x01\0\0\0webm" + b"\0" * 600, ("video", "webm")),
    ],
)
def test_photos_and_videos_are_told_by_their_first_bytes(head, media):
    found = upload.sniff(head[: upload.SNIFF_BYTES])
    assert found is not None and (found.type, found.ext) == media


@pytest.mark.parametrize("head", [b"%PDF-1.7\n" + b"\0" * 600, b"<!doctype html>" + b" " * 600, b"PK\x03\x04" * 200])
def test_anything_else_is_refused_as_soon_as_its_head_arrives(tmp_path, head):
    assert upload.sniff(head[: upload.SNIFF_BYTES]) is None
    staging = upload.Staging(tmp_path, 10 * 1024**2)
    with pytest.raises(PhotosError) as caught:
        staging.write(head)
    assert caught.value.code == "invalid_argument" and "not a photo or video" in caught.value.detail


def test_names_keep_a_safe_base_name_and_get_an_extension():
    photo = upload.Media("photo", "jpg")
    assert upload.filename("../../etc/IMG_0001.JPG", photo, 0) == "IMG_0001.JPG"
    assert upload.filename("C:\\Users\\x\\Café plage.heic", photo, 0) == "Café plage.heic"
    assert upload.filename("holiday", photo, 0) == "holiday.jpg"
    assert upload.filename("a\x00b\nc.png", photo, 0) == "abc.png"
    assert upload.filename(None, photo, 1_792_000_000) == "photo-20261014-174640.jpg"
    assert len(upload.filename("x" * 500 + ".png", photo, 0)) == upload.FILENAME_MAX
    assert upload.header_filename("Café plage.heic") == "Caf_ plage.heic"


def test_staging_hashes_caps_and_forgets(tmp_path):
    with upload.Staging(tmp_path / "u", len(PNG)) as staging:
        for start in range(0, len(PNG), 7):
            staging.write(PNG[start : start + 7])
        ready = staging.finish("pic", None, 1_792_000_000.0)
        assert ready.size == len(PNG) and ready.sha1 == b64sha1(PNG) and ready.name == "pic.png"
        assert ready.modified_ms == 1_792_000_000_000 and ready.file.read() == PNG
    assert list((tmp_path / "u").iterdir()) == []  # an anonymous file: nothing is left on disk
    with pytest.raises(PhotosError) as caught, upload.Staging(tmp_path / "u", len(PNG) - 1) as staging:
        staging.write(PNG)
    assert "larger than" in caught.value.detail
    with pytest.raises(PhotosError), upload.Staging(tmp_path / "u", 100) as staging:
        staging.finish("x", None, 0)  # empty


def test_a_client_modified_time_is_used_only_when_plausible():
    now = 1_792_000_000.0
    assert upload.modified_ms("1700000000000", now) == 1_700_000_000_000
    for bad in ("", "abc", "12", str(int((now + 2 * 86_400) * 1000)), None, True):
        assert upload.modified_ms(bad, now) is None


def test_matches_and_created_items_share_one_shape():
    assert upload.parse_matches([]) == []
    (match,) = upload.parse_matches([[stored(NEW, "fake-hash")]])
    assert (match.media_key, match.width, match.height, match.tz_offset) == (NEW, 40, 30, 28_800_000)
    assert upload.parse_created([[stored(NEW)]]).media_key == NEW
    with pytest.raises(PhotosError) as caught:
        upload.parse_created([[["fake-token", None, 0]]])
    assert caught.value.code == "schema_changed"


# -- the transport -------------------------------------------------------------------------------------------------


def chunked_route(seen, size: int, token: bytes, granularity: str = "262144"):
    def handler(method, url, params, data):
        seen.append({"url": url, "params": params, "data": data})
        if params == {"authuser": "0"}:
            return HttpResponse(200, {"x-goog-upload-status": "active", "x-goog-upload-url": SESSION_URL,
                                      "x-goog-upload-chunk-granularity": granularity}, b"")  # fmt: skip
        received = sum(len(s["data"]) for s in seen[1:])
        if received >= size:
            return HttpResponse(200, {"x-goog-upload-status": "final", "content-type": "application/octet-stream"},
                                token)  # fmt: skip
        return HttpResponse(200, {"x-goog-upload-status": "active"}, b"")

    return handler


def test_bytes_go_to_the_upload_endpoint_in_the_web_apps_shape(tmp_path, clock):
    seen: list[dict[str, Any]] = []
    t, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), UPLOAD: None}, clock)
    sender.routes[UPLOAD] = chunked_route(seen, len(PNG), b"CAISfake-upload-token-000\n")
    file = staged(tmp_path)
    assert t.upload_bytes(file.file, size=file.size, name="Café.png", sha1=file.sha1) == "CAISfake-upload-token-000"
    start, chunk = sender.requests
    assert start["url"] == "https://photos.google.com/_/upload/uploadmedia/interactive"
    assert start["params"] == {"authuser": "0"} and start["data"] == upload.start_body(len(PNG))
    headers = start["headers"]
    assert headers["x-goog-upload-command"] == "start" and headers["x-goog-upload-protocol"] == "resumable"
    assert headers["x-goog-upload-header-content-length"] == str(len(PNG))
    assert headers["x-goog-hash"] == f"sha1={b64sha1(PNG)}" and headers["x-goog-upload-file-name"] == "Caf_.png"
    assert "x-same-domain" not in headers and "cookie" in headers
    assert headers["content-type"] == "application/x-www-form-urlencoded;charset=UTF-8"
    assert chunk["url"] == SESSION_URL and chunk["data"] == PNG
    assert chunk["headers"]["x-goog-upload-command"] == "upload, finalize"
    assert chunk["headers"]["x-goog-upload-offset"] == "0"
    # No app page and no batchexecute: the upload endpoint needs only the session's cookies.
    assert sender.hosts() == ["POST photos.google.com/_/upload/uploadmedia/interactive"] * 2
    assert t.state.describe()["budgets"]["rpc_left"] == 3000 - 1  # one unit per file, whatever its size


def test_a_large_file_goes_in_chunks_of_the_granularity(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(upload, "CHUNK", 8)
    seen: list[dict[str, Any]] = []
    t, sender = make_transport(tmp_path, {UPLOAD: None}, clock)
    sender.routes[UPLOAD] = chunked_route(seen, 20, b"CAISfake-upload-token-000", granularity="4")
    data = bytes(range(20))
    assert t.upload_bytes(io.BytesIO(data), size=20, name="v.mp4", sha1="fake") == "CAISfake-upload-token-000"
    chunks = sender.requests[1:]
    assert [r["headers"]["x-goog-upload-offset"] for r in chunks] == ["0", "8", "16"]
    assert [r["headers"]["x-goog-upload-command"] for r in chunks] == ["upload", "upload", "upload, finalize"]
    assert b"".join(r["data"] for r in chunks) == data
    assert t.state.describe()["budgets"]["rpc_left"] == 3000 - 1


def test_a_session_address_elsewhere_is_never_followed(tmp_path, clock):
    t, sender = make_transport(tmp_path, {UPLOAD: None}, clock)
    for elsewhere in (
        "https://elsewhere.example.com/_/upload/uploadmedia/interactive?upload_id=x",
        "https://photos.google.com/_/PhotosUi/data/batchexecute",
        "http://photos.google.com/_/upload/uploadmedia/interactive",
    ):
        sender.requests.clear()
        sender.routes[UPLOAD] = HttpResponse(
            200, {"x-goog-upload-status": "active", "x-goog-upload-url": elsewhere}, b""
        )
        file = staged(tmp_path)
        with pytest.raises(PhotosError) as caught:
            t.upload_bytes(file.file, size=file.size, name="a.png", sha1=file.sha1)
        assert caught.value.code == "schema_changed" and len(sender.requests) == 1


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (status(401), "session_expired"),
        (status(429), "blocked"),
        (status(403), "refused"),
        (status(413), "invalid_argument"),
        (status(302), "upstream_error"),
    ],  # fmt: skip
)
def test_upload_failures_are_classified_and_change_nothing(tmp_path, clock, answer, code):
    t, _ = make_transport(tmp_path, {UPLOAD: answer}, clock)
    file = staged(tmp_path)
    with pytest.raises(PhotosError) as caught:
        t.upload_bytes(file.file, size=file.size, name="a.png", sha1=file.sha1)
    assert caught.value.code == code != "outcome_unknown"
    assert t.state.describe()["breaker"]["open"] is (code == "blocked")


def test_the_start_is_retried_but_a_chunk_never(tmp_path, clock):
    active = HttpResponse(200, {"x-goog-upload-status": "active", "x-goog-upload-url": SESSION_URL}, b"")
    t, sender = make_transport(tmp_path, {UPLOAD: [status(503), active, NetworkError("reset"), active]}, clock)
    file = staged(tmp_path)
    with pytest.raises(PhotosError) as caught:
        t.upload_bytes(file.file, size=file.size, name="a.png", sha1=file.sha1)
    assert caught.value.code == "upstream_error"  # never outcome_unknown: nothing is in the library yet
    assert [r["params"] for r in sender.requests] == [{"authuser": "0"}, {"authuser": "0"}, None]


# -- the service ---------------------------------------------------------------------------------------------------


class Library(Albums):
    """Albums, plus a library that knows files by hash, takes uploads and makes items from them."""

    def __init__(self, state):
        super().__init__(state)
        self.by_hash: dict[str, str] = {}
        self.uploads: list[tuple[bytes, str]] = []
        self.tokens: dict[str, str] = {}
        self.seen: list[str] = []

    def rpc(self, payloads_):
        self.seen += [p.rpcid for p in payloads_]
        if payloads_[0].rpcid == "snAcKc" and payloads_[0].data[0] not in self.albums:
            return []  # no such album: Google answers without data
        if payloads_[0].rpcid == "swbisb":
            (wanted,) = payloads_[0].data[0]
            payloads_[0].payload_id = "generic"
            hit = self.by_hash.get(wanted)
            return [RpcResult("swbisb", "generic", [[stored(hit, wanted)]] if hit else [])]
        return super().rpc(payloads_)

    def upload_bytes(self, source, *, size, name, sha1):
        data = source.read()
        assert len(data) == size
        self.uploads.append((data, name))
        token = f"fake-token-{len(self.uploads)}"
        self.tokens[token] = sha1
        return token

    def write(self, payloads_):
        if payloads_[0].rpcid != "mdpdU":
            return super().write(payloads_)
        if self.fail_write:
            raise self.fail_write
        self.writes.append([(p.rpcid, p.data) for p in payloads_])
        token = payloads_[0].data[0][0][0]
        payloads_[0].payload_id = "generic"
        new = f"AF1QipNEWITEM{len(self.by_hash):04d}" + "0" * 27
        self.by_hash[self.tokens[token]] = new
        return [RpcResult("mdpdU", "generic", [[stored(new, token)]])]


@pytest.fixture
def library(state) -> Library:
    return Library(state)


@pytest.fixture
def svc(library, tmp_path, clock) -> PhotosService:
    return PhotosService(library, ThumbCache(tmp_path / "cache", 1024 * 1024), CursorStore(), clock=clock)


def test_a_new_file_is_uploaded_and_made_an_item(svc, library, tmp_path):
    result = svc.upload(staged(tmp_path))
    item = result["item"]
    assert library.uploads == [(PNG, "pic.png")]
    assert library.writes == [[("mdpdU", [[["fake-token-1", "pic.png", 1_700_000_000_000, 2, 2]]])]]
    assert item["id"].startswith("AF1QipNEWITEM") and item["url"] == f"https://photos.google.com/photo/{item['id']}"
    assert (item["type"], item["filename"], item["width"], item["height"]) == ("photo", "pic.png", 40, 30)
    assert item["taken_at"].endswith("+08:00") and item["tz_known"] and item["uploaded_at"].endswith("Z")
    assert result["already_in_library"] is False and result["album"] is None and result["size_bytes"] == len(PNG)
    assert [(a["action"], a["outcome"]) for a in audit(svc)] == [("upload", "sent")]
    assert svc.status()["budgets"]["write_left"] == 499


def test_a_file_already_in_the_library_is_not_sent_again(svc, library, tmp_path):
    first = svc.upload(staged(tmp_path))
    again = svc.upload(staged(tmp_path, name="other name.png"))
    assert len(library.uploads) == 1 and len(library.writes) == 1
    assert again["already_in_library"] is True and again["item"]["id"] == first["item"]["id"]


def test_into_an_album_is_checked_first_and_read_back(svc, library, tmp_path):
    result = svc.upload(staged(tmp_path), ALBUM)
    new = result["item"]["id"]
    assert library.writes[-1] == [("E1Cajb", [[new], ALBUM])]
    assert result["album"]["id"] == ALBUM and result["album"]["added"] == 1 and result["album"]["count"] == 6
    shared = svc.upload(staged(tmp_path, JPEG, "b.jpg"), SHARED)
    assert library.writes[-1][0][0] == "laUYf" and shared["album"]["added"] == 1
    # A link checked its album when it was made: per file only the add, no reads.
    reads = library.seen.count("snAcKc")
    quick = svc.upload(staged(tmp_path, make_image(20, 20, "GIF"), "c.gif"), ALBUM, check_album=False)
    assert library.seen.count("snAcKc") == reads and quick["album"] == {
        "id": ALBUM, "url": f"https://photos.google.com/album/{ALBUM}", "added": None}  # fmt: skip


def test_a_wrong_album_fails_before_anything_is_sent(svc, library, tmp_path):
    with pytest.raises(PhotosError) as caught:
        svc.upload(staged(tmp_path), "AF1QipFAKENOSUCHALBUM" + "0" * 20)
    assert caught.value.code == "item_unavailable"
    assert library.uploads == [] and library.writes == []


def test_the_write_budget_stops_an_upload_before_it_is_sent(svc, library, tmp_path, state):
    state.write_budget = 0
    with pytest.raises(PhotosError) as caught:
        svc.upload(staged(tmp_path))
    assert caught.value.code == "budget_exhausted" and library.uploads == []


def test_a_failed_album_step_still_reports_the_item(svc, library, tmp_path):
    library.titles.pop(ALBUM)
    library.albums.pop(ALBUM)  # deleted after the link checked it

    def gone(payloads_):
        if payloads_[0].rpcid == "E1Cajb":
            payloads_[0].payload_id = "generic"
            return []  # Google answers the add without data
        return Library.write(library, payloads_)

    library.write = gone  # type: ignore[method-assign]
    result = svc.upload(staged(tmp_path), ALBUM, check_album=False)
    assert result["item"]["id"].startswith("AF1QipNEWITEM") and result["already_in_library"] is False
    assert result["album"]["error"]["code"] == "refused" and result["album"]["added"] == 0


def test_google_refusing_the_item_and_an_unknown_outcome_are_reported(svc, library, tmp_path):
    def refused(payloads_):
        payloads_[0].payload_id = "generic"
        return []

    library.write = refused  # type: ignore[method-assign]
    with pytest.raises(PhotosError) as caught:
        svc.upload(staged(tmp_path))
    assert caught.value.code == "refused"
    del library.write
    library.fail_write = PhotosError("outcome_unknown")
    with pytest.raises(PhotosError) as caught:
        svc.upload(staged(tmp_path, JPEG, "b.jpg"))
    assert caught.value.code == "outcome_unknown"
    assert [a["outcome"] for a in audit(svc)] == ["refused", "outcome_unknown"]


# -- the guarded fetch ---------------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes = b""):
        self.status_code, self.headers, self.body = status_code, headers, body

    def iter_content(self):
        for start in range(0, len(self.body), 100):
            yield self.body[start : start + 100]

    def close(self):
        pass


class FakeSessions:
    def __init__(self, answers: list[FakeResponse]):
        self.answers = answers
        self.made: list[dict[str, Any]] = []

    def __call__(self, **options):
        record = {"options": options}
        self.made.append(record)
        answers = self.answers

        class Session:
            def get(self, url, **kwargs):
                record["url"], record["kwargs"] = url, kwargs
                return answers.pop(0)

            def close(self):
                record["closed"] = True

        return Session()


def resolver(table: dict[str, list[str]]):
    return lambda host: table[host]


def test_a_link_is_fetched_from_the_pinned_public_address(tmp_path):
    sessions = FakeSessions([FakeResponse(200, {"content-disposition": 'inline; filename="sunset.png"'}, PNG)])
    got = bytearray()
    name = fetch.fetch("https://Images.Example.com/a/b?sig=x", got.extend,
                       resolver=resolver({"images.example.com": ["93.184.215.14", "2606:2800::1"]}),
                       session_factory=sessions)  # fmt: skip
    assert bytes(got) == PNG and name == "sunset.png"
    (made,) = sessions.made
    options = made["options"]
    from curl_cffi import CurlOpt

    assert options["curl_options"][CurlOpt.RESOLVE] == ["images.example.com:443:93.184.215.14"]
    assert options["curl_options"][CurlOpt.PROTOCOLS_STR] == "https" and options["trust_env"] is False
    assert options["curl_options"][CurlOpt.PROXY] == ""
    assert 0 < options["curl_options"][CurlOpt.TIMEOUT_MS] <= fetch.FETCH_DEADLINE * 1000  # headers included
    assert made["url"] == "https://images.example.com/a/b?sig=x" and made["closed"]
    assert made["kwargs"]["allow_redirects"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a.jpg",
        "https://example.com:8443/a.jpg",
        "https://name@example.com/a.jpg",
        "ftp://x/a",
        "file:///etc/passwd",
        "https:///nohost",
        "https://x y/",
    ],  # fmt: skip
)
def test_only_https_links_on_port_443_are_followed(url):
    with pytest.raises(PhotosError) as caught:
        fetch.fetch(url, lambda _: None, resolver=resolver({}), session_factory=FakeSessions([]))
    assert caught.value.code in ("invalid_argument", "fetch_failed")


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["192.168.1.1"],
        ["172.17.0.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["::1"],
        ["fd00::1"],
        ["fe80::1%eth0"],
        ["198.18.0.1"],
        ["93.184.215.14", "10.0.0.1"],
        [],
        ["64:ff9b::a00:1"],
        ["::7f00:1"],
        ["::ffff:10.0.0.1"],
        ["224.0.0.1"],
        ["ff0e::1"],
        ["2002:a00:1::1"],
        ["2001:0:4136:e378:8000:63bf:f5ff:fffe"],
    ],  # fmt: skip
)
def test_a_host_with_any_private_address_is_refused_before_connecting(addresses):
    sessions = FakeSessions([])
    with pytest.raises(PhotosError) as caught:
        fetch.fetch("https://inside.example.com/x.jpg", lambda _: None,
                    resolver=resolver({"inside.example.com": addresses}), session_factory=sessions)  # fmt: skip
    assert caught.value.code == "invalid_argument" and sessions.made == []


def test_redirects_are_checked_like_the_link_and_limited():
    sessions = FakeSessions([FakeResponse(302, {"location": "https://cdn.example.net/x.jpg"}),
                             FakeResponse(302, {"location": "https://intranet.example.net/x.jpg"})])  # fmt: skip
    table = {"a.example.com": ["93.184.215.14"], "cdn.example.net": ["93.184.215.15"],
             "intranet.example.net": ["10.0.0.5"]}  # fmt: skip
    with pytest.raises(PhotosError) as caught:
        fetch.fetch("https://a.example.com/x", lambda _: None, resolver=resolver(table), session_factory=sessions)
    assert caught.value.code == "invalid_argument" and len(sessions.made) == 2
    loop = FakeSessions([FakeResponse(302, {"location": "/again"}) for _ in range(5)])
    with pytest.raises(PhotosError) as caught:
        fetch.fetch("https://a.example.com/x", lambda _: None, resolver=resolver(table), session_factory=loop)
    assert caught.value.code == "fetch_failed" and len(loop.made) == fetch.MAX_REDIRECTS + 1


def test_the_deadline_covers_every_hop():
    clock = Clock(0.0)
    sessions = FakeSessions([FakeResponse(302, {"location": "/next"})])
    table = {"a.example.com": ["93.184.215.14"]}

    def slow(**options):
        clock.now += fetch.FETCH_DEADLINE  # the first hop took all the time there was
        return sessions(**options)

    with pytest.raises(PhotosError) as caught:
        fetch.fetch(
            "https://a.example.com/x", lambda _: None, resolver=resolver(table), session_factory=slow, monotonic=clock
        )
    assert caught.value.code == "fetch_failed" and len(sessions.made) == 1


def test_an_error_answer_is_fetch_failed():
    sessions = FakeSessions([FakeResponse(404, {})])
    with pytest.raises(PhotosError) as caught:
        fetch.fetch("https://a.example.com/x.jpg", lambda _: None,
                    resolver=resolver({"a.example.com": ["93.184.215.14"]}), session_factory=sessions)  # fmt: skip
    assert caught.value.code == "fetch_failed"


# -- the tools and upload links ------------------------------------------------------------------------------------


def call(server_, name: str, args: dict) -> dict:
    import asyncio

    from fastmcp import Client

    async def go():
        async with Client(server_) as client:
            return await client.call_tool(name, args, raise_on_error=False)

    result = asyncio.run(go())
    return json.loads(result.content[0].text)


def test_photos_upload_takes_base64_or_a_link(svc, library, monkeypatch):
    mcp = server.create_server(svc)
    added = call(mcp, "photos_upload", {"data": base64.b64encode(PNG).decode(), "filename": "drawn"})
    assert added["item"]["filename"] == "drawn.png" and library.uploads[-1] == (PNG, "drawn.png")
    data_url = "data:image/jpeg;base64," + base64.urlsafe_b64encode(JPEG).decode().rstrip("=")
    assert call(mcp, "photos_upload", {"data": data_url})["item"]["type"] == "photo"

    def fake_fetch(url, sink, **_):
        assert url == "https://example.com/pic"
        sink(make_image(10, 10, "GIF"))
        return "pic"

    monkeypatch.setattr(fetch, "fetch", fake_fetch)
    assert call(mcp, "photos_upload", {"url": "https://example.com/pic"})["item"]["filename"] == "pic.gif"
    for bad in ({}, {"url": "https://example.com/a", "data": "AAAA"}, {"data": "not base64!"},
                {"data": base64.b64encode(b"%PDF-1.7" + b"\0" * 600).decode()}):  # fmt: skip
        assert call(mcp, "photos_upload", bad)["error"]["code"] == "invalid_argument", bad
    assert "photos_upload_link" not in {t.name for t in _tools(mcp)}  # no public address configured


def _tools(mcp):
    import asyncio

    from fastmcp import Client

    async def go():
        async with Client(mcp) as client:
            return await client.list_tools()

    return asyncio.run(go())


@pytest.fixture
def linked(tmp_path, clock):
    """A server with a public address, its default account served by a scripted Google."""
    (tmp_path / "session").mkdir()
    write_meta_default(tmp_path / "session")
    settings = make_settings(
        tmp_path, PHOTOS_PUBLIC_URL="https://example.com/photos", PHOTOS_UPLOAD_MAX_BYTES=str(2**20)
    )
    known: dict[str, str] = {}

    def batch(method, url, params, data):
        from urllib.parse import parse_qs

        freq = json.loads(parse_qs(data)["f.req"][0])[0][0]
        rpcid, args = freq[0], json.loads(freq[1])
        if rpcid == "swbisb":
            hit = known.get(args[0][0])
            return ok(rpc_body(wrb("swbisb", [[stored(hit, args[0][0])]] if hit else [])))
        if rpcid == "EWgK9e":
            return ok(rpc_body(wrb("EWgK9e", None)))  # no filenames: the answer goes out without one
        if rpcid == "mdpdU":
            new = f"AF1QipFAKEHTTPITEM{len(known):04d}" + "0" * 22
            known[b64sha1(PNG)] = new
            return ok(rpc_body(wrb("mdpdU", [[stored(new, args[0][0][0])]])))
        raise AssertionError(rpcid)

    routes = {
        PAGE: ok(app_page(), "text/html"),
        BATCH: batch,
        UPLOAD: chunked_route([], len(PNG), b"CAISfake-token-0001"),
    }
    from conftest import FakeSender

    sender = FakeSender(routes)
    registry = server.Accounts.from_settings(settings, sender_factory=lambda snapshot: sender)
    auth = bearer_auth_from_env({"MCP_BEARER_TOKEN": PROXY})
    app = server.create_server(registry, auth=auth).http_app(**server.HTTP_OPTIONS)
    with TestClient(app) as client:
        yield client, sender


def tool(client, name: str, args: dict | None = None, person: str = "main") -> dict:
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {PROXY}",
               "mcp-protocol-version": "2025-03-26", HEADER: person}  # fmt: skip
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def test_an_upload_link_is_a_page_and_a_put_address_without_the_bearer(linked):
    client, sender = linked
    link = tool(client, "photos_upload_link")
    assert link["url"].startswith("https://example.com/photos/uploads/") and link["files_max"] == 100
    assert link["url"] in link["put_example"] and link["album"] is None
    path = link["url"].removeprefix("https://example.com/photos")
    page = client.get(path)
    assert page.status_code == 200 and "Add to Google Photos" in page.text and "o•••@example.com" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    put = client.put(path, content=PNG, headers={"X-File-Name": "Caf%C3%A9.png", "X-File-Modified": "1700000000000"})
    assert put.status_code == 201, put.text
    body = put.json()
    assert body["item"]["filename"] == "Café.png" and body["already_in_library"] is False and body["files_left"] == 99
    again = client.put(path, content=PNG)
    assert again.status_code == 201 and again.json()["already_in_library"] is True
    assert sender.hosts().count("POST photos.google.com/_/upload/uploadmedia/interactive") == 2  # sent once


def test_an_upload_link_refuses_what_is_not_a_photo_and_ids_it_does_not_know(linked):
    client, _ = linked
    path = tool(client, "photos_upload_link")["url"].removeprefix("https://example.com/photos")
    bad = client.put(path, content=b"%PDF-1.7\n" + b"\0" * 2000)
    assert bad.status_code == 400 and "not a photo or video" in bad.json()["error"]["message"]
    big = client.put(path, content=b"\xff\xd8\xff" + b"\0" * (2**20))
    assert big.status_code == 400 and "larger than 1 MiB" in big.json()["error"]["message"]
    for unknown in ("/uploads/" + "x" * 43, "/uploads/short"):
        assert client.get(unknown).status_code == 404 and "expired" in client.get(unknown).text
        assert client.put(unknown, content=PNG).status_code == 404


def test_an_upload_link_checks_its_album_when_it_is_made(linked):
    client, _ = linked
    error = tool(client, "photos_upload_link", {"album_id": "not an album!"})
    assert error["error"]["code"] == "invalid_argument"


def test_only_a_server_with_a_public_address_offers_links(tmp_path):
    (tmp_path / "session").mkdir()
    write_meta_default(tmp_path / "session")
    plain = server.create_server(server.Accounts.from_settings(make_settings(tmp_path)))
    assert "photos_upload_link" not in {t.name for t in _tools(plain)}
    assert "photos_upload" in {t.name for t in _tools(plain)}


def test_the_link_adds_to_the_account_that_made_it_and_expires(tmp_path):
    from photos_mcp import uploads
    from photos_mcp.uploads import UploadLinks

    settings = make_settings(tmp_path, PHOTOS_PUBLIC_URL="https://example.com/photos")
    asked: list[str | None] = []

    class Svc:
        account = type("A", (), {"id": "second", "email": "second@example.com", "created_at": 5})()

        def album(self, album_id):
            raise AssertionError

        def upload(self, staged_, album_id, *, check_album):
            assert album_id is None and check_album is False
            return {"item": {"type": "photo"}, "already_in_library": False, "freshness": {}}

        def record_error(self, code):
            pass

    def service_for(account_id):
        asked.append(account_id)
        return Svc()

    clock = Clock()
    links = UploadLinks(settings, service_for, upload.Gate(2), clock=clock)
    made = links.create(Svc())  # type: ignore[arg-type]
    path = "/" + made["url"].rsplit("/", 1)[1]
    with TestClient(links.app()) as client:
        response = client.put(path, content=PNG)
        assert response.status_code == 201 and asked == ["second"]
        assert response.json()["files_left"] == 99
        # The account was removed and added again (for someone else): the old link adds nothing to it.
        Svc.account = type("A", (), {"id": "second", "email": "other@example.com", "created_at": 9})()
        replaced = client.put(path, content=PNG)
        assert replaced.status_code == 502 and replaced.json()["error"]["code"] == "account_not_connected"
        clock.now += uploads.LINK_TTL + 1
        assert client.put(path, content=PNG).status_code == 404
        assert client.get(path).status_code == 404


def test_a_file_waits_for_an_upload_slot_before_it_is_read(tmp_path, monkeypatch):
    from photos_mcp import uploads
    from photos_mcp.uploads import UploadLinks

    settings = make_settings(tmp_path, PHOTOS_PUBLIC_URL="https://example.com/photos")
    monkeypatch.setattr(uploads, "GATE_WAIT", 1.0)

    class Svc:
        account = None

        def upload(self, staged_, album_id, *, check_album):
            raise AssertionError("never reached: no slot")

    gate = upload.Gate(1)
    assert gate.try_acquire()  # another upload holds the only slot
    links = UploadLinks(settings, lambda _: Svc(), gate, clock=Clock())
    made = links.create(Svc())  # type: ignore[arg-type]
    with TestClient(links.app()) as client:
        busy = client.put("/" + made["url"].rsplit("/", 1)[1], content=PNG)
    assert busy.status_code == 503 and busy.json()["error"]["code"] == "busy"
    gate.release()
    assert gate.try_acquire()  # the waiting request never took the slot
