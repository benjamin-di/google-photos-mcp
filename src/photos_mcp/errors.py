"""The error taxonomy (README "Errors"). Tools report ``{code, next_step, retryable}`` and nothing else.

Exception text never reaches a tool result: every message a model sees comes from the
fixed ``NEXT_STEP`` table below (plus, for ``invalid_argument``, our own validation text).
"""

from __future__ import annotations

from typing import Any

NEXT_STEP: dict[str, str] = {
    "session_unavailable": (
        "The Google session snapshot is missing or unreadable. The operator must publish one "
        "(photos-mcp-session); check photos_status later. Do not retry in a loop."
    ),
    "session_expired": (
        "Google rejected the session. Retry once photos_status shows a newer generation (the "
        "operator refreshes the session snapshot)."
    ),
    "account_mismatch": (
        "The session belongs to a different Google account. Stop and tell the operator; this "
        "is never corrected automatically."
    ),
    "blocked": (
        "Google pushed back (rate limit, /sorry or captcha). No Google calls until the breaker "
        "closes (photos_status shows when). Do not retry now."
    ),
    "consent_required": "Google wants a consent screen confirmed. Retry once photos_status shows a newer generation.",
    "stale_token": (
        "Google rejected the request token even after a refresh. Retry in a few minutes; if it "
        "persists, the web API changed and the operator must update photos-mcp."
    ),
    "schema_changed": (
        "Google's response format changed and this tool cannot read it. Tell the operator "
        "(README: schema_changed); other tools may still work."
    ),
    "media_session_required": (
        "Google did not serve this image to the session. Other items may work; if it repeats, "
        "the operator should refresh the session snapshot (it needs the image host's cookies)."
    ),
    "item_unavailable": "This item could not be fetched (deleted, not in this library, or not an image). Skip it.",
    "budget_exhausted": (
        "The daily Google request budget is spent. It resets at midnight in the server's budget time "
        "zone (photos_status: budgets.resets_at)."
    ),
    "busy": "Another request to Google is still running. Retry in a few seconds.",
    "upstream_error": "Google or the network failed after retries. Retry in a minute.",
    "invalid_argument": "Fix the arguments and call again.",
    "invalid_cursor": (
        "This cursor belongs to another query, source or album, or is malformed. Start again without a cursor."
    ),
    "cursor_expired": "This cursor expired. Start again without a cursor and skip ids you already have.",
    "internal_error": "photos-mcp failed unexpectedly. Retry once; if it repeats, tell the operator.",
}

RETRYABLE = frozenset({"busy", "upstream_error", "stale_token", "internal_error"})


class PhotosError(Exception):
    """A classified failure. ``log_detail`` is for logs only and must not carry secrets."""

    def __init__(self, code: str, detail: str | None = None, *, log_detail: str | None = None) -> None:
        if code not in NEXT_STEP:
            raise ValueError(f"unknown error code {code}")
        super().__init__(code)
        self.code = code
        self.detail = detail  # only for invalid_argument: our own validation text
        self.log_detail = log_detail

    def payload(self) -> dict[str, Any]:
        next_step = NEXT_STEP[self.code]
        if self.code == "invalid_argument" and self.detail:
            next_step = f"{self.detail}. {next_step}"
        return {"code": self.code, "next_step": next_step, "retryable": self.code in RETRYABLE}


def invalid(detail: str) -> PhotosError:
    return PhotosError("invalid_argument", detail)
