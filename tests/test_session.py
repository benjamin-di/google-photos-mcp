"""The session snapshot format (README "Session snapshot"), its checked-in sample and the reader."""

import copy
import json
import os
import shutil

import pytest
from conftest import EMAIL, SESSION_FIXTURE, session_dir, write_meta

from photos_mcp.errors import PhotosError
from photos_mcp.session import (
    HEADER_KINDS,
    SCHEMA,
    SET_COOKIE_NAMES,
    CookieStore,
    SessionReader,
    canonical_order,
    parse_cookies_txt,
    parse_meta,
    read_snapshot,
)

NOW = 1_790_000_000.0


SCHEMA1_FIELDS = {"schema", "generation", "email", "account_index", "user_agent", "chromium_major", "verified_at"}


def _meta(name="meta.json"):
    return json.loads((SESSION_FIXTURE / name).read_text())


def schema1_dir(tmp_path):
    target = tmp_path / "schema1"
    target.mkdir()
    shutil.copy(SESSION_FIXTURE / "cookies.txt", target / "cookies.txt")
    shutil.copy(SESSION_FIXTURE / "meta.schema1.json", target / "meta.json")
    return target


def test_sample_snapshot_matches_the_contract():
    meta = _meta()
    assert meta["schema"] == SCHEMA == 2
    assert set(meta) == SCHEMA1_FIELDS | {"headers"}
    assert set(meta["headers"]) == set(HEADER_KINDS) == {"page", "rpc", "image"}
    for kind, pairs in meta["headers"].items():
        names = [name for name, _ in pairs]
        assert len(names) == len(set(names)) and all(name == name.lower() for name in names), kind
        assert dict(pairs)["user-agent"] == meta["user_agent"], kind
        assert not {"cookie", "authorization", "referer", "host", "x-client-data"} & set(names), kind
    snapshot = read_snapshot(SESSION_FIXTURE)
    assert snapshot.generation == 7 and snapshot.email == EMAIL and snapshot.chromium_major == 149
    assert snapshot.schema == 2
    # The browser's values, verbatim, in Chrome's wire order (the capture's own order is sorted).
    assert snapshot.headers["rpc"] == canonical_order("rpc", [tuple(pair) for pair in meta["headers"]["rpc"]])
    names = {c.name for c in snapshot.cookies.cookies}
    assert names <= SET_COOKIE_NAMES  # the sample only uses the export names
    host_only = {(c.domain, c.name) for c in snapshot.cookies.cookies if c.host_only}
    assert ("photos.fife.usercontent.google.com", "OSID") in host_only
    assert ("photos.google.com", "OSID") in host_only


def test_host_only_rows_go_to_their_host_only():
    store = read_snapshot(SESSION_FIXTURE).cookies
    fife = store.header_for("photos.fife.usercontent.google.com", "/pw/x", NOW)
    photos = store.header_for("photos.google.com", "/", NOW)
    assert "OSID=fake-fife-osid" in fife and "fake-photos-osid" not in fife
    assert "OSID=fake-photos-osid" in photos and "fake-fife-osid" not in photos
    assert "SID=fake-sid-value" in fife and "SID=fake-sid-value" in photos  # .google.com rows
    lh = store.header_for("lh3.googleusercontent.com", "/", NOW)
    assert lh == ""  # googleusercontent.com is not google.com


def test_cookie_rows_must_be_consistent():
    good = ".google.com\tTRUE\t/\tTRUE\t0\tSID\tv\nphotos.google.com\tFALSE\t/\tTRUE\t0\tOSID\tw\n"
    rows = parse_cookies_txt(good)
    assert [(c.domain, c.host_only) for c in rows] == [("google.com", False), ("photos.google.com", True)]
    for bad in (
        "google.com\tTRUE\t/\tTRUE\t0\tSID\tv\n",  # TRUE without a leading dot
        ".google.com\tFALSE\t/\tTRUE\t0\tSID\tv\n",  # FALSE with a leading dot
        ".google.com\tTRUE\t/\tTRUE\tsoon\tSID\tv\n",
        ".google.com\tTRUE\t/\tTRUE\t0\tSID\n",
    ):
        with pytest.raises(ValueError):
            parse_cookies_txt(bad)
    # Rows for other domains are ignored, not trusted.
    assert parse_cookies_txt(".example.com\tTRUE\t/\tTRUE\t0\tSID\tv\n") == []


def test_expired_cookies_are_not_sent():
    store = CookieStore(parse_cookies_txt(".google.com\tTRUE\t/\tTRUE\t100\tSID\told\n"))
    assert store.header_for("photos.google.com", "/", NOW) == ""


def test_set_cookie_merges_allowlisted_names_in_memory_only(tmp_path):
    directory = session_dir(tmp_path)
    before = (directory / "cookies.txt").read_text()
    store = read_snapshot(directory).cookies
    merged = store.merge_set_cookie(
        "photos.google.com",
        [
            "SIDCC=new-sidcc; expires=Fri, 01-Oct-2027 00:00:00 GMT; path=/; domain=.google.com; Secure; priority=high",
            "OSID=new-osid; Max-Age=3600; path=/; Secure; HttpOnly",
            "TRACKER=x; domain=.google.com; path=/",  # not allowlisted
            "SID=; Max-Age=0; domain=.google.com; path=/",  # deletion: ignored
            "NID=evil; domain=.example.com; path=/",  # not a parent domain
        ],
        NOW,
    )
    assert merged == 2
    header = store.header_for("photos.google.com", "/", NOW)
    assert "SIDCC=new-sidcc" in header and "OSID=new-osid" in header and "SID=fake-sid-value" in header
    assert "TRACKER" not in header and "evil" not in header
    assert "fake-photos-osid" not in header
    # The fife host keeps its own OSID.
    assert "OSID=fake-fife-osid" in store.header_for("photos.fife.usercontent.google.com", "/", NOW)
    assert (directory / "cookies.txt").read_text() == before  # never written back


def test_schema1_sample_still_reads_with_self_consistent_templates(tmp_path):
    meta = _meta("meta.schema1.json")
    assert meta["schema"] == 1 and set(meta) == SCHEMA1_FIELDS
    snapshot = read_snapshot(schema1_dir(tmp_path))
    assert snapshot.schema == 1 and snapshot.chromium_major == 141
    for kind in HEADER_KINDS:
        headers = dict(snapshot.headers[kind])
        assert headers["user-agent"] == meta["user_agent"]
        assert 'v="141"' in headers["sec-ch-ua"] and headers["sec-ch-ua-platform"] == '"Linux"'
        assert headers["sec-ch-ua-mobile"] == "?0"
    assert dict(snapshot.headers["page"])["sec-fetch-mode"] == "navigate"
    rpc = dict(snapshot.headers["rpc"])
    assert (rpc["sec-fetch-mode"], rpc["sec-fetch-dest"], rpc["x-same-domain"]) == ("cors", "empty", "1")
    image = dict(snapshot.headers["image"])
    assert (image["sec-fetch-mode"], image["sec-fetch-dest"]) == ("no-cors", "image")


@pytest.mark.parametrize("major,expected", [(120, '"Not_A Brand";v="8"'), (131, '"Not_A Brand";v="24"')])
def test_synthesised_sec_ch_ua_follows_chromium_grease(major, expected):
    from photos_mcp.session import sec_ch_ua

    assert expected in sec_ch_ua(major) and f'"Chromium";v="{major}"' in sec_ch_ua(major)


def _with_headers(mutate):
    meta = copy.deepcopy(_meta())
    mutate(meta["headers"])
    return meta


@pytest.mark.parametrize(
    "meta",
    [
        _with_headers(lambda h: h.pop("image")),
        _with_headers(lambda h: h.update(rpc=[])),
        _with_headers(lambda h: h["page"].append(["accept", "dup"])),
        _with_headers(lambda h: h["page"].append(["Sec-Fetch-Mode", "navigate"])),
        _with_headers(lambda h: h["page"].append(["sec-ch-ua-model", "a\nb"])),
        _with_headers(lambda h: h["page"].append(["sec-ch-ua-model", "x" * 513])),
        _with_headers(lambda h: h["page"].extend([[f"sec-fetch-x{i}", "1"] for i in range(30)])),
        _with_headers(lambda h: h["image"].__setitem__(1, ["user-agent", "Mozilla/5.0 Chrome/148.0.0.0"])),
        _with_headers(lambda h: h["rpc"].__setitem__(0, ["sec-ch-ua-platform"])),
        {**_meta(), "headers": None},
        {**_meta(), "chromium_major": 148},  # must match the user agent
    ],
)
def test_schema2_header_validation(meta):
    with pytest.raises(ValueError):
        parse_meta(meta)


def test_headers_outside_the_allowlist_are_dropped_never_sent():
    meta = _with_headers(
        lambda h: (
            h["rpc"].extend([["cookie", "SID=x"], ["x-client-data", "abc"], ["referer", "https://x/"]]),
            h["page"].append(["content-type", "text/plain"]),  # content-type is rpc-only
        )
    )
    headers = parse_meta(meta)["headers"]
    assert {"cookie", "x-client-data", "referer"}.isdisjoint(name for name, _ in headers["rpc"])
    assert "content-type" not in dict(headers["page"]) and "content-type" in dict(headers["rpc"])


@pytest.mark.parametrize(
    "change",
    [
        {"schema": 3},
        {"schema": True},
        {"generation": 0},
        {"generation": "7"},
        {"account_index": 1},
        {"user_agent": "curl/8"},
        {"chromium_major": None},
        {"verified_at": "yesterday"},
        {"email": ""},
    ],
)
@pytest.mark.parametrize("sample", ["meta.json", "meta.schema1.json"])
def test_meta_validation(change, sample):
    meta = _meta(sample)
    meta.update(change)
    with pytest.raises(ValueError):
        parse_meta(meta)


def test_missing_or_broken_snapshot_is_session_unavailable(tmp_path):
    reader = SessionReader(tmp_path / "nothing")
    with pytest.raises(PhotosError) as caught:
        reader.current()
    assert caught.value.code == "session_unavailable"
    directory = session_dir(tmp_path)
    (directory / "cookies.txt").write_text("garbage\n")
    with pytest.raises(PhotosError) as caught:
        SessionReader(directory).current()
    assert caught.value.code == "session_unavailable"


def test_reader_reloads_only_on_a_newer_generation(tmp_path):
    directory = session_dir(tmp_path, generation=7)
    reader = SessionReader(directory)
    first = reader.current()
    assert reader.current() is first  # unchanged stat: no re-read
    write_meta(directory, 8)
    second = reader.current()
    assert second.generation == 8 and second is not first
    write_meta(directory, 5)  # an older generation never replaces a newer one
    assert reader.current().generation == 8
    os.remove(directory / "meta.json")
    with pytest.raises(PhotosError):
        reader.current()


def test_a_kind_without_complete_fetch_metadata_falls_back_to_the_synthesised_template(caplog):
    from photos_mcp.session import synthesise_templates

    def damage(h):
        # rpc: a provisional renderer capture without any sec-fetch-*; image: dest of a page.
        h["rpc"][:] = [pair for pair in h["rpc"] if not pair[0].startswith("sec-fetch-")]
        h["image"][:] = [[n, "document" if n == "sec-fetch-dest" else v] for n, v in h["image"]]

    meta = _with_headers(damage)
    meta["generation"] = 41
    with caplog.at_level("WARNING"):
        headers = parse_meta(meta)["headers"]
        parse_meta(meta)  # the same generation again: logged once
    synthesised = synthesise_templates(meta["user_agent"])
    assert headers["rpc"] == synthesised["rpc"] and headers["image"] == synthesised["image"]
    assert headers["page"] == canonical_order("page", [tuple(p) for p in _meta()["headers"]["page"]])  # kept
    warnings = [r.getMessage() for r in caplog.records if "fetch metadata" in r.getMessage()]
    assert sorted(warnings) == [
        "generation 41 headers.image lacks complete fetch metadata; using the synthesised template",
        "generation 41 headers.rpc lacks complete fetch metadata; using the synthesised template",
    ]
    assert all(meta["user_agent"] not in w for w in warnings)


@pytest.mark.parametrize("kind", ["page", "rpc", "image"])
def test_templates_follow_chrome_wire_order_whatever_order_the_capture_reports(kind):
    from photos_mcp.session import CANONICAL_ORDER, canonical_order, synthesise_templates

    synthesised = synthesise_templates(_meta()["user_agent"])[kind]
    assert canonical_order(kind, synthesised) == synthesised  # the synthesised order is canonical
    # CDP's requestWillBeSentExtraInfo reports names sorted: the reader restores the wire order.
    captured = sorted([*synthesised, ("sec-ch-ua-arch", '"x86"'), ("x-unknown", "1")])
    ordered = canonical_order(kind, tuple(captured))
    names = [name for name, _ in ordered]
    known = [n for n in names if n in CANONICAL_ORDER[kind]]
    assert known == sorted(known, key=CANONICAL_ORDER[kind].index)
    assert names.index("x-unknown") == names.index("sec-fetch-site") - 1  # stable slot
    assert sorted(ordered) == sorted(captured)  # nothing lost or changed

    meta = _with_headers(lambda h: h[kind].sort())
    assert [n for n, _ in parse_meta(meta)["headers"][kind]] == [
        n for n, _ in canonical_order(kind, tuple(tuple(p) for p in _meta()["headers"][kind]))
    ]


@pytest.mark.parametrize("kind", ["page", "rpc", "image"])
@pytest.mark.parametrize("sample", ["meta.json", "meta.schema1.json"])
def test_every_request_follows_the_recorded_chrome_wire_order(kind, sample, tmp_path):
    """Ground truth: a real Chrome recorded with --log-net-log (names only). Whatever
    photos-mcp sends for a kind is a subsequence of that order."""
    from photos_mcp.transport import headers_for

    recorded = json.loads((SESSION_FIXTURE.parent / "chrome_wire_order.json").read_text())[kind]
    target = tmp_path / "s"
    target.mkdir()
    shutil.copy(SESSION_FIXTURE / "cookies.txt", target / "cookies.txt")
    shutil.copy(SESSION_FIXTURE / sample, target / "meta.json")
    snapshot = read_snapshot(target)
    sent = [name for name, _ in headers_for(snapshot, kind, "POST" if kind == "rpc" else "GET", "SID=x")]
    assert set(sent) <= set(recorded), set(sent) - set(recorded)
    assert sent == [name for name in recorded if name in sent]
