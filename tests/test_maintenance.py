from __future__ import annotations

import json
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

if sys.platform != "linux":
    pytest.skip("optional maintenance requires Linux", allow_module_level=True)

from signin_window.automation.auth import (
    AuthFailureCategory as MintFailureCategory,
)
from signin_window.automation.auth import (
    AuthFailureEvidence as MintFailureEvidence,
)
from signin_window.automation.auth import (
    AuthFailureStage as MintFailureStage,
)
from signin_window.automation.auth import (
    AuthFlowFailure as AcquisitionFailure,
)
from signin_window.automation.auth import (
    AuthOutcome as MintOutcome,
)
from signin_window.leases import LeaseBusyError
from signin_window.sessions import AttemptBudget

from photos_maintenance.browser import GoogleTargetBrowser, _WireHeaderRecorder
from photos_maintenance.config import load_identity_target
from photos_maintenance.keeper import IdentityKeeper, IdentityRunResult
from photos_maintenance.models import PersistenceError, ProviderReadiness
from photos_maintenance.models import Requirement as LoginRequirement
from photos_maintenance.outcomes import (
    AccountMismatch,
    BlockReason,
    HoldReason,
    IdentityOutcome,
    ProviderBlocked,
    SessionDead,
    SignInHold,
    VerifyFailed,
    VerifyFailure,
)
from photos_maintenance.page import parse_wiz
from photos_maintenance.snapshot import (
    COOKIES_FILE,
    HEADER_KINDS,
    META_FILE,
    ExportCookie,
    SnapshotError,
    SnapshotMeta,
    filter_header_template,
    parse_cookies_txt,
    publish_snapshot,
    published_generation,
    render_cookies_txt,
    render_meta,
    select_export_cookies,
    stage_snapshot,
    validate_header_templates,
)
from photos_maintenance.state import TargetStateStore


def CredentialBudget(root, **kwargs):
    return AttemptBudget(root, error_type=PersistenceError, **kwargs)


FIXTURES = Path(__file__).parent / "fixtures" / "identity_snapshot"

EMAIL = "owner@example.com"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

SEC_CH_UA = '"Chromium";v="149", "Not)A;Brand";v="24"'

ACCEPT_LANGUAGE = "en-US,en;q=0.9"

HEADERS: dict[str, tuple[tuple[str, str], ...]] = {
    "page": (
        ("sec-ch-ua", SEC_CH_UA),
        ("sec-ch-ua-mobile", "?0"),
        ("sec-ch-ua-platform", '"Linux"'),
        ("upgrade-insecure-requests", "1"),
        ("user-agent", UA),
        (
            "accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        ),
        ("sec-fetch-site", "none"),
        ("sec-fetch-mode", "navigate"),
        ("sec-fetch-user", "?1"),
        ("sec-fetch-dest", "document"),
        ("accept-language", ACCEPT_LANGUAGE),
        ("priority", "u=0, i"),
    ),
    "rpc": (
        ("sec-ch-ua-platform", '"Linux"'),
        ("x-same-domain", "1"),
        ("user-agent", UA),
        ("sec-ch-ua", SEC_CH_UA),
        ("content-type", "application/x-www-form-urlencoded;charset=UTF-8"),
        ("sec-ch-ua-mobile", "?0"),
        ("accept", "*/*"),
        ("origin", "https://photos.google.com"),
        ("sec-fetch-site", "same-origin"),
        ("sec-fetch-mode", "cors"),
        ("sec-fetch-dest", "empty"),
        ("accept-language", ACCEPT_LANGUAGE),
        ("priority", "u=1, i"),
    ),
    "image": (
        ("sec-ch-ua-platform", '"Linux"'),
        ("user-agent", UA),
        ("sec-ch-ua", SEC_CH_UA),
        ("sec-ch-ua-mobile", "?0"),
        ("accept", "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"),
        ("sec-fetch-site", "same-site"),
        ("sec-fetch-mode", "no-cors"),
        ("sec-fetch-dest", "image"),
        ("accept-language", ACCEPT_LANGUAGE),
        ("priority", "i"),
    ),
}

NOW = datetime(2026, 10, 14, 3, 0, tzinfo=UTC)

_DRIVER_CONTROLS = (
    "account_choices",
    "alternate",
    "consent",
    "email",
    "email_submit",
    "password",
    "password_choice",
    "password_submit",
    "totp",
    "totp_choice",
    "totp_submit",
    "use_another",
)


def _fixture_cookies() -> list[ExportCookie]:
    spec = load_identity_target("google")
    raw = json.loads((FIXTURES / "browser_cookies.json").read_text())
    return select_export_cookies(raw, hosts=spec.export_hosts, names=spec.export_names)


def _fixture_meta(generation: int = 7) -> SnapshotMeta:
    return SnapshotMeta(
        generation=generation,
        email=EMAIL,
        user_agent=UA,
        chromium_major=149,
        verified_at=NOW,
        headers=HEADERS,
    )


def test_exporter_output_matches_the_photos_mcp_contract_sample() -> None:
    assert render_cookies_txt(_fixture_cookies()) == (FIXTURES / "cookies.txt").read_bytes()
    assert render_meta(_fixture_meta()) == (FIXTURES / "meta.json").read_bytes()
    # The compact layout is still exactly the JSON the meta describes.
    assert json.loads(render_meta(_fixture_meta())) == _fixture_meta().to_json()


def test_sample_meta_has_exactly_the_contract_keys() -> None:
    meta = json.loads((FIXTURES / "meta.json").read_text())

    assert list(meta) == [
        "schema",
        "generation",
        "email",
        "account_index",
        "user_agent",
        "chromium_major",
        "verified_at",
        "headers",
    ]
    assert meta["schema"] == 2
    assert meta["generation"] == 7
    assert meta["email"] == EMAIL
    assert meta["chromium_major"] == 149
    assert list(meta["headers"]) == ["page", "rpc", "image"]
    assert meta["account_index"] == 0
    assert meta["verified_at"].endswith("Z")


def _with(kind: str, *pairs: tuple[str, str]) -> dict[str, Any]:
    headers: dict[str, Any] = dict(HEADERS)
    headers[kind] = tuple(pairs)
    return headers


@pytest.mark.parametrize(
    "headers",
    [
        {k: v for k, v in HEADERS.items() if k != "image"},
        _with("page", *[p for p in HEADERS["page"] if p[0] != "user-agent"]),
        _with("page", *HEADERS["page"][:-1], ("user-agent", UA + " x")),
        _with("rpc", *HEADERS["rpc"], ("cookie", "SID=1")),
        _with("image", *HEADERS["image"], ("referer", "https://photos.google.com/")),
        _with("page", *HEADERS["page"], ("content-type", "text/html")),
        _with("page", *HEADERS["page"], ("accept-language", "en")),
        _with("image", *HEADERS["image"], ("sec-ch-ua-arch", "x86\nInjected: 1")),
        _with("image", *HEADERS["image"], ("sec-ch-ua-model", "x" * 513)),
        _with(
            "image",
            *HEADERS["image"],
            *[(f"sec-ch-ua-x{n}", "1") for n in range(30)],
        ),
        _with(
            "rpc",
            *[p for p in HEADERS["rpc"] if p[0] != "sec-fetch-dest"],
            ("sec-fetch-dest", "document"),
        ),
        _with("page"),
        *[
            _with(kind, *[p for p in HEADERS[kind] if p[0] != name])
            for kind in HEADER_KINDS
            for name in ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest")
        ],
        _with(
            "image",
            *[p for p in HEADERS["image"] if p[0] != "sec-fetch-dest"],
            ("sec-fetch-dest", "empty"),
        ),
    ],
)
def test_header_templates_enforce_the_schema_2_contract(
    headers: dict[str, Any],
) -> None:
    with pytest.raises(SnapshotError):
        validate_header_templates(headers, user_agent=UA)
    meta = _fixture_meta()
    with pytest.raises(SnapshotError):
        render_meta(
            SnapshotMeta(
                generation=1,
                email=EMAIL,
                user_agent=UA,
                chromium_major=149,
                verified_at=NOW,
                headers=headers,
            )
        )
    assert render_meta(meta)


def test_chromium_major_must_match_the_user_agent() -> None:
    with pytest.raises(SnapshotError):
        render_meta(
            SnapshotMeta(
                generation=1,
                email=EMAIL,
                user_agent=UA,
                chromium_major=141,
                verified_at=NOW,
                headers=HEADERS,
            )
        )


def test_header_filter_keeps_order_and_drops_everything_unlisted() -> None:
    raw = [
        (":method", "POST"),
        ("Sec-CH-UA", "a"),
        ("Cookie", "SID=1"),
        ("Content-Type", "application/x-www-form-urlencoded"),
        ("X-Same-Domain", "1"),
        ("sec-ch-ua", "duplicate"),
        ("Authorization", "SAPISIDHASH x"),
        ("Sec-Fetch-Mode", "cors"),
    ]

    assert filter_header_template("rpc", raw) == (
        ("sec-ch-ua", "a"),
        ("content-type", "application/x-www-form-urlencoded"),
        ("x-same-domain", "1"),
        ("sec-fetch-mode", "cors"),
    )
    assert ("content-type", "application/x-www-form-urlencoded") not in (filter_header_template("page", raw))


def test_exporter_maps_host_only_and_filters_names_and_hosts() -> None:
    cookies = _fixture_cookies()
    rows = [
        line.split("\t") for line in (FIXTURES / "cookies.txt").read_text().splitlines() if not line.startswith("#")
    ]

    assert {(c.domain, c.name) for c in cookies} == {
        ("google.com", "SID"),
        ("google.com", "__Secure-1PSID"),
        ("google.com", "NID"),
        ("photos.google.com", "OSID"),
        ("photos.google.com", "__Secure-OSID"),
        ("photos.fife.usercontent.google.com", "OSID"),
        ("photos.fife.usercontent.google.com", "__Secure-OSID"),
    }
    for domain, subdomains, _path, _secure, expires, _name, value in rows:
        assert value.startswith("fake-")
        if domain.startswith("."):
            assert subdomains == "TRUE"
        else:
            assert subdomains == "FALSE"
            assert domain in {
                "photos.google.com",
                "photos.fife.usercontent.google.com",
            }
        assert expires.isdigit()
    session_rows = [row for row in rows if row[0].startswith("photos.fife")]
    assert {row[4] for row in session_rows} == {"0"}
    assert parse_cookies_txt((FIXTURES / "cookies.txt").read_bytes()) == cookies


def test_exporter_rejects_values_that_would_break_rows() -> None:
    cookie = ExportCookie("google.com", False, "/", True, 0, "SID", "a\tb")
    with pytest.raises(SnapshotError):
        render_cookies_txt([cookie])


def test_reader_rejects_a_dot_that_contradicts_the_subdomain_flag() -> None:
    with pytest.raises(SnapshotError):
        parse_cookies_txt(b"photos.google.com\tTRUE\t/\tTRUE\t0\tOSID\tx\n")


def test_publish_writes_cookies_before_meta_and_removes_staging(
    tmp_path: Path,
) -> None:
    export = tmp_path / "google"
    export.mkdir()
    staging = stage_snapshot(export, _fixture_cookies(), _fixture_meta())
    written: list[str] = []

    def record(path: Path, payload: bytes) -> None:
        written.append(path.name)
        path.write_bytes(payload)

    publish_snapshot(export, staging, write=record)

    assert written == [COOKIES_FILE, META_FILE]
    assert not staging.exists()
    sample = (FIXTURES / "cookies.txt").read_bytes()
    assert (export / COOKIES_FILE).read_bytes() == sample
    assert published_generation(export) == 7


def test_published_files_are_private_and_replace_atomically(tmp_path: Path) -> None:
    export = tmp_path / "google"
    export.mkdir()
    for generation in (1, 2):
        staging = stage_snapshot(export, _fixture_cookies(), _fixture_meta(generation))
        publish_snapshot(export, staging)

    assert published_generation(export) == 2
    assert sorted(path.name for path in export.iterdir()) == [COOKIES_FILE, META_FILE]
    for name in (COOKIES_FILE, META_FILE):
        assert stat.S_IMODE((export / name).stat().st_mode) == 0o600


class FakeBrowser:
    def __init__(
        self,
        *,
        warm: list[object] | None = None,
        sign_in: object | None = None,
    ) -> None:
        self.warm_script = list(warm or [EMAIL])
        self.sign_in_effect = sign_in
        self.sign_ins = 0
        self.headers = dict(HEADERS)

    def warm(self) -> str:
        step = self.warm_script.pop(0) if self.warm_script else EMAIL
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, str)
        return step

    def sign_in(self) -> None:
        self.sign_ins += 1
        if isinstance(self.sign_in_effect, BaseException):
            raise self.sign_in_effect

    def export_cookies(self) -> list[ExportCookie]:
        return _fixture_cookies()

    def user_agent(self) -> str:
        return UA

    def request_headers(self) -> dict[str, tuple[tuple[str, str], ...]]:
        return dict(self.headers)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        browser: FakeBrowser | None = None,
        missing: tuple[LoginRequirement, ...] = (),
        lease_busy: bool = False,
    ) -> None:
        self.clock = Clock()
        self.export = tmp_path / "export" / "google"
        self.state = TargetStateStore(tmp_path / "identity-targets", utc_now=self.clock)
        self.budget = CredentialBudget(tmp_path / "session-keeper", utc_now=self.clock)
        self.browser = browser or FakeBrowser()
        self.opened = 0
        self.missing = missing
        self.lease_busy = lease_busy
        self.keeper = IdentityKeeper(
            target="google",
            expected_email=EMAIL,
            export_dir=self.export,
            state=self.state,
            budget=self.budget,
            readiness=lambda: ProviderReadiness(provider="google", missing=self.missing),
            open_browser=self._open,
            lease=self._lease,
            utc_now=self.clock,
        )

    @contextmanager
    def _open(self) -> Iterator[FakeBrowser]:
        self.opened += 1
        yield self.browser

    @contextmanager
    def _lease(self) -> Iterator[None]:
        if self.lease_busy:
            raise LeaseBusyError("busy")
        yield

    def run(self, *, allow: bool = True) -> IdentityRunResult:
        return self.preflight_then_run(allow=allow)

    def preflight_then_run(self, *, allow: bool) -> IdentityRunResult:
        early = self.keeper.preflight()
        if early is not None:
            return early
        return self.keeper.run(allow_credential_mint=allow)


def test_warm_session_publishes_a_verified_generation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    result = harness.run(allow=False)

    assert result.outcome is IdentityOutcome.PUBLISHED
    assert result.generation == 1
    assert result.signed_in is False
    assert result.exit_code == 0
    assert harness.browser.sign_ins == 0
    meta = json.loads((harness.export / META_FILE).read_text())
    assert meta["generation"] == 1
    assert meta["email"] == EMAIL
    assert meta["chromium_major"] == 149
    assert meta["schema"] == 2
    assert meta["headers"] == {k: [list(p) for p in v] for k, v in HEADERS.items()}
    assert meta["verified_at"] == "2026-10-14T03:00:00Z"
    assert not [p for p in harness.export.iterdir() if p.name.startswith(".")]
    assert harness.budget.attempts_today("google") == 0


def test_generation_is_monotonic_across_runs_and_published_state(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    assert harness.run().generation == 1
    assert harness.run().generation == 2
    # A newer published snapshot (e.g. restored export) still moves forward.
    staging = stage_snapshot(harness.export, _fixture_cookies(), _fixture_meta(9))
    publish_snapshot(harness.export, staging)
    assert harness.run().generation == 10
    # A lost export directory never rewinds the generation.
    for path in harness.export.iterdir():
        path.unlink()
    assert harness.run().generation == 11


def test_dead_session_without_permission_pages_and_spends_nothing(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, browser=FakeBrowser(warm=[SessionDead()]))

    result = harness.run(allow=False)

    assert result.outcome is IdentityOutcome.MINT_NOT_PERMITTED
    assert result.exit_code == 1
    assert harness.browser.sign_ins == 0
    assert harness.budget.attempts_today("google") == 0
    assert not (harness.export / META_FILE).exists()


def test_dead_session_on_an_unready_host_does_not_sign_in(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        browser=FakeBrowser(warm=[SessionDead()]),
        missing=(LoginRequirement.OP_SERVICE_TOKEN,),
    )

    result = harness.run()

    assert result.outcome is IdentityOutcome.HOST_NOT_READY
    assert result.exit_code == 1
    assert harness.browser.sign_ins == 0
    assert harness.budget.attempts_today("google") == 0


def test_dead_session_signs_in_once_reserving_budget_first(tmp_path: Path) -> None:
    budget_seen: list[int] = []
    browser = FakeBrowser(warm=[SessionDead(), EMAIL])
    harness = Harness(tmp_path, browser=browser)

    def sign_in() -> None:
        budget_seen.append(harness.budget.attempts_today("google"))
        held = harness.state.load("google").hold
        assert held is not None
        assert held.reason is HoldReason.SIGN_IN_INTERRUPTED
        browser.sign_ins += 1

    browser.sign_in = sign_in  # type: ignore[method-assign]

    result = harness.run()

    assert result.outcome is IdentityOutcome.PUBLISHED
    assert result.signed_in is True
    assert result.generation == 1
    assert budget_seen == [1]
    assert harness.state.load("google").hold is None


def test_spent_budget_pages_once_the_snapshot_is_stale(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert harness.run().outcome is IdentityOutcome.PUBLISHED
    reserved_at = harness.clock.now + timedelta(minutes=30)
    harness.clock.now = reserved_at
    assert harness.budget.reserve("google", limit=1)
    harness.browser.warm_script = [SessionDead(), SessionDead()]

    fresh = harness.run()

    assert fresh.outcome is IdentityOutcome.BUDGET_EXHAUSTED
    assert fresh.snapshot_age_s == 30 * 60
    assert fresh.next_sign_in_at == reserved_at + timedelta(hours=24)
    assert fresh.exit_code == 0
    assert harness.browser.sign_ins == 0

    # The same answer pages once the snapshot is older than three hours.
    harness.clock.now = NOW + timedelta(hours=3, seconds=1)
    stale = harness.run()

    assert stale.outcome is IdentityOutcome.BUDGET_EXHAUSTED
    assert stale.snapshot_age_s == 3 * 3600 + 1
    assert stale.exit_code == 1
    assert stale.public_dict()["needs_attention"] is True


def test_never_published_always_pages(tmp_path: Path) -> None:
    harness = Harness(tmp_path, browser=FakeBrowser(warm=[SessionDead()]))
    assert harness.budget.reserve("google", limit=1)

    result = harness.run()

    assert result.outcome is IdentityOutcome.BUDGET_EXHAUSTED
    assert result.snapshot_age_s is None
    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("effect", "reason"),
    [
        (SignInHold(HoldReason.CREDENTIAL_REJECTED), HoldReason.CREDENTIAL_REJECTED),
        (SignInHold(HoldReason.ACCOUNT_ALERT), HoldReason.ACCOUNT_ALERT),
        (
            SignInHold(HoldReason.SECOND_FACTOR_UNSUPPORTED),
            HoldReason.SECOND_FACTOR_UNSUPPORTED,
        ),
        (SignInHold(HoldReason.CHALLENGE), HoldReason.CHALLENGE),
        (SignInHold(HoldReason.TIMEOUT), HoldReason.TIMEOUT),
        (RuntimeError("driver crashed"), HoldReason.SIGN_IN_INFRASTRUCTURE),
    ],
)
def test_sign_in_failure_records_a_durable_hold_and_never_retries(
    tmp_path: Path, effect: BaseException, reason: HoldReason
) -> None:
    browser = FakeBrowser(warm=[SessionDead()], sign_in=effect)
    harness = Harness(tmp_path, browser=browser)

    result = harness.run()

    assert result.outcome is IdentityOutcome.HELD
    assert result.reason == reason.value
    assert result.exit_code == 1
    hold = harness.state.load("google").hold
    assert hold is not None and hold.reason is reason
    assert result.hold_id == hold.hold_id

    # A day later the budget has room again, but the hold still stops it.
    harness.clock.now += timedelta(days=2)
    again = harness.run()
    assert again.outcome is IdentityOutcome.HELD
    assert again.hold_id == hold.hold_id
    assert browser.sign_ins == 1
    assert harness.opened == 1

    assert harness.state.clear_hold("google", hold.hold_id)
    browser.warm_script = [EMAIL]
    assert harness.run().outcome is IdentityOutcome.PUBLISHED


def test_still_dead_after_sign_in_is_a_hold_not_a_daily_retry(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, browser=FakeBrowser(warm=[SessionDead(), SessionDead()]))

    result = harness.run()

    assert result.outcome is IdentityOutcome.HELD
    assert result.reason == HoldReason.SESSION_STILL_DEAD.value
    assert result.signed_in is True
    assert result.exit_code == 1
    hold = harness.state.load("google").hold
    assert hold is not None and hold.reason is HoldReason.SESSION_STILL_DEAD
    assert result.hold_id == hold.hold_id

    # Two days later the budget has room again; the hold still stops it.
    harness.clock.now += timedelta(days=2)
    harness.browser.warm_script = [SessionDead()]
    assert harness.run().outcome is IdentityOutcome.HELD
    assert harness.browser.sign_ins == 1
    assert harness.opened == 1


def test_page_that_proves_nothing_never_signs_in(tmp_path: Path) -> None:
    # Only positive signed-out evidence may spend a sign-in. A 5xx, an error
    # page or a slow page is a verify failure that pages without a sign-in.
    harness = Harness(
        tmp_path,
        browser=FakeBrowser(warm=[VerifyFailed(VerifyFailure.PAGE_INVALID)]),
    )

    result = harness.run(allow=True)

    assert result.outcome is IdentityOutcome.VERIFY_FAILED
    assert result.reason == "page_invalid"
    assert result.signed_in is False
    assert harness.browser.sign_ins == 0
    assert harness.budget.attempts_today("google") == 0
    assert harness.state.load("google").hold is None
    assert not (harness.export / META_FILE).exists()


def test_google_pushback_records_escalating_cooldowns(tmp_path: Path) -> None:
    browser = FakeBrowser(
        warm=[
            ProviderBlocked(BlockReason.SORRY_PAGE),
            ProviderBlocked(BlockReason.RATE_LIMITED),
            ProviderBlocked(BlockReason.CAPTCHA),
            ProviderBlocked(BlockReason.SORRY_PAGE),
        ]
    )
    harness = Harness(tmp_path, browser=browser)
    expected_hours = [2, 8, 24, 24]

    for hours in expected_hours:
        result = harness.run()
        assert result.outcome is IdentityOutcome.BLOCKED
        # Never published: every answer pages until a snapshot exists.
        assert result.exit_code == 1
        assert result.cooldown_until == harness.clock.now + timedelta(hours=hours)
        waiting = harness.run()
        assert waiting.outcome is IdentityOutcome.COOLDOWN_ACTIVE
        assert waiting.exit_code == 1
        harness.clock.now += timedelta(hours=hours, minutes=1)

    assert browser.sign_ins == 0
    assert harness.opened == 4
    assert not (harness.export / META_FILE).exists()
    assert harness.run().outcome is IdentityOutcome.PUBLISHED
    assert harness.state.load("google").consecutive_blocks == 0


@pytest.mark.parametrize("where", ["browser", "keeper"])
def test_account_mismatch_is_a_hold(tmp_path: Path, where: str) -> None:
    # The keeper re-checks the browser's answer before it stages anything.
    step = AccountMismatch() if where == "browser" else "other@example.com"
    harness = Harness(tmp_path, browser=FakeBrowser(warm=[step]))

    result = harness.run()

    assert result.outcome is IdentityOutcome.HELD
    assert result.reason == HoldReason.ACCOUNT_MISMATCH.value
    assert result.exit_code == 1
    assert harness.browser.sign_ins == 0
    assert not (harness.export / META_FILE).exists()


def test_verify_failure_keeps_the_previous_generation(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        browser=FakeBrowser(warm=[EMAIL, VerifyFailed(VerifyFailure.IMAGE_RESPONSE_MISSING)]),
    )
    assert harness.run().generation == 1

    result = harness.run()

    assert result.outcome is IdentityOutcome.VERIFY_FAILED
    assert result.reason == "image_response_missing"
    # Paging follows freshness, not the outcome: the snapshot is still fresh.
    assert result.exit_code == 0
    assert published_generation(harness.export) == 1
    harness.clock.now += timedelta(hours=4)
    harness.browser.warm_script = [VerifyFailed(VerifyFailure.PAGE_INVALID)]
    assert harness.run().exit_code == 1


def test_missing_or_invalid_headers_publish_nothing_and_never_sign_in(
    tmp_path: Path,
) -> None:
    browser = FakeBrowser()
    browser.headers = {k: v for k, v in HEADERS.items() if k != "rpc"}
    harness = Harness(tmp_path, browser=browser)

    result = harness.run(allow=True)

    assert result.outcome is IdentityOutcome.VERIFY_FAILED
    assert result.reason == "headers_missing"
    assert browser.sign_ins == 0
    assert not (harness.export / META_FILE).exists()


def test_busy_lease_with_a_fresh_snapshot_is_a_non_paging_skip(tmp_path: Path) -> None:
    harness = Harness(tmp_path, lease_busy=True)
    harness.state.record_publish("google", 1)

    result = harness.run()

    assert result.outcome is IdentityOutcome.SKIPPED_LEASE_BUSY
    assert result.snapshot_age_s == 0
    assert result.exit_code == 0
    assert harness.opened == 0


def test_browser_failure_is_infrastructure(tmp_path: Path) -> None:
    harness = Harness(tmp_path, browser=FakeBrowser(warm=[RuntimeError("boom")]))

    result = harness.run()

    assert result.outcome is IdentityOutcome.INFRASTRUCTURE_ERROR
    assert result.reason == "unclassified"
    assert result.exit_code == 1


def _gd_page(email: str = EMAIL) -> str:
    wiz = {
        "oPEP7c": email,
        "FdrFJe": "-123",
        "cfb2h": "synthetic-build-label",
        "SNlM0e": "xsrf:token",
        "Im6cmf": "/_/PhotosUi",
    }
    return '<html><script data-id="_gd" nonce="n">window.WIZ_global_data = ' + json.dumps(wiz) + ";</script></html>"


def test_wiz_parser_needs_the_signed_in_script() -> None:
    assert parse_wiz("<html>window.WIZ_global_data = {};</html>") is None
    assert parse_wiz(_gd_page())["oPEP7c"] == EMAIL  # type: ignore[index]
    # Unparseable assignment still yields keys through the per-key fallback.
    broken = _gd_page().replace(";</script>", ",broken;</script>")
    assert parse_wiz(broken)["SNlM0e"] == "xsrf:token"  # type: ignore[index]


class FakePage:
    def __init__(self, browser: FakeMintContext) -> None:
        self._browser = browser

    def content(self) -> str:
        return self._browser.html


class FakeMintContext:
    def __init__(self, urls: list[str], html: str = "") -> None:
        self.urls = urls
        self.html = html
        self.page = FakePage(self)
        self.clicks = 0
        self.navigations: list[str] = []

    @property
    def current_url(self) -> str:
        return self.urls[0]

    def click_optional(self, selectors: tuple[str, ...]) -> bool:
        self.clicks += 1
        if len(self.urls) > 1:
            self.urls.pop(0)
        return True

    def wait(self, seconds: float) -> None:
        return None

    def navigate_committed(self, url: str, *, timeout_seconds: float) -> None:
        self.navigations.append(url)
        self.urls.insert(0, url)


class FakeDriver:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.resolved_first: list[str] = []

    def drive(self, browser: Any, *, callback_complete: Any, resolve_challenge: Any):
        resolve_challenge(browser)
        self.resolved_first.append(browser.current_url)
        if self.error is not None:
            raise self.error
        browser.urls.insert(0, "https://photos.google.com/")
        browser.html = _gd_page()
        assert callback_complete()

        class Result:
            callback_complete = True

        return Result()


def _target_browser(context: FakeMintContext, driver: FakeDriver | None = None) -> GoogleTargetBrowser:
    ticks = iter(range(10_000))
    return GoogleTargetBrowser(
        context,  # type: ignore[arg-type]
        load_identity_target("google"),
        expected_email=EMAIL,
        driver=driver or FakeDriver(),  # type: ignore[arg-type]
        challenge_resolver=object(),  # type: ignore[arg-type]
        monotonic=lambda: float(next(ticks)),
    )


def test_post_login_interstitials_are_skipped_at_most_four_times() -> None:
    pages = [f"https://accounts.google.com/web/prompt/{n}" for n in range(6)]
    context = FakeMintContext([*pages, "https://photos.google.com/"])

    _target_browser(context).skip_interstitials()

    assert context.clicks == 4
    assert context.current_url == pages[4]


def test_interstitial_counts_as_identity_completion() -> None:
    context = FakeMintContext(["https://accounts.google.com/web/profile-picture"])
    assert _target_browser(context)._identity_done()
    context.urls = ["https://accounts.google.com/v3/signin/challenge/pk"]
    assert not _target_browser(context)._identity_done()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://photos.google.com/", None),
        ("https://accounts.google.com/v3/signin/identifier", SessionDead),
        ("https://accounts.google.com/ServiceLogin?continue=x", SessionDead),
        ("https://www.google.com/photos/about/", SessionDead),
        ("https://google.com/photos/about", SessionDead),
        ("https://www.google.com/photos/about?hl=en", SessionDead),
        # Other Google hosts prove nothing about the session.
        ("https://myaccount.google.com/", VerifyFailed),
        ("https://consent.google.com/ml?continue=x", VerifyFailed),
        ("https://gds.google.com/web/landing", VerifyFailed),
        ("https://www.google.com/", VerifyFailed),
        ("https://mail.google.com/photos/about", VerifyFailed),
        ("https://www.google.com/sorry/index", ProviderBlocked),
        # An interstitial that did not go away proves nothing either way.
        ("https://accounts.google.com/web/profile-picture", VerifyFailed),
        ("https://photos.fife.usercontent.google.com/x", VerifyFailed),
        ("https://example.com/", VerifyFailed),
        ("chrome-error://chromewebdata/", VerifyFailed),
        ("about:blank", VerifyFailed),
    ],
)
def test_only_positive_signed_out_evidence_is_a_dead_session(url: str, expected: type[Exception] | None) -> None:
    browser = _target_browser(FakeMintContext([url]))
    if expected is None:
        browser._classify_location()
        return
    with pytest.raises(expected):
        browser._classify_location()


def test_sign_in_enters_the_login_url_after_driver_preparation() -> None:
    driver = FakeDriver()
    context = FakeMintContext(["https://accounts.google.com/v3/signin/identifier"])

    _target_browser(context, driver).sign_in()

    assert context.navigations[0] == "about:blank"
    assert driver.resolved_first == [load_identity_target("google").login_url]


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (MintOutcome.CREDENTIAL_REJECTED, HoldReason.CREDENTIAL_REJECTED),
        (MintOutcome.SECOND_FACTOR_REQUIRED, HoldReason.SECOND_FACTOR_UNSUPPORTED),
        (MintOutcome.ACCOUNT_ALERT, HoldReason.ACCOUNT_ALERT),
        (MintOutcome.TIMEOUT, HoldReason.TIMEOUT),
        (MintOutcome.UNKNOWN_FLOW, HoldReason.UNKNOWN_FLOW),
    ],
)
def test_driver_failures_become_closed_hold_reasons(outcome: MintOutcome, reason: HoldReason) -> None:
    failure = AcquisitionFailure(
        outcome,
        MintFailureEvidence(stage=MintFailureStage.CREDENTIAL, category=MintFailureCategory.UNKNOWN),
    )
    context = FakeMintContext(["about:blank"])

    with pytest.raises(SignInHold) as raised:
        _target_browser(context, FakeDriver(failure)).sign_in()

    assert raised.value.reason is reason


class FakeImageResponse:
    def __init__(self, url: str, status: int = 200) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-type": "image/jpeg"}


def _wire_headers(kind: str) -> dict[str, str]:
    """ExtraInfo headers: wire order, pseudo-headers and secrets included."""

    secrets = [
        (":authority", "photos.google.com"),
        ("cookie", "SID=secret; OSID=secret"),
        ("referer", "https://photos.google.com/photo/AF1QipSECRET"),
        ("x-client-data", "CIa2yQEIo7bJAQ=="),
        ("x-goog-ext-353267353-jspb", "[null,null,1]"),
        ("content-length", "123"),
        ("accept-encoding", "gzip, deflate, br, zstd"),
    ]
    if kind != "rpc":
        secrets.append(("content-type", "text/plain"))
    pairs = list(HEADERS[kind])
    # Interleave the secrets so dropping them must keep the wire order.
    merged: list[tuple[str, str]] = []
    for n, pair in enumerate(pairs):
        merged.append(pair)
        if n < len(secrets):
            merged.append(secrets[n])
    return dict(merged)


def _provisional_headers(kind: str) -> dict[str, str]:
    """What the renderer reports: alphabetical, without fetch metadata."""

    return dict(
        sorted(
            (name, value) for name, value in HEADERS[kind] if not name.startswith("sec-fetch-") and name != "priority"
        )
    )


class FakeCDPSession:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.handlers: dict[str, list[Any]] = {}
        self.sent: list[str] = []
        self.detached = False

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def send(self, method: str, params: Any = None) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("cdp unavailable")
        self.sent.append(method)
        return {}

    def detach(self) -> None:
        self.detached = True

    def emit(self, event: str, params: dict[str, Any]) -> None:
        if self.detached or "Network.enable" not in self.sent:
            return
        for handler in self.handlers.get(event, []):
            handler(params)


class FakeBrowserContext:
    def __init__(self, session: FakeCDPSession) -> None:
        self.session = session

    def new_cdp_session(self, page: Any) -> FakeCDPSession:
        return self.session


class FakeWarmPage:
    def __init__(self, context: FakeWarmContext, cdp: FakeCDPSession) -> None:
        self._context = context
        self.context = FakeBrowserContext(cdp)
        self.events: dict[str, list[Any]] = {"response": []}

    @property
    def listeners(self) -> list[Any]:
        return list(self.events["response"])

    def on(self, event: str, callback: Any) -> None:
        self.events[event].append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        self.events[event].remove(callback)

    def emit(self, event: str, value: Any) -> None:
        for listener in list(self.events[event]):
            listener(value)

    def wait_for_load_state(self, state: str, timeout: float) -> None:
        return None

    def content(self) -> str:
        return self._context.html


class FakeWarmContext:
    def __init__(
        self,
        *,
        final_url: str = "https://photos.google.com/",
        status: int = 200,
        html: str = "",
        tile: bool = True,
        image_on_click: bool = True,
        requests: tuple[str, ...] = HEADER_KINDS,
        wire: str = "sent",
        cdp: FakeCDPSession | None = None,
    ) -> None:
        self.requests = requests
        #: "sent": ExtraInfo after requestWillBeSent; "early": before it;
        #: "missing": never; "provisional": renderer headers only.
        self.wire = wire
        self.cdp = cdp or FakeCDPSession()
        self._ids = iter(range(1, 1000))
        self.final_url = final_url
        self.status = status
        self.html = html or _gd_page()
        self.tile = tile
        self.image_on_click = image_on_click
        self.page = FakeWarmPage(self, self.cdp)
        self.current_url = "about:blank"
        self.clicked = 0

    def user_agent(self) -> str:
        return UA

    def request(self, kind: str, url: str, *, method: str = "GET", type_: str = "Fetch") -> None:
        request_id = f"1000.{next(self._ids)}"
        sent = {
            "requestId": request_id,
            "request": {"url": url, "method": method, "headers": {"x": "1"}},
            "type": type_,
        }
        source = HEADERS.get(kind, HEADERS["image"])
        headers = (
            _provisional_headers(kind)
            if self.wire == "provisional" and kind in HEADERS
            else _wire_headers(kind)
            if kind in HEADERS
            else dict(source)
        )
        extra = {"requestId": request_id, "headers": headers}
        if self.wire == "early":
            self.cdp.emit("Network.requestWillBeSentExtraInfo", extra)
        self.cdp.emit("Network.requestWillBeSent", sent)
        if self.wire in {"sent", "provisional"}:
            self.cdp.emit("Network.requestWillBeSentExtraInfo", extra)

    def navigate_observed(self, url: str, *, timeout_seconds: float) -> Any:
        self.current_url = self.final_url
        # Requests that are none of the exported kinds are ignored.
        self.request("other", "https://www.gstatic.com/x")
        if "page" in self.requests:
            self.request("page", url, type_="Document")
        if "rpc" in self.requests:
            self.request(
                "rpc",
                "https://photos.google.com/_/PhotosUi/data/batchexecute?rpcids=x",
                method="POST",
                type_="XHR",
            )
        # Grid thumbnails load before any click; they must not count.
        self.page.emit(
            "response",
            FakeImageResponse("https://photos.fife.usercontent.google.com/a"),
        )

        class Observed:
            status = self.status

        return Observed()

    def wait(self, seconds: float) -> None:
        return None

    def wait_for_visible(self, selectors: tuple[str, ...], *, timeout_seconds: float) -> bool:
        return self.tile

    def click(self, selectors: tuple[str, ...]) -> None:
        self.clicked += 1
        if "image" in self.requests:
            self.request("image", "https://photos.fife.usercontent.google.com/b", type_="Image")
        if self.image_on_click:
            self.page.emit(
                "response",
                FakeImageResponse("https://photos.fife.usercontent.google.com/b"),
            )


def _warm_browser(context: FakeWarmContext) -> GoogleTargetBrowser:
    ticks = iter(range(10_000))
    return GoogleTargetBrowser(
        context,  # type: ignore[arg-type]
        load_identity_target("google"),
        expected_email=EMAIL,
        driver=FakeDriver(),  # type: ignore[arg-type]
        challenge_resolver=object(),  # type: ignore[arg-type]
        monotonic=lambda: float(next(ticks)),
    )


def _warm(context: FakeWarmContext) -> str:
    return _warm_browser(context).warm()


def test_warm_proves_the_account_and_a_post_click_image() -> None:
    context = FakeWarmContext()

    assert _warm(context) == EMAIL
    assert context.clicked == 1
    assert context.page.listeners == []


@pytest.mark.parametrize("wire", ["sent", "early"])
def test_warm_captures_wire_headers_reduced_to_the_allowlist(wire: str) -> None:
    context = FakeWarmContext(wire=wire)
    browser = _warm_browser(context)

    browser.warm()
    captured = browser.request_headers()

    # The wire order is kept and nothing outside the allowlist survives,
    # whichever of the two CDP events arrives first.
    assert captured == HEADERS
    assert context.cdp.sent == ["Network.enable", "Network.setCacheDisabled"]
    assert context.cdp.detached is True
    flat = json.dumps(captured).lower()
    for forbidden in (
        "accept-encoding",
        "cookie",
        "secret",
        "referer",
        "x-client-data",
        "x-goog",
        "content-length",
        ":authority",
        "text/plain",
    ):
        assert forbidden not in flat


@pytest.mark.parametrize("missing", HEADER_KINDS)
def test_warm_without_one_request_kind_fails_headers_missing(missing: str) -> None:
    context = FakeWarmContext(requests=tuple(kind for kind in HEADER_KINDS if kind != missing))

    with pytest.raises(VerifyFailed) as raised:
        _warm(context)

    assert raised.value.reason is VerifyFailure.HEADERS_MISSING
    assert context.page.listeners == []
    assert context.cdp.detached is True


@pytest.mark.parametrize(
    "context",
    [
        # ExtraInfo never arrives: only the renderer's view exists.
        FakeWarmContext(wire="missing"),
        # Provisional headers (alphabetical, no sec-fetch-*) are never exported.
        FakeWarmContext(wire="provisional"),
        # No CDP session at all.
        FakeWarmContext(cdp=FakeCDPSession(fail=True)),
    ],
)
def test_warm_without_wire_headers_fails_headers_missing(
    context: FakeWarmContext,
) -> None:
    with pytest.raises(VerifyFailed) as raised:
        _warm(context)

    assert raised.value.reason is VerifyFailure.HEADERS_MISSING
    assert context.cdp.detached is True


def test_redirect_hops_pair_in_order_and_ambiguous_requests_are_skipped() -> None:
    context = FakeWarmContext()
    recorder = _WireHeaderRecorder(context.page)
    recorder.open()
    cdp = context.cdp

    def sent(request_id: str, url: str, type_: str = "Document") -> None:
        cdp.emit(
            "Network.requestWillBeSent",
            {
                "requestId": request_id,
                "request": {"url": url, "method": "GET"},
                "type": type_,
            },
        )

    def extra(request_id: str, headers: dict[str, str]) -> None:
        cdp.emit(
            "Network.requestWillBeSentExtraInfo",
            {"requestId": request_id, "headers": headers},
        )

    # A redirected navigation: two hops, two ExtraInfo events.
    sent("nav", "https://photos.google.com/")
    extra("nav", {"user-agent": "hop-1", "cookie": "SID=x"})
    sent("nav", "https://photos.google.com/?pli=1")
    extra("nav", _wire_headers("page"))
    # One hop whose ExtraInfo is missing cannot be attributed.
    sent("odd", "https://photos.google.com/a")
    sent("odd", "https://photos.google.com/b")
    extra("odd", _wire_headers("page"))
    recorder.close()

    rows = recorder.requests()

    assert [(url, headers[0]) for url, _m, _t, headers in rows] == [
        ("https://photos.google.com/", ("user-agent", "hop-1")),
        ("https://photos.google.com/?pli=1", HEADERS["page"][0]),
    ]
    assert "SID" not in json.dumps(rows)
    assert cdp.detached is True


@pytest.mark.parametrize(
    ("context", "expected", "reason"),
    [
        (
            FakeWarmContext(final_url="https://www.google.com/photos/about/"),
            SessionDead,
            None,
        ),
        (
            FakeWarmContext(final_url="https://accounts.google.com/v3/signin/identifier"),
            SessionDead,
            None,
        ),
        (
            FakeWarmContext(final_url="https://www.google.com/"),
            VerifyFailed,
            VerifyFailure.PAGE_INVALID,
        ),
        # No positive signed-out evidence: a page that proved nothing.
        (
            FakeWarmContext(html="<html>no data</html>"),
            VerifyFailed,
            VerifyFailure.PAGE_INVALID,
        ),
        (
            FakeWarmContext(status=503, html="<html>Error 503</html>"),
            VerifyFailed,
            VerifyFailure.PAGE_INVALID,
        ),
        (
            FakeWarmContext(status=500),
            VerifyFailed,
            VerifyFailure.PAGE_INVALID,
        ),
        # An error status on a sign-in hop or the public page is an outage,
        # never a reason to spend a credential sign-in.
        (
            FakeWarmContext(
                final_url="https://accounts.google.com/ServiceLogin?passive=1",
                status=503,
            ),
            VerifyFailed,
            VerifyFailure.PAGE_INVALID,
        ),
        (
            FakeWarmContext(final_url="https://www.google.com/photos/about/", status=502),
            VerifyFailed,
            VerifyFailure.PAGE_INVALID,
        ),
        (
            FakeWarmContext(final_url="https://www.google.com/sorry/index", status=503),
            ProviderBlocked,
            BlockReason.SORRY_PAGE,
        ),
        (
            FakeWarmContext(final_url="chrome-error://chromewebdata/"),
            VerifyFailed,
            VerifyFailure.PAGE_INVALID,
        ),
        (
            FakeWarmContext(html=_gd_page("")),
            VerifyFailed,
            VerifyFailure.PAGE_INVALID,
        ),
        (
            FakeWarmContext(final_url="https://www.google.com/sorry/index"),
            ProviderBlocked,
            BlockReason.SORRY_PAGE,
        ),
        (FakeWarmContext(status=429), ProviderBlocked, BlockReason.RATE_LIMITED),
        (FakeWarmContext(html=_gd_page("other@example.com")), AccountMismatch, None),
        (FakeWarmContext(tile=False), VerifyFailed, VerifyFailure.LIBRARY_EMPTY),
        (
            FakeWarmContext(image_on_click=False),
            VerifyFailed,
            VerifyFailure.IMAGE_RESPONSE_MISSING,
        ),
    ],
)
def test_warm_classifies_every_non_success(context: FakeWarmContext, expected: type[Exception], reason: object) -> None:
    with pytest.raises(expected) as raised:
        _warm(context)
    if reason is not None:
        assert getattr(raised.value, "reason", None) is reason
    assert context.page.listeners == []


def test_credential_budget_reports_when_the_window_reopens(tmp_path: Path) -> None:
    clock = Clock()
    budget = CredentialBudget(tmp_path, utc_now=clock)
    assert budget.next_attempt_at("google", limit=1) is None
    assert budget.reserve("google", limit=1)
    assert budget.next_attempt_at("google", limit=1) == NOW + timedelta(hours=24)
    clock.now = NOW + timedelta(hours=24, seconds=1)
    assert budget.next_attempt_at("google", limit=1) is None


def test_replace_hold_after_a_concurrent_acknowledge_records_a_fresh_hold(
    tmp_path: Path,
) -> None:
    store = TargetStateStore(tmp_path, utc_now=Clock())
    provisional = store.record_hold("google", HoldReason.SIGN_IN_INTERRUPTED)
    assert store.clear_hold("google", provisional.hold_id)

    final = store.replace_hold("google", provisional.hold_id, HoldReason.CHALLENGE)

    assert final.reason is HoldReason.CHALLENGE
    assert final.hold_id != provisional.hold_id
    assert store.load("google").hold == final


def test_sign_in_failure_is_held_even_if_acknowledged_mid_attempt(
    tmp_path: Path,
) -> None:
    browser = FakeBrowser(warm=[SessionDead()])
    harness = Harness(tmp_path, browser=browser)

    def sign_in() -> None:
        provisional = harness.state.load("google").hold
        assert provisional is not None
        assert harness.state.clear_hold("google", provisional.hold_id)
        raise SignInHold(HoldReason.CHALLENGE)

    browser.sign_in = sign_in  # type: ignore[method-assign]

    result = harness.run()

    assert result.outcome is IdentityOutcome.HELD
    assert result.reason == HoldReason.CHALLENGE.value
    hold = harness.state.load("google").hold
    assert hold is not None and hold.reason is HoldReason.CHALLENGE
    assert result.hold_id == hold.hold_id
    assert result.exit_code == 1


def test_unreadable_state_reads_as_stale(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert harness.run().exit_code == 0
    (tmp_path / "identity-targets" / "google.json").write_text("{not json")

    result = harness.keeper._result(IdentityOutcome.COOLDOWN_ACTIVE)

    assert result.snapshot_age_s is None
    assert result.exit_code == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://consent.google.com/ml?continue=https://photos.google.com/",
        "https://myaccount.google.com/",
        "https://gds.google.com/web/landing?continue=https://photos.google.com/",
    ],
)
def test_other_google_hosts_never_lead_to_a_sign_in(tmp_path: Path, url: str) -> None:
    context = FakeWarmContext(final_url=url)
    with pytest.raises(VerifyFailed) as raised:
        _warm(context)
    assert raised.value.reason is VerifyFailure.PAGE_INVALID

    harness = Harness(tmp_path, browser=FakeBrowser(warm=[raised.value]))
    result = harness.run(allow=True)

    assert result.outcome is IdentityOutcome.VERIFY_FAILED
    assert result.reason == "page_invalid"
    assert harness.browser.sign_ins == 0
    assert harness.budget.attempts_today("google") == 0


@pytest.mark.parametrize("reason", list(BlockReason))
def test_pushback_during_sign_in_is_held_as_well_as_cooled_down(tmp_path: Path, reason: BlockReason) -> None:
    browser = FakeBrowser(warm=[SessionDead()], sign_in=ProviderBlocked(reason))
    harness = Harness(tmp_path, browser=browser)

    result = harness.run()

    assert result.outcome is IdentityOutcome.HELD
    assert result.reason == HoldReason.SIGN_IN_BLOCKED.value
    assert result.exit_code == 1
    state = harness.state.load("google")
    assert state.hold is not None
    assert state.hold.reason is HoldReason.SIGN_IN_BLOCKED
    assert result.hold_id == state.hold.hold_id
    assert state.cooldown is not None and state.cooldown.reason is reason
    assert result.cooldown_until == NOW + timedelta(hours=2)

    # After the cooldown and the budget window, the hold still stops it.
    harness.clock.now += timedelta(days=2)
    browser.warm_script = [SessionDead()]
    again = harness.run()
    assert again.outcome is IdentityOutcome.HELD
    assert again.hold_id == state.hold.hold_id
    assert browser.sign_ins == 1
    assert harness.opened == 1


def test_pushback_on_a_plain_warm_up_is_only_a_cooldown(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        browser=FakeBrowser(warm=[ProviderBlocked(BlockReason.CAPTCHA)]),
    )

    result = harness.run()

    assert result.outcome is IdentityOutcome.BLOCKED
    assert result.reason == "captcha"
    assert harness.state.load("google").hold is None


@pytest.mark.parametrize(
    ("offset", "age", "stale"),
    [
        (timedelta(minutes=-4), 0, False),
        (timedelta(minutes=-6), None, True),
        (timedelta(days=-1), None, True),
        (timedelta(hours=1), 3600, False),
    ],
)
def test_a_publish_time_in_the_future_is_stale(tmp_path: Path, offset: timedelta, age: int | None, stale: bool) -> None:
    harness = Harness(tmp_path)
    published = NOW - offset
    TargetStateStore(tmp_path / "identity-targets", utc_now=lambda: published).record_publish("google", 1)
    harness.state.record_block("google", BlockReason.SORRY_PAGE)

    result = harness.run()

    assert result.outcome is IdentityOutcome.COOLDOWN_ACTIVE
    assert result.snapshot_age_s == age
    assert result.needs_attention is stale


def test_owner_disconnect_stops_maintenance_without_opening_browser_or_spending_budget(tmp_path):
    from photos_mcp.connection_controls import PublicationGuard
    from photos_mcp.connections_auth import BrowserStore

    harness = Harness(tmp_path)
    controls = BrowserStore(tmp_path / "connections")
    controls.set_control("first", 1)
    harness.keeper._connection_guard = PublicationGuard(controls, "first")
    result = harness.run()
    assert result.outcome is IdentityOutcome.DISCONNECTED
    assert result.public_dict()["maintenance_suspended"] is True
    assert not result.needs_attention and result.generation is None
    assert harness.opened == 0 and harness.budget.attempts_today("google") == 0


def test_disconnect_and_resume_during_verification_cannot_publish_an_old_attempt(tmp_path):
    from photos_mcp.connection_controls import PublicationGuard
    from photos_mcp.connections_auth import BrowserStore

    harness = Harness(tmp_path)
    controls = BrowserStore(tmp_path / "connections")
    harness.keeper._connection_guard = PublicationGuard(controls, "first")
    assert harness.run().outcome is IdentityOutcome.PUBLISHED
    accepted = (harness.export / META_FILE).read_bytes()
    warm = harness.browser.warm

    def changed_generation():
        controls.set_control("first", 1)
        controls.set_control("first", 0)
        return warm()

    harness.browser.warm = changed_generation
    result = harness.run()
    assert result.outcome is IdentityOutcome.DISCONNECTED
    assert (harness.export / META_FILE).read_bytes() == accepted
    assert harness.state.load("google").last_generation == 1
