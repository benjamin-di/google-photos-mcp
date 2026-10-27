"""One bounded account upkeep run (docs/MAINTENANCE.md).

warm -> browser verify + header capture -> stage -> atomic publish. The browser
is the only liveness authority: only its positive signed-out evidence
(``SessionDead``) may lead to a sign-in; the reader proves each new generation
over HTTP itself.

A dead session may sign in once, only with explicit permission, a ready host
and an unspent daily budget. Every sign-in failure becomes a durable hold;
Site pushback becomes an escalating cooldown, and during a sign-in also the
hold `sign_in_blocked`. Nothing here retries.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from signin_window.automation.profiles import ProfileError as BrowserRuntimeError
from signin_window.leases import LeaseBusyError
from signin_window.sessions import SessionStillDead as _StillDead
from signin_window.sessions import WarmFirst
from signin_window.state import atomic_private_write

from photos_mcp.connection_controls import ConnectionSuspended

from .models import FrozenModel, PersistenceError, ProviderReadiness
from .outcomes import (
    AccountMismatch,
    HoldReason,
    IdentityOutcome,
    InfrastructureReason,
    ProviderBlocked,
    SessionDead,
    SignInHold,
    VerifyFailed,
    VerifyFailure,
)
from .page import same_account
from .snapshot import (
    ExportCookie,
    HeaderTemplate,
    SnapshotError,
    SnapshotMeta,
    chromium_major,
    discard_staging,
    prepare_export_dir,
    publish_snapshot,
    published_generation,
    stage_snapshot,
    validate_header_templates,
)
from .state import TargetState, TargetStateStore

#: At most one credential sign-in per rolling 24 hours.
DAILY_SIGN_IN_LIMIT = 1
#: The protected target: a published snapshot no older than this.
STALE_AFTER = timedelta(hours=3)
#: Clock skew tolerated for a publish time that lies in the future.
FUTURE_TOLERANCE = timedelta(minutes=5)


def snapshot_age_seconds(last_published_at: datetime | None, now: datetime) -> int | None:
    """Seconds since the publish; ``None`` (stale) if never or implausible.

    A publish time more than :data:`FUTURE_TOLERANCE` in the future is a
    broken clock or state, not a fresh snapshot.
    """

    if last_published_at is None:
        return None
    age = now - last_published_at
    if age < -FUTURE_TOLERANCE:
        return None
    return max(0, int(age.total_seconds()))


def snapshot_is_stale(age_seconds: int | None) -> bool:
    """Never published, or published longer than :data:`STALE_AFTER` ago."""

    return age_seconds is None or age_seconds > STALE_AFTER.total_seconds()


class IdentityRunResult(FrozenModel):
    target: str
    outcome: IdentityOutcome
    reason: str | None = None
    generation: int | None = None
    signed_in: bool = False
    #: The hold present after the run, whatever the outcome.
    hold_id: str | None = None
    cooldown_until: datetime | None = None
    #: Seconds since the last publish; ``None`` if never published or unknown.
    snapshot_age_s: int | None = None
    #: When the rolling 24 h budget next allows a sign-in; ``None`` if now.
    next_sign_in_at: datetime | None = None

    @property
    def needs_attention(self) -> bool:
        """A person is needed: a hold exists or the snapshot is stale.

        For active connections this is independent of the outcome code, so a repeated skip,
        cooldown or spent budget cannot keep the check green while the reader
        reads an ageing snapshot. An owner-suspended account has no reader access
        and reports its deliberate suspension separately, retaining any hold.
        """

        if self.outcome is IdentityOutcome.DISCONNECTED:
            return False  # The reader is blocked; a suspended connection is not a stale active one.
        return self.hold_id is not None or snapshot_is_stale(self.snapshot_age_s)

    @property
    def exit_code(self) -> int:
        return 1 if self.needs_attention else 0

    def public_dict(self) -> dict[str, Any]:
        value = self.model_dump(mode="json")
        value["needs_attention"] = self.needs_attention
        if self.outcome is IdentityOutcome.DISCONNECTED:
            value["maintenance_suspended"] = True
        return value


class IdentityStatusReport(FrozenModel):
    """Stored state only: no browser, no network."""

    target: str
    state: TargetState
    snapshot_age_s: int | None = None
    next_sign_in_at: datetime | None = None

    @property
    def stale(self) -> bool:
        return snapshot_is_stale(self.snapshot_age_s)

    def public_dict(self) -> dict[str, Any]:
        dumped = self.model_dump(mode="json")
        return {
            "target": self.target,
            **dumped["state"],
            "snapshot_age_s": self.snapshot_age_s,
            "stale": self.stale,
            "next_sign_in_at": dumped["next_sign_in_at"],
        }


def read_identity_status(
    target: str,
    *,
    state: TargetStateStore,
    budget: SignInBudget,
    now: datetime,
) -> IdentityStatusReport:
    """Stored hold, cooldown, freshness and sign-in budget; contacts nothing."""

    stored = state.load(target)
    return IdentityStatusReport(
        target=target,
        state=stored,
        snapshot_age_s=snapshot_age_seconds(stored.last_published_at, now),
        next_sign_in_at=budget.next_attempt_at(target, limit=DAILY_SIGN_IN_LIMIT),
    )


class IdentityAcknowledgement(FrozenModel):
    target: str
    hold_id: str
    cleared: bool

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class TargetBrowser(Protocol):
    def warm(self) -> str: ...

    def sign_in(self) -> None: ...

    def export_cookies(self) -> list[ExportCookie]: ...

    def request_headers(self) -> dict[str, HeaderTemplate]: ...

    def user_agent(self) -> str: ...


class SignInBudget(Protocol):
    def reserve(self, provider: str, *, limit: int) -> bool: ...

    def next_attempt_at(self, provider: str, *, limit: int) -> datetime | None: ...


class _MintRefused(Exception):
    def __init__(self, result: IdentityRunResult) -> None:
        super().__init__(result.outcome.value)
        self.result = result


class IdentityKeeper:
    def __init__(
        self,
        *,
        target: str,
        expected_email: str,
        export_dir: Path,
        state: TargetStateStore,
        budget: SignInBudget,
        readiness: Callable[[], ProviderReadiness],
        open_browser: Callable[[], AbstractContextManager[TargetBrowser]],
        lease: Callable[[], AbstractContextManager[None]],
        utc_now: Callable[[], datetime],
        snapshot_writer: Callable[[Path, TargetBrowser, int, str, datetime], None] | None = None,
        snapshot_generation: Callable[[Path], int] = published_generation,
        connection_guard=None,
    ) -> None:
        self._target = target
        self._expected_email = expected_email
        self._export_dir = export_dir
        self._state = state
        self._budget = budget
        self._readiness = readiness
        self._open_browser = open_browser
        self._lease = lease
        self._utc_now = utc_now
        self._snapshot_writer = snapshot_writer
        self._snapshot_generation = snapshot_generation
        self._connection_guard = connection_guard
        self._connection_epoch = None
        self._positive_signed_out = False

    def status(self) -> IdentityStatusReport:
        """Stored state plus freshness and budget; touches no browser."""

        return read_identity_status(self._target, state=self._state, budget=self._budget, now=self._utc_now())

    def preflight(self) -> IdentityRunResult | None:
        """Report an existing hold or cooldown without touching a browser."""

        state = self._state.load(self._target)
        if state.hold is not None:
            return self._result(
                IdentityOutcome.HELD,
                reason=state.hold.reason.value,
                hold_id=state.hold.hold_id,
            )
        if state.cooldown is not None and state.cooldown.until > self._utc_now():
            return self._result(
                IdentityOutcome.COOLDOWN_ACTIVE,
                reason=state.cooldown.reason.value,
                cooldown_until=state.cooldown.until,
            )
        return None

    def run(self, *, allow_credential_mint: bool) -> IdentityRunResult:
        try:
            with self._lease():
                result = self._run_leased(allow_credential_mint=allow_credential_mint)
                try:
                    self._record_owner_status(result)
                except ConnectionSuspended:
                    return self._result(IdentityOutcome.DISCONNECTED)
                except OSError:
                    return self._infrastructure(InfrastructureReason.STATE, result.signed_in)
                return result
        except LeaseBusyError:
            return self._result(IdentityOutcome.SKIPPED_LEASE_BUSY)

    def _record_owner_status(self, result):
        if self._connection_guard is None or result.outcome in (
            IdentityOutcome.DISCONNECTED,
            IdentityOutcome.COOLDOWN_ACTIVE,
            IdentityOutcome.SKIPPED_LEASE_BUSY,
        ):
            return
        if result.outcome == IdentityOutcome.PUBLISHED:
            state = "signed_in"
        elif self._positive_signed_out and result.outcome in (
            IdentityOutcome.MINT_NOT_PERMITTED,
            IdentityOutcome.BUDGET_EXHAUSTED,
            IdentityOutcome.HOST_NOT_READY,
        ):
            state = "signed_out"
        else:
            state = "error"
        with self._publication_guard():
            self._export_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            payload = dict(
                state=state,
                generation=published_generation(self._export_dir),
                email=self._expected_email,
                checked_at=int(self._utc_now().timestamp()),
                error=result.reason,
            )
            atomic_private_write(self._export_dir / "keeper.json", json.dumps(payload).encode())

    def _run_leased(self, *, allow_credential_mint: bool) -> IdentityRunResult:
        from .human_state import NativePromotion

        # A stopped human custodian may have interrupted its two-path commit.
        NativePromotion(self._state.root.parent, self._export_dir, self._connection_guard).recover()
        self._positive_signed_out = False
        # Re-read under the lease: another run may have just held or blocked.
        if self._connection_guard is not None:
            try:
                self._connection_epoch = self._connection_guard.check()
            except ConnectionSuspended:
                return self._result(IdentityOutcome.DISCONNECTED)
        early = self.preflight()
        if early is not None:
            return early
        signed_in = False
        try:
            prepare_export_dir(self._export_dir)
            discard_staging(self._export_dir)
            with self._open_browser() as browser:
                flow = WarmFirst(
                    lambda: self._observed_refresh(browser),
                    lambda: self._sign_in(browser, allow=allow_credential_mint),
                    signed_out=SessionDead,
                )
                try:
                    generation = flow.run()
                finally:
                    signed_in = flow.reauthenticated
                return self._result(
                    IdentityOutcome.PUBLISHED,
                    generation=generation,
                    signed_in=signed_in,
                )

        except ConnectionSuspended:
            return self._result(IdentityOutcome.DISCONNECTED)
        except _MintRefused as refused:
            return refused.result
        except SignInHold as hold:
            # Already recorded by _sign_in; report it.
            stored = self._state.load(self._target)
            current = stored.hold
            cooldown = stored.cooldown
            return self._result(
                IdentityOutcome.HELD,
                reason=hold.reason.value,
                hold_id=current.hold_id if current is not None else None,
                cooldown_until=(cooldown.until if cooldown is not None and cooldown.until > self._utc_now() else None),
                signed_in=signed_in,
            )
        except _StillDead:
            # A sign-in that did not take must not be repeated every day.
            recorded = self._state.record_hold(self._target, HoldReason.SESSION_STILL_DEAD)
            return self._result(
                IdentityOutcome.HELD,
                reason=recorded.reason.value,
                hold_id=recorded.hold_id,
                signed_in=True,
            )
        except AccountMismatch:
            recorded = self._state.record_hold(self._target, HoldReason.ACCOUNT_MISMATCH)
            return self._result(
                IdentityOutcome.HELD,
                reason=recorded.reason.value,
                hold_id=recorded.hold_id,
                signed_in=signed_in,
            )
        except ProviderBlocked as blocked:
            cooldown = self._state.record_block(self._target, blocked.reason)
            return self._result(
                IdentityOutcome.BLOCKED,
                reason=blocked.reason.value,
                cooldown_until=cooldown.until,
                signed_in=signed_in,
            )
        except VerifyFailed as failed:
            return self._result(
                IdentityOutcome.VERIFY_FAILED,
                reason=failed.reason.value,
                signed_in=signed_in,
            )
        except BrowserRuntimeError:
            return self._infrastructure(InfrastructureReason.BROWSER, signed_in)
        except (SnapshotError, PersistenceError):
            return self._infrastructure(InfrastructureReason.STATE, signed_in)
        except Exception:
            # A scheduled run must answer in its closed vocabulary. A sign-in
            # that crashed here already left its provisional hold behind.
            return self._infrastructure(InfrastructureReason.UNCLASSIFIED, signed_in)
        finally:
            discard_staging(self._export_dir)

    def _refresh(self, browser: TargetBrowser) -> int:
        """Warm, verify and capture headers in the browser, stage, publish."""

        email = browser.warm()
        if not same_account(email, self._expected_email):
            raise AccountMismatch()
        generation = (
            max(
                self._snapshot_generation(self._export_dir),
                self._state.load(self._target).last_generation,
            )
            + 1
        )
        if self._snapshot_writer is not None:
            with self._publication_guard():
                self._snapshot_writer(self._export_dir, browser, generation, email, self._utc_now())
                self._state.record_publish(self._target, generation)
            return generation
        cookies = browser.export_cookies()
        user_agent = browser.user_agent()
        try:
            major = chromium_major(user_agent)
        except SnapshotError as error:
            raise VerifyFailed(VerifyFailure.EXPORT_INCOMPLETE) from error
        headers = browser.request_headers()
        try:
            validate_header_templates(headers, user_agent=user_agent)
        except SnapshotError as error:
            raise VerifyFailed(VerifyFailure.HEADERS_MISSING) from error
        staging = stage_snapshot(
            self._export_dir,
            cookies,
            SnapshotMeta(
                generation=generation,
                email=email,
                user_agent=user_agent,
                chromium_major=major,
                verified_at=self._utc_now(),
                headers=headers,
            ),
        )
        with self._publication_guard():
            publish_snapshot(self._export_dir, staging)
            self._state.record_publish(self._target, generation)
        return generation

    def _observed_refresh(self, browser):
        try:
            return self._refresh(browser)
        except SessionDead:
            self._positive_signed_out = True
            raise

    def _publication_guard(self):
        return (
            self._connection_guard.publish(self._connection_epoch)
            if self._connection_guard is not None
            else nullcontext()
        )

    def _sign_in(self, browser: TargetBrowser, *, allow: bool) -> None:
        if self._connection_guard is not None:
            self._connection_guard.check()
        if not allow:
            raise _MintRefused(self._result(IdentityOutcome.MINT_NOT_PERMITTED, reason="session_dead"))
        readiness = self._readiness()
        if readiness.missing:
            raise _MintRefused(
                self._result(
                    IdentityOutcome.HOST_NOT_READY,
                    reason=",".join(item.value for item in readiness.missing),
                )
            )
        if not self._budget.reserve(self._target, limit=DAILY_SIGN_IN_LIMIT):
            raise _MintRefused(self._result(IdentityOutcome.BUDGET_EXHAUSTED, reason="session_dead"))
        # Provisional hold first: a killed or crashed attempt stays held.
        provisional = self._state.record_hold(self._target, HoldReason.SIGN_IN_INTERRUPTED)
        try:
            browser.sign_in()
        except SignInHold as hold:
            self._state.replace_hold(self._target, provisional.hold_id, hold.reason)
            raise
        except ProviderBlocked as blocked:
            # Pushback during a sign-in: hold it, so an expired cooldown and a
            # fresh budget never resubmit credentials into the same captcha.
            self._state.replace_hold(self._target, provisional.hold_id, HoldReason.SIGN_IN_BLOCKED)
            self._state.record_block(self._target, blocked.reason)
            raise SignInHold(HoldReason.SIGN_IN_BLOCKED) from blocked
        except Exception as error:
            self._state.replace_hold(self._target, provisional.hold_id, HoldReason.SIGN_IN_INFRASTRUCTURE)
            raise SignInHold(HoldReason.SIGN_IN_INFRASTRUCTURE) from error
        self._state.clear_hold(self._target, provisional.hold_id)

    def _infrastructure(self, reason: InfrastructureReason, signed_in: bool) -> IdentityRunResult:
        return self._result(
            IdentityOutcome.INFRASTRUCTURE_ERROR,
            reason=reason.value,
            signed_in=signed_in,
        )

    def _result(self, outcome: IdentityOutcome, **values: Any) -> IdentityRunResult:
        """Attach the post-run hold, snapshot age and next sign-in time.

        Unreadable state leaves the age unknown, which reads as stale: the
        answer then pages rather than staying green on a guess.
        """

        try:
            report = self.status()
        except Exception:
            report = None
        if report is not None:
            hold = report.state.hold
            values["hold_id"] = hold.hold_id if hold is not None else None
            values["snapshot_age_s"] = report.snapshot_age_s
            values["next_sign_in_at"] = report.next_sign_in_at
        return IdentityRunResult(target=self._target, outcome=outcome, **values)


__all__ = [
    "DAILY_SIGN_IN_LIMIT",
    "STALE_AFTER",
    "IdentityAcknowledgement",
    "IdentityKeeper",
    "IdentityRunResult",
    "IdentityStatusReport",
    "read_identity_status",
    "snapshot_age_seconds",
    "snapshot_is_stale",
]
