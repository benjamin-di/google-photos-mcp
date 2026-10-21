"""CurlSender against a local HTTP server: no automatic redirects, no cookie jar, streamed cap,
Set-Cookie headers handed back for the allowlisted merge, and the exact request shape per
kind (the snapshot's header template replayed in order, nothing from curl_cffi added)."""

import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import SESSION_FIXTURE

from photos_mcp.session import read_snapshot
from photos_mcp.transport import (
    ACCEPT_ENCODING,
    BodyTooLarge,
    CurlSender,
    NetworkError,
    chrome_profiles,
    headers_for,
    impersonate_target,
)

# Added by HTTP framing on a plain-HTTP local server (h2c upgrade), not by the request shape.
FRAMING = {"host", "connection", "upgrade", "http2-settings", "content-length"}


class Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *args):
        pass

    def _record(self, body=b""):
        headers = self.headers
        Handler.seen.append(
            {
                "path": self.path,
                "ua": headers.get("User-Agent"),
                "cookie": headers.get("Cookie"),
                "body": body,
                "raw": [(name, value) for name, value in headers.items()],
            }
        )

    def do_GET(self):
        self._record()
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/trickle":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            for _ in range(40):  # a byte every 50 ms: never idle long enough for a read timeout
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.05)
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()  # no Content-Length: the cap must apply while streaming
            self.wfile.write(b"x" * 4096)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Set-Cookie", "SIDCC=a; Path=/")
            self.send_header("Set-Cookie", "NID=b; Path=/")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._record(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"[]")


@pytest.fixture
def server():
    Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def snapshot():
    return read_snapshot(SESSION_FIXTURE)


def test_no_redirects_no_jar_and_set_cookie_returned(server, snapshot):
    sender = CurlSender(snapshot)
    first = sender.send("GET", f"{server}/plain", headers=headers_for(snapshot, "page", "GET", "SID=x"), max_bytes=1024)
    assert first.status == 200 and first.body == b"ok"
    assert sorted(first.set_cookies) == ["NID=b; Path=/", "SIDCC=a; Path=/"]
    redirect = sender.send(
        "GET", f"{server}/redirect", headers=headers_for(snapshot, "page", "GET", None), max_bytes=1024
    )
    assert redirect.status == 302 and redirect.headers["location"] == "/target"
    assert [s["path"] for s in Handler.seen] == ["/plain", "/redirect"]  # /target never requested
    assert Handler.seen[0]["ua"] == snapshot.user_agent and Handler.seen[0]["cookie"] == "SID=x"
    assert Handler.seen[1]["cookie"] is None  # the response's cookies were not kept


def test_post_form_body_and_params(server, snapshot):
    sender = CurlSender(snapshot)
    response = sender.send(
        "POST",
        f"{server}/data/batchexecute",
        headers=headers_for(snapshot, "rpc", "POST", "SID=x"),
        params={"rpcids": "lcxiM", "rt": "c"},
        data="f.req=%5B%5D&at=tok",
        max_bytes=1024,
    )
    assert response.status == 200
    assert Handler.seen[0]["path"] == "/data/batchexecute?rpcids=lcxiM&rt=c"
    assert Handler.seen[0]["body"] == b"f.req=%5B%5D&at=tok"


def test_streamed_cap(server, snapshot):
    with pytest.raises(BodyTooLarge):
        CurlSender(snapshot).send("GET", f"{server}/big", headers=[], max_bytes=1000)


def test_a_trickling_body_hits_the_total_deadline(server, snapshot, monkeypatch):
    import photos_mcp.transport as transport

    monkeypatch.setattr(transport, "BODY_DEADLINE", 0.3)
    started = time.monotonic()
    with pytest.raises(NetworkError):
        CurlSender(snapshot).send("GET", f"{server}/trickle", headers=[], max_bytes=1000)
    assert time.monotonic() - started < 1.5


def test_connection_failure_is_network_error(snapshot):
    with pytest.raises(NetworkError):
        CurlSender(snapshot).send("GET", "http://127.0.0.1:9/", headers=[], max_bytes=10)


# -- the request shape actually on the wire ---------------------------------------------


def _snapshots(tmp_path):
    schema1 = tmp_path / "schema1"
    schema1.mkdir()
    shutil.copy(SESSION_FIXTURE / "cookies.txt", schema1 / "cookies.txt")
    shutil.copy(SESSION_FIXTURE / "meta.schema1.json", schema1 / "meta.json")
    return {"schema2": read_snapshot(SESSION_FIXTURE), "schema1": read_snapshot(schema1)}


def _ua_platform(user_agent):
    return {"X11": '"Linux"', "Windows": '"Windows"', "Macintosh": '"macOS"'}[
        next(key for key in ("X11", "Windows", "Macintosh") if key in user_agent)
    ]


@pytest.mark.parametrize("schema", ["schema2", "schema1"])
@pytest.mark.parametrize("kind,method", [("page", "GET"), ("rpc", "POST"), ("image", "GET")])
def test_headers_sent_per_kind_match_the_template_and_the_user_agent(server, tmp_path, schema, kind, method):
    snapshot = _snapshots(tmp_path)[schema]
    expected = headers_for(snapshot, kind, method, "SID=x")
    CurlSender(snapshot).send(
        method, f"{server}/{kind}", headers=expected, data="f.req=%5B%5D" if method == "POST" else None, max_bytes=1024
    )
    raw = Handler.seen[-1]["raw"]
    sent = [(name.lower(), value) for name, value in raw if name.lower() not in FRAMING]
    # Exactly the template plus cookie/referer/accept-encoding, in order: no curl_cffi defaults.
    # curl-impersonate sends ``priority`` on HTTP/2 only, as Chrome does; this server speaks 1.1.
    assert sent == [pair for pair in expected if pair[0] != "priority"]
    assert any(name == "priority" for name, _ in expected)
    assert not any(name.startswith("Sec-") or name in ("Accept-Encoding", "Accept-Language") for name, _ in raw)
    headers = dict(expected)
    ua_major = re.search(r"Chrome/(\d+)\.", headers["user-agent"]).group(1)
    assert headers["user-agent"] == snapshot.user_agent
    assert f'"Chromium";v="{ua_major}"' in headers["sec-ch-ua"]
    assert re.findall(r'"(?:Chromium|Google Chrome)";v="(\d+)"', headers["sec-ch-ua"]) == [ua_major]
    assert headers["sec-ch-ua-platform"] == _ua_platform(snapshot.user_agent)
    assert headers["accept-encoding"] == ACCEPT_ENCODING == "gzip, deflate, br, zstd"
    assert headers["cookie"] == "SID=x"
    if kind == "page":
        assert (headers["sec-fetch-mode"], headers["sec-fetch-dest"]) == ("navigate", "document")
        assert headers["upgrade-insecure-requests"] == "1" and "referer" not in headers
    elif kind == "rpc":
        assert (headers["sec-fetch-mode"], headers["sec-fetch-dest"]) == ("cors", "empty")
        assert headers["x-same-domain"] == "1" and headers["origin"] == "https://photos.google.com"
        assert headers["content-type"].startswith("application/x-www-form-urlencoded")
        assert "upgrade-insecure-requests" not in headers and "sec-fetch-user" not in headers
    else:
        assert (headers["sec-fetch-mode"], headers["sec-fetch-dest"]) == ("no-cors", "image")
        assert "upgrade-insecure-requests" not in headers and "x-same-domain" not in headers
    if kind != "page":
        assert headers["referer"] == "https://photos.google.com/"


def test_impersonation_follows_the_snapshot_chrome_version(snapshot):
    profiles = dict(chrome_profiles())
    assert CurlSender(snapshot).impersonate == impersonate_target(snapshot.chromium_major)
    target = impersonate_target(snapshot.chromium_major)
    major = next(m for m, name in profiles.items() if name == target)
    assert major <= snapshot.chromium_major  # never claims a newer Chrome than the browser
    assert impersonate_target(min(profiles) - 5) == profiles[min(profiles)]  # nothing older: nearest
    assert impersonate_target(max(profiles) + 30) == profiles[max(profiles)]


def test_fixture_chrome_is_within_four_majors_of_an_impersonation_profile(snapshot):
    """Drift guard: when the exporting Chrome (the sample tracks it) outruns curl_cffi's newest
    profile by more than 4 majors, upgrade curl_cffi (make lock) before shipping."""
    target = impersonate_target(snapshot.chromium_major)
    major = next(m for m, name in chrome_profiles() if name == target)
    assert abs(snapshot.chromium_major - major) <= 4, (snapshot.chromium_major, target)
