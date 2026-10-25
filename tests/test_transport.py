"""The transport against a scripted sender: one test per error class, plus the policy
(allowlists, redirects, retries, pacing, budgets, breaker, generation reload)."""

import json
import threading
from urllib.parse import parse_qs

import pytest
from conftest import (
    BATCH,
    GLOBALS,
    PAGE,
    FakeSender,
    app_page,
    make_image,
    make_transport,
    ok,
    redirect,
    rpc_body,
    status,
    wrb,
    write_meta,
)

from photos_mcp import transport
from photos_mcp.errors import PhotosError
from photos_mcp.transport import NetworkError, host_allowed
from photos_mcp.vendor.gpwc import payloads

FIFE = ("GET", "photos.fife.usercontent.google.com/pw/X=w256-h256-k-no")
FIFE_URL = "https://photos.fife.usercontent.google.com/pw/X=w256-h256-k-no"


def lcxim(size=1):
    return payloads.GetLibraryPageByTakenDate(source="library", page_size=size)


def good_batch(_m=None, _u=None, _p=None, _d=None):
    return ok(rpc_body(wrb("lcxiM", [[], None])))


def code_of(excinfo) -> str:
    return excinfo.value.code


def test_bootstrap_then_rpc_sends_the_verified_shape(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: good_batch()}, clock)
    (result,) = t.rpc([lcxim(3)])
    assert result.rpcid == "lcxiM" and result.data == [[], None]
    page, post = sender.requests
    assert page["method"] == "GET" and page["url"] == "https://photos.google.com/"
    assert "OSID=fake-photos-osid" in page["headers"]["cookie"]
    assert "fake-fife-osid" not in page["headers"]["cookie"]
    assert post["params"] == {
        "rpcids": "lcxiM",
        "source-path": "/",
        "f.sid": GLOBALS["FdrFJe"],
        "bl": GLOBALS["cfb2h"],
        "rt": "c",
    }
    form = parse_qs(post["data"])
    assert form["at"] == [GLOBALS["SNlM0e"]]
    assert json.loads(form["f.req"][0]) == [[["lcxiM", "[null,null,3,null,1,1]", None, "generic"]]]
    assert post["headers"]["content-type"].startswith("application/x-www-form-urlencoded")
    # Globals are cached per generation: a second RPC does not reload the page.
    t.rpc([lcxim()])
    assert sender.hosts().count("GET photos.google.com/") == 1


def test_multi_envelope_chunk_is_fully_read(tmp_path, clock):
    body = rpc_body(wrb("fDcn4b", [["ext"]], "1"), wrb("VrseUb", [["info"]], "2"))
    t, _ = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: ok(body)}, clock)
    results = t.rpc([payloads.GetItemInfoExt("AF1QipFAKE01"), payloads.GetItemInfo("AF1QipFAKE01")])
    assert [(r.rpcid, r.data) for r in results] == [("fDcn4b", [["ext"]]), ("VrseUb", [["info"]])]


def test_sign_in_redirect_is_session_expired_and_not_retried_until_a_new_generation(tmp_path, clock):
    routes = {PAGE: redirect("https://accounts.google.com/ServiceLogin?continue=https://photos.google.com/")}
    t, sender = make_transport(tmp_path, routes, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "session_expired"
    with pytest.raises(PhotosError) as again:
        t.rpc([lcxim()])
    assert code_of(again) == "session_expired"
    assert len(sender.requests) == 1  # the dead generation is not re-hit
    # A newer generation is tried again (and works).
    write_meta(tmp_path / "session", 8)
    sender.routes[PAGE] = ok(app_page(), "text/html")
    sender.routes[BATCH] = good_batch()
    assert t.rpc([lcxim()])[0].data == [[], None]


def test_dead_generation_survives_a_restart(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: redirect("https://www.google.com/photos/about/")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "session_expired"
    t2, sender2 = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html")}, clock)
    with pytest.raises(PhotosError) as caught:
        t2.rpc([lcxim()])
    assert code_of(caught) == "session_expired" and sender2.requests == []


def test_missing_gd_on_a_normal_page_is_retryable_and_keeps_the_generation(tmp_path, clock):
    # Only a redirect to sign-in or /photos/about proves a signed-out session (README "Liveness").
    t, _ = make_transport(tmp_path, {PAGE: ok(b"<html><body>Welcome</body></html>", "text/html")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "upstream_error"
    assert t.state.dead_code(t.reader.current().generation) is None


def test_a_bare_401_reports_session_expired_without_killing_the_generation(tmp_path, clock):
    t, _ = make_transport(tmp_path, {PAGE: status(401)}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "session_expired"
    assert t.state.dead_code(t.reader.current().generation) is None


def test_consent_redirect(tmp_path, clock):
    t, _ = make_transport(tmp_path, {PAGE: redirect("https://consent.google.com/ml?continue=x")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "consent_required"
    assert t.state.dead_code(t.reader.current().generation) is None  # reported, not fatal


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("https://www.google.com/sorry/index?continue=x", "blocked"),
        ("https://www.google.de/sorry/index", "blocked"),
        ("https://www.google.co.uk/sorry/index", "blocked"),
        ("https://www.google.com.au/sorry/index", "blocked"),
        ("https://google.fr/sorry/", "blocked"),
        ("https://notgoogle.com/sorry/index", None),
        ("https://www.google.com.evil.example/sorry/index", None),
        ("https://www.google.de/search", None),
    ],
)
def test_the_block_page_is_recognised_on_every_google_domain(location, code):
    assert transport._redirect_code(location) == code


def test_sorry_redirect_trips_the_persisted_breaker_and_escalates(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: redirect("https://www.google.com/sorry/index?continue=x")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "blocked"
    with pytest.raises(PhotosError) as again:
        t.rpc([lcxim()])
    assert code_of(again) == "blocked" and len(sender.requests) == 1  # no Google call while open
    described = t.state.describe()["breaker"]
    assert described["open"] and described["level"] == 1
    # A restarted server still honours it.
    t2, sender2 = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html")}, clock)
    with pytest.raises(PhotosError):
        t2.rpc([lcxim()])
    assert sender2.requests == []
    # After 30 min it closes; the next block within a day escalates to 2 h.
    clock.now += 30 * 60 + 1
    sender.routes[PAGE] = status(429)
    with pytest.raises(PhotosError) as third:
        t.rpc([lcxim()])
    assert code_of(third) == "blocked"
    assert t.state.describe()["breaker"]["level"] == 2
    assert t.state._data["breaker"]["until"] == pytest.approx(clock.now + 2 * 3600)


def test_captcha_page_is_blocked(tmp_path, clock):
    body = b"<html><body><div class='g-recaptcha'></div>Our systems have detected unusual traffic</body></html>"
    t, _ = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: ok(body, "text/html")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "blocked"


def test_html_instead_of_rpc_is_retryable_and_keeps_the_generation(tmp_path, clock):
    t, _ = make_transport(
        tmp_path,
        {PAGE: ok(app_page(), "text/html"), BATCH: ok(b"<!DOCTYPE html><html>Sign in</html>", "text/html")},
        clock,
    )
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "upstream_error"
    assert t.state.dead_code(t.reader.current().generation) is None


def test_stale_token_rebuilds_globals_once_and_retries_once(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: [status(400), good_batch()]}, clock)
    assert t.rpc([lcxim()])[0].data == [[], None]
    assert sender.hosts() == [
        "GET photos.google.com/",
        "POST photos.google.com/_/PhotosUi/data/batchexecute",
        "GET photos.google.com/",
        "POST photos.google.com/_/PhotosUi/data/batchexecute",
    ]


def test_stale_token_twice_is_reported(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: status(400)}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "stale_token"
    assert len(sender.requests) == 4


def test_all_er_envelopes_are_schema_changed(tmp_path, clock):
    body = rpc_body(["er", None, None, None, None, 400, None, None, None, 3])
    t, _ = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: ok(body)}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "schema_changed"


def test_no_envelope_and_undecodable_data_are_schema_changed(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: ok(b")]}'\n\n[]\n")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "schema_changed"
    sender.routes[BATCH] = ok(rpc_body(["wrb.fr", "lcxiM", "{not json", None, None, None, "generic"]))
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "schema_changed"


def test_5xx_and_network_errors_retry_twice_then_upstream_error(tmp_path, clock):
    t, sender = make_transport(
        tmp_path,
        {PAGE: ok(app_page(), "text/html"), BATCH: [status(503), NetworkError("Timeout"), good_batch()]},
        clock,
    )
    assert t.rpc([lcxim()])[0].data == [[], None]
    assert len(sender.requests) == 4
    sender.routes[BATCH] = status(502)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "upstream_error"
    assert len(sender.requests) == 4 + 3  # 1 + 2 retries
    # 4xx other than 400/401/403/429 is not retried.
    sender.routes[BATCH] = status(404)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "upstream_error" and len(sender.requests) == 8


def test_requests_are_paced(tmp_path, clock):
    t, _ = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: good_batch()}, clock)
    t.rpc([lcxim()])
    t.rpc([lcxim()])
    # rand=0.5 → 350 ms + 0.5 × 150 ms jitter between consecutive requests.
    assert clock.sleeps == [0.425, 0.425]


def test_busy_after_the_queue_wait(tmp_path, clock):
    t, _ = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: good_batch()}, clock, queue_wait=0.05)
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with t.pacer.slot():
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(5)
    try:
        with pytest.raises(PhotosError) as caught:
            t.rpc([lcxim()])
        assert code_of(caught) == "busy" and caught.value.payload()["retryable"] is True
    finally:
        release.set()
        thread.join()


def test_daily_rpc_budget(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: good_batch()}, clock, rpc_budget=3)
    t.rpc([lcxim()])  # page GET + 1 RPC
    t.rpc([lcxim()])
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "budget_exhausted"
    assert len(sender.requests) == 3
    # Resets at midnight in the budget time zone.
    clock.now += 24 * 3600
    t.rpc([lcxim()])


def test_rpc_allowlist_is_enforced(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: good_batch()}, clock)
    rogue = payloads.GetSearchPage("x")
    rogue.rpcid = "XwAOJf"  # trash
    with pytest.raises(RuntimeError):
        t.rpc([rogue])
    assert sender.requests == []


def test_account_mismatch_from_meta_and_from_the_page(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html")}, clock)
    write_meta(tmp_path / "session", 9, email="someone-else@example.com")
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "account_mismatch" and sender.requests == []
    write_meta(tmp_path / "session", 10)
    sender.routes[PAGE] = ok(app_page({**GLOBALS, "oPEP7c": "intruder@example.com"}), "text/html")
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "account_mismatch"
    with pytest.raises(PhotosError) as again:
        t.rpc([lcxim()])
    assert code_of(again) == "account_mismatch" and len(sender.requests) == 1


def test_missing_snapshot_is_session_unavailable(tmp_path, clock):
    t, sender = make_transport(tmp_path, {}, clock)
    (tmp_path / "session" / "meta.json").unlink()
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "session_unavailable" and sender.requests == []


def test_generation_reload_uses_new_cookies_and_refetches_globals(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: good_batch()}, clock)
    t.rpc([lcxim()])
    session = tmp_path / "session"
    text = (session / "cookies.txt").read_text().replace("fake-sid-value", "rotated-sid")
    (session / "cookies.txt").write_text(text)
    write_meta(session, 8)
    t.rpc([lcxim()])
    assert sender.hosts().count("GET photos.google.com/") == 2
    assert "SID=rotated-sid" in sender.requests[-1]["headers"]["cookie"]


def test_set_cookie_updates_apply_to_later_requests(tmp_path, clock):
    page = ok(app_page(), "text/html", set_cookies=["SIDCC=fresh; domain=.google.com; path=/; Max-Age=600"])
    t, sender = make_transport(tmp_path, {PAGE: page, BATCH: good_batch()}, clock)
    t.rpc([lcxim()])
    assert "SIDCC=fresh" in sender.requests[-1]["headers"]["cookie"]


# -- images ---------------------------------------------------------------------------


def test_image_fetch_sends_the_fife_cookies(tmp_path, clock):
    jpeg = make_image(64, 48, "JPEG")
    t, sender = make_transport(tmp_path, {FIFE: ok(jpeg, "image/jpeg")}, clock)
    assert t.fetch_image(FIFE_URL) == jpeg
    cookie = sender.requests[0]["headers"]["cookie"]
    assert "OSID=fake-fife-osid" in cookie and "fake-photos-osid" not in cookie
    assert t.state.describe()["budgets"]["thumb_left"] == 1999
    assert t.state.describe()["budgets"]["rpc_left"] == 3000  # no app page needed for images


def test_image_redirects_inside_the_allowlist_are_followed(tmp_path, clock):
    jpeg = make_image(10, 10, "JPEG")
    routes = {
        FIFE: redirect("https://lh3.googleusercontent.com/abc=w256"),
        ("GET", "lh3.googleusercontent.com/abc=w256"): ok(jpeg, "image/jpeg"),
    }
    t, sender = make_transport(tmp_path, routes, clock)
    assert t.fetch_image(FIFE_URL) == jpeg


def test_image_redirect_outside_the_allowlist_is_not_followed(tmp_path, clock):
    t, sender = make_transport(tmp_path, {FIFE: redirect("https://evil.example.com/x.jpg")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.fetch_image(FIFE_URL)
    assert code_of(caught) == "item_unavailable" and len(sender.requests) == 1


@pytest.mark.parametrize("target", ["https://accounts.google.com/ServiceLogin", "https://consent.google.com/ml"])
def test_image_sign_in_redirect_is_media_session_required_and_keeps_the_session(tmp_path, clock, target):
    t, sender = make_transport(tmp_path, {FIFE: redirect(target), PAGE: ok(app_page(), "text/html")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.fetch_image(FIFE_URL)
    assert code_of(caught) == "media_session_required"
    assert t.state.dead_code(7) is None


@pytest.mark.parametrize("code", [403, 404])
def test_image_403_404_is_item_unavailable(tmp_path, clock, code):
    t, _ = make_transport(tmp_path, {FIFE: status(code)}, clock)
    with pytest.raises(PhotosError) as caught:
        t.fetch_image(FIFE_URL)
    assert code_of(caught) == "item_unavailable"


def test_image_body_cap(tmp_path, clock):
    big = b"\xff\xd8\xff" + b"0" * (15 * 1024 * 1024)
    t, _ = make_transport(tmp_path, {FIFE: ok(big, "image/jpeg")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.fetch_image(FIFE_URL)
    assert code_of(caught) == "item_unavailable"


def test_daily_thumbnail_budget(tmp_path, clock):
    t, _ = make_transport(tmp_path, {FIFE: ok(make_image(4, 4, "JPEG"), "image/jpeg")}, clock, thumb_budget=1)
    t.fetch_image(FIFE_URL)
    with pytest.raises(PhotosError) as caught:
        t.fetch_image(FIFE_URL)
    assert code_of(caught) == "budget_exhausted"


@pytest.mark.parametrize(
    "url",
    [
        "http://photos.fife.usercontent.google.com/pw/X",
        "https://evil.example.com/pw/X",
        "https://photos.google.com.evil.example/x",
        "https://lh10.googleusercontent.com/x",
        "https://user@photos.fife.usercontent.google.com/pw/X",
        "https://photos.fife.usercontent.google.com:8443/pw/X",
    ],
)
def test_host_allowlist_rejects(url, tmp_path, clock):
    assert not host_allowed(url)
    t, sender = make_transport(tmp_path, {}, clock)
    with pytest.raises(PhotosError):
        t.fetch_image(url)
    assert sender.requests == []


def test_host_allowlist_accepts():
    for url in (
        "https://photos.google.com/",
        "https://photos.fife.usercontent.google.com/pw/X",
        "https://lh3.googleusercontent.com/x",
    ):
        assert host_allowed(url)


def test_rpc_never_follows_a_redirect(tmp_path, clock):
    t, sender = make_transport(
        tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: redirect("https://photos.google.com/elsewhere")}, clock
    )
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "upstream_error"  # a redirect inside Google proves nothing
    assert sender.hosts()[-1] == "POST photos.google.com/_/PhotosUi/data/batchexecute"


def test_logs_carry_no_urls_or_secrets(tmp_path, clock, caplog):
    caplog.set_level("INFO")
    t, _ = make_transport(tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: good_batch()}, clock)
    t.rpc([lcxim()])
    text = " ".join(f"{r.getMessage()} {getattr(r, 'extras', '')}" for r in caplog.records)
    for secret in ("fake-sid-value", GLOBALS["SNlM0e"], GLOBALS["FdrFJe"], "f.sid", "https://"):
        assert secret not in text


def test_sender_is_built_with_the_snapshot(tmp_path, clock):
    snapshots = []
    sender = FakeSender({PAGE: ok(app_page(), "text/html"), BATCH: good_batch()})
    t, _ = make_transport(tmp_path, {}, clock)
    t._sender_factory = lambda snapshot: snapshots.append(snapshot) or sender
    t.rpc([lcxim()])
    assert snapshots and snapshots[0].chromium_major == 149 and "Chrome/149" in snapshots[0].user_agent


@pytest.mark.parametrize(
    "location",
    [
        "https://www.google.com/photos/about/",
        "https://www.google.com/photos/about/?hl=en&continue=https://photos.google.com/",
        "https://google.com/photos/about",
        "https://accounts.google.com/ServiceLogin?service=lho&continue=https://photos.google.com/",
        "https://accounts.google.com/v3/signin/identifier?continue=https://photos.google.com/",
    ],
)
def test_signed_out_redirects_are_session_expired_and_kill_the_generation(tmp_path, clock, location):
    t, sender = make_transport(tmp_path, {PAGE: redirect(location)}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "session_expired"
    assert t.state.dead_code(7) == "session_expired"
    with pytest.raises(PhotosError):
        t.rpc([lcxim()])
    assert len(sender.requests) == 1  # never re-hit with the dead cookies


def test_other_google_redirects_are_not_signed_out(tmp_path, clock):
    t, _ = make_transport(tmp_path, {PAGE: redirect("https://www.google.com/search?q=x")}, clock)
    with pytest.raises(PhotosError) as caught:
        t.rpc([lcxim()])
    assert code_of(caught) == "upstream_error" and t.state.dead_code(7) is None


def test_each_request_kind_replays_its_template(tmp_path, clock):
    jpeg = make_image(8, 8, "JPEG")
    t, sender = make_transport(
        tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: good_batch(), FIFE: ok(jpeg, "image/jpeg")}, clock
    )
    t.rpc([lcxim()])
    t.fetch_image(FIFE_URL)
    page, post, image = (r["headers"] for r in sender.requests)
    assert (page["sec-fetch-mode"], post["sec-fetch-mode"], image["sec-fetch-mode"]) == ("navigate", "cors", "no-cors")
    assert post["x-same-domain"] == "1" and "x-same-domain" not in page
    assert image["sec-fetch-dest"] == "image" and "referer" not in page
    assert post["referer"] == image["referer"] == "https://photos.google.com/"
    assert 'v="149"' in page["sec-ch-ua"] and "Chrome/149" in image["user-agent"]


def test_signed_out_probe_sends_no_cookie_and_follows_nothing(tmp_path, clock):
    t, sender = make_transport(tmp_path, {PAGE: redirect("https://www.google.com/photos/about/")}, clock)
    response = t.probe_page(with_cookies=False)
    assert response.status == 302 and "photos/about" in response.headers["location"]
    (request,) = sender.requests
    assert "cookie" not in request["headers"]
    assert t.state.dead_code(7) is None  # a probe never classifies
    assert t.state.describe()["budgets"]["rpc_left"] == 2999


def test_image_fetches_run_up_to_four_at_a_time(tmp_path, clock):
    from concurrent.futures import ThreadPoolExecutor

    active, peak = [0], [0]
    lock = threading.Lock()
    first_batch = threading.Event()
    release = threading.Event()
    jpeg = make_image(8, 8, "JPEG")

    def slow(*_):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            if active[0] == 4:
                first_batch.set()
        try:
            assert release.wait(10), "the test did not release the image responses"
        finally:
            with lock:
                active[0] -= 1
        return ok(jpeg, "image/jpeg")

    t, _ = make_transport(tmp_path, {("GET", "photos.fife.usercontent.google.com"): slow}, clock)
    t.image_pacer.sleep = lambda s: None  # spacing is covered by the pacer tests
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(t.fetch_image, f"https://photos.fife.usercontent.google.com/pw/X{i}") for i in range(12)]
        try:
            # Observe four active requests without relying on a scheduling gap.
            assert first_batch.wait(10), "four image requests did not overlap"
        finally:
            release.set()
        assert all(future.result(timeout=10) == jpeg for future in futures)
    assert peak[0] == 4


def test_concurrent_pacer_spaces_starts():
    from photos_mcp.transport import Pacer

    now = [0.0]
    sleeps = []
    pacer = Pacer(concurrency=4, min_interval=0.1, jitter=0.0, sleep=sleeps.append, monotonic=lambda: now[0])
    for _ in range(3):
        with pacer.slot():
            pacer.space()  # nothing finishes: the starts are still 100 ms apart
    assert [round(s, 3) for s in sleeps] == [0.1, 0.2]
