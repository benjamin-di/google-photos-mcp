"""Closed vocabularies and internal signals for identity-target runs."""

from __future__ import annotations

from enum import StrEnum


class IdentityOutcome(StrEnum):
    """What one ``identity keep`` run did. Closed; consumers branch on it."""

    #: A new snapshot generation passed browser verification and was published.
    PUBLISHED = "published"
    #: The connection owner deliberately suspended access; no sign-in is attempted.
    DISCONNECTED = "disconnected"
    #: Another run holds the profile lease; nothing was attempted.
    SKIPPED_LEASE_BUSY = "skipped_lease_busy"
    #: An earlier ``blocked`` cooldown is still running; nothing was attempted.
    COOLDOWN_ACTIVE = "cooldown_active"
    #: The session is dead and the rolling 24 h sign-in was already spent.
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: A durable hold exists (new or earlier); only an operator clears it.
    HELD = "held"
    #: The site pushed back (``/sorry``, 429, captcha); a cooldown was recorded.
    BLOCKED = "blocked"
    #: No positive signed-out evidence, but the browser could not prove the
    #: library, an image or the export; never triggers a sign-in.
    VERIFY_FAILED = "verify_failed"
    #: The session is dead and ``--allow-credential-mint`` was not given.
    MINT_NOT_PERMITTED = "mint_not_permitted"
    #: The session is dead and this host cannot complete a sign-in.
    HOST_NOT_READY = "host_not_ready"
    #: Browser, display or local state failure outside the site's answer.
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class HoldReason(StrEnum):
    CHALLENGE = "challenge"
    ACCOUNT_ALERT = "account_alert"
    SECOND_FACTOR_UNSUPPORTED = "second_factor_unsupported"
    CREDENTIAL_REJECTED = "credential_rejected"
    TIMEOUT = "timeout"
    UNKNOWN_FLOW = "unknown_flow"
    ACCOUNT_MISMATCH = "account_mismatch"
    SIGN_IN_UNCONFIRMED = "sign_in_unconfirmed"
    #: Recorded before a sign-in starts; survives a killed or crashed attempt.
    SIGN_IN_INTERRUPTED = "sign_in_interrupted"
    SIGN_IN_INFRASTRUCTURE = "sign_in_infrastructure"
    #: Still signed out right after a completed sign-in; never repeated daily.
    SESSION_STILL_DEAD = "session_still_dead"
    #: The site pushed back (/sorry, 429, captcha) during a sign-in; a cooldown
    #: is recorded too, but credentials are never resubmitted into it.
    SIGN_IN_BLOCKED = "sign_in_blocked"


class BlockReason(StrEnum):
    ACCESS_DENIED = "access_denied"
    SORRY_PAGE = "sorry_page"
    RATE_LIMITED = "rate_limited"
    CAPTCHA = "captcha"


class VerifyFailure(StrEnum):
    LIBRARY_EMPTY = "library_empty"
    IMAGE_RESPONSE_MISSING = "image_response_missing"
    EXPORT_INCOMPLETE = "export_incomplete"
    #: the site answered without proving a signed-in library: an HTTP
    #: error status (not 429), an error page, a slow page, no ``_gd`` or no
    #: account email.
    PAGE_INVALID = "page_invalid"
    #: The warm-up did not observe a valid page, rpc and image request whose
    #: real headers schema 2 must export.
    HEADERS_MISSING = "headers_missing"


class InfrastructureReason(StrEnum):
    BROWSER = "browser"
    STATE = "state"
    CONFIGURATION = "configuration"
    UNCLASSIFIED = "unclassified"


class SessionDead(Exception):
    """Positive signed-out evidence from the browser: a sign-in page or
    the site's public product page. Nothing else may raise it."""


class ProviderBlocked(Exception):
    def __init__(self, reason: BlockReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class AccountMismatch(Exception):
    """The signed-in account is not the expected one; never auto-corrected."""


class VerifyFailed(Exception):
    def __init__(self, reason: VerifyFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


class SignInHold(Exception):
    """A sign-in ended in a state that must not be retried automatically."""

    def __init__(self, reason: HoldReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


__all__ = [
    "AccountMismatch",
    "BlockReason",
    "ProviderBlocked",
    "HoldReason",
    "IdentityOutcome",
    "InfrastructureReason",
    "SessionDead",
    "SignInHold",
    "VerifyFailed",
    "VerifyFailure",
]
