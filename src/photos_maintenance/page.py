"""Read Google's signed-in page data (``WIZ_global_data``) from HTML.

Patchright evaluates page scripts in an isolated world, so
``window.WIZ_global_data`` is not visible there; the browser warm-up parses the
served HTML instead.
"""

from __future__ import annotations

import json
import re

_GD_SCRIPT = re.compile(r"<script\b[^>]*\bdata-id=[\"']_gd[\"']", re.IGNORECASE)
_WIZ_ASSIGNMENT = re.compile(r"WIZ_global_data\s*=\s*(\{.*?\});", re.DOTALL)
_WIZ_KEY = r"\"{key}\"\s*:\s*(\"(?:[^\"\\]|\\.)*\")"

#: Keys read from the signed-in page: account email, session id, build label,
#: XSRF token and the app path prefix (only the email is required today).
WIZ_KEYS = ("oPEP7c", "FdrFJe", "cfb2h", "SNlM0e", "Im6cmf")


def has_signed_in_data(html: str) -> bool:
    return _GD_SCRIPT.search(html) is not None


def parse_wiz(html: str) -> dict[str, str] | None:
    """Return the string-valued WIZ keys we use, or ``None`` without ``_gd``."""

    if not has_signed_in_data(html):
        return None
    values: dict[str, str] = {}
    match = _WIZ_ASSIGNMENT.search(html)
    if match is not None:
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            values = {key: value for key in WIZ_KEYS if isinstance(value := payload.get(key), str)}
    for key in WIZ_KEYS:
        if key in values:
            continue
        found = re.search(_WIZ_KEY.format(key=re.escape(key)), html)
        if found is None:
            continue
        try:
            decoded = json.loads(found.group(1))
        except ValueError:
            continue
        if isinstance(decoded, str):
            values[key] = decoded
    return values


def same_account(observed: str | None, expected: str) -> bool:
    return observed is not None and (observed.strip().casefold() == expected.strip().casefold() != "")


__all__ = ["WIZ_KEYS", "has_signed_in_data", "parse_wiz", "same_account"]
