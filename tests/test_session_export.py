"""photos-mcp-session: every snapshot it writes is one photos-mcp's reader accepts (never live:
the browser commands are covered through their pure parts)."""

from __future__ import annotations

import json
import stat
import sys

import pytest
from conftest import FIXTURES

from photos_mcp import session_export as export
from photos_mcp.session import canonical_order, read_snapshot, synthesise_templates

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
SAMPLE = FIXTURES / "session" / "cookies.txt"


def _import(tmp_path, cookies=SAMPLE, user_agent=UA):
    return export.import_cookies(cookies, email=" Owner@Example.com ", user_agent=user_agent, out_dir=tmp_path / "s")


def test_import_writes_a_private_snapshot_the_reader_accepts(tmp_path):
    assert _import(tmp_path) == {"out": str(tmp_path / "s"), "schema": 1, "generation": 1}
    snapshot = read_snapshot(tmp_path / "s")
    assert (snapshot.schema, snapshot.generation, snapshot.email) == (1, 1, "owner@example.com")
    assert snapshot.headers == synthesise_templates(UA)
    assert snapshot.cookies.header_for("photos.fife.usercontent.google.com", "/")  # host-only CDN cookies
    assert stat.S_IMODE((tmp_path / "s").stat().st_mode) == 0o700
    for name in ("cookies.txt", "meta.json"):
        assert stat.S_IMODE((tmp_path / "s" / name).stat().st_mode) == 0o600
    assert _import(tmp_path)["generation"] == 2  # a newer generation every time
    assert not [p for p in (tmp_path / "s").iterdir() if p.name.startswith(".")]  # no temp files left


def test_import_keeps_only_google_session_cookies(tmp_path):
    rows = SAMPLE.read_text() + ".example.org\tTRUE\t/\tTRUE\t0\tSID\tother-site\n"
    rows += ".google.com\tTRUE\t/\tTRUE\t0\tPREF\tnot-a-session-cookie\n"
    (tmp_path / "c.txt").write_text(rows)
    _import(tmp_path, tmp_path / "c.txt")
    text = (tmp_path / "s" / "cookies.txt").read_text()
    assert "other-site" not in text and "PREF" not in text and "fake-sid-value" in text


@pytest.mark.parametrize(
    ("drop", "message"),
    [(("\tSID\t", "\t__Secure-1PSID\t"), "sign-in cookie"), (("photos.fife.usercontent",), "open one photo")],
)
def test_import_refuses_an_incomplete_export(tmp_path, drop, message):
    rows = [r for r in SAMPLE.read_text().splitlines() if not any(d in r for d in drop)]
    (tmp_path / "c.txt").write_text("\n".join(rows) + "\n")
    with pytest.raises(export.ExportError, match=message):
        _import(tmp_path, tmp_path / "c.txt")
    assert not (tmp_path / "s" / "meta.json").exists()


@pytest.mark.parametrize("user_agent", [UA.replace("Chrome/149", "HeadlessChrome/149"), "Mozilla/5.0 Firefox/140.0"])
def test_import_refuses_a_user_agent_photos_mcp_cannot_replay(tmp_path, user_agent):
    with pytest.raises(export.ExportError):
        _import(tmp_path, user_agent=user_agent)


def test_main_reports_errors_without_a_traceback(tmp_path, capsys):
    args = ["import", "--cookies", str(tmp_path / "missing.txt"), "--email", "a@example.com"]
    assert export.main([*args, "--user-agent", UA, "--out", str(tmp_path / "s")]) == 1
    assert capsys.readouterr().err.startswith("photos-mcp-session: cannot read")


def test_browser_cookies_follow_playwright_domain_conventions():
    cookies = export.browser_cookies(
        [
            {"domain": ".google.com", "name": "SID", "value": "a", "path": "/", "expires": -1, "secure": True},
            {"domain": "photos.fife.usercontent.google.com", "name": "OSID", "value": "b", "expires": 1.9e9},
            {"domain": ".google.com", "name": 7, "value": "ignored"},
        ]
    )
    assert [(c.domain, c.host_only, c.expires) for c in cookies] == [
        ("google.com", False, 0),
        ("photos.fife.usercontent.google.com", True, 1_900_000_000),
    ]


def _wire_events(recorder: export.WireHeaders) -> None:
    fetch = {
        "page": {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"},
        "rpc": {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"},
        "image": {"sec-fetch-site": "same-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"},
    }
    requests = [
        ("1", "https://photos.google.com/", "GET", "Document", {**fetch["page"], "accept": "text/html"}),
        ("2", "https://photos.google.com/_/PhotosUi/data/batchexecute?rpcids=lcxiM", "POST", "XHR",
         {**fetch["rpc"], "x-same-domain": "1", "content-type": "application/x-www-form-urlencoded;charset=UTF-8"}),
        ("3", "https://photos.fife.usercontent.google.com/pw/x=w100", "GET", "Image", fetch["image"]),
        ("4", "https://www.gstatic.com/x.js", "GET", "Script", fetch["image"]),
    ]  # fmt: skip
    for request_id, url, method, kind, headers in requests:
        recorder.on_sent({"requestId": request_id, "request": {"url": url, "method": method}, "type": kind})
        secrets = {"cookie": "SID=secret", "x-client-data": "id", "authorization": "SAPISIDHASH x"}
        recorder.on_wire({"requestId": request_id, "headers": {"User-Agent": UA, **headers, **secrets}})
    # A request whose wire headers never arrived (renderer-provisional only) is no candidate.
    recorder.on_sent({"requestId": "5", "request": {"url": "https://photos.google.com/", "method": "GET"}})


def test_wire_headers_become_a_schema_2_snapshot(tmp_path):
    recorder = export.WireHeaders()
    _wire_events(recorder)
    headers, synthesised = recorder.templates(UA)
    assert synthesised == []
    flat = json.dumps(headers)
    assert "secret" not in flat and "x-client-data" not in flat and "authorization" not in flat
    meta = export.build_meta(generation=3, email="owner@example.com", user_agent=UA, headers=headers,
                             verified_at=export.datetime.now(export.UTC))  # fmt: skip
    cookies = export.select_cookies(export.parse_cookies_txt(SAMPLE.read_text()))
    export.publish(tmp_path, cookies, meta)
    snapshot = read_snapshot(tmp_path)
    assert (snapshot.schema, snapshot.generation) == (2, 3)
    for kind in ("page", "rpc", "image"):
        assert snapshot.headers[kind] == canonical_order(kind, headers[kind])
    assert dict(snapshot.headers["rpc"])["x-same-domain"] == "1"


def test_a_kind_never_seen_on_the_wire_is_synthesised():
    recorder = export.WireHeaders()
    recorder.on_sent({"requestId": "1", "request": {"url": "https://photos.google.com/", "method": "GET"}})
    recorder.on_wire({"requestId": "1", "headers": {"user-agent": "another browser", "sec-fetch-dest": "document"}})
    headers, synthesised = recorder.templates(UA)
    assert synthesised == ["page", "rpc", "image"] and headers == synthesise_templates(UA)


def test_page_email_needs_the_signed_in_page():
    page = '<script data-id="_gd">window.WIZ_global_data = {"oPEP7c":"owner@example.com","x":1};</script>'
    assert export.page_email(page) == "owner@example.com"
    assert export.page_email('{"oPEP7c":"owner@example.com"}') is None  # no _gd: signed out
    assert export.page_email('<script data-id="_gd"></script>') is None


def test_browser_commands_explain_a_missing_playwright(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(export.ExportError, match="photos-mcp\\[session\\]"):
        export._playwright()


@pytest.mark.parametrize(
    ("url", "reading"),
    [
        ("https://accounts.google.com/ServiceLogin?continue=x", "signed_out"),
        ("https://accounts.google.com/v3/signin/identifier?continue=x", "signed_out"),
        ("https://accounts.google.com/AccountChooser", "signed_out"),
        ("https://www.google.com/photos/about/", "signed_out"),
        ("https://accounts.google.com/v3/signin/challenge/pwd", "challenge"),
        ("https://accounts.google.com/speedbump/idvreenable", "challenge"),
        ("https://accounts.google.com/web/recoveryoptions", "interstitial"),
        ("https://gds.google.com/web/chip", "interstitial"),
        ("https://photos.google.com/", None),
        ("https://example.com/photos/about", None),
    ],
)
def test_only_googles_sign_in_pages_read_as_signed_out(url, reading):
    assert export.signed_out_page(url) == reading
