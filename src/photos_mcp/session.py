"""Read the Google session snapshot an exporter publishes (``photos-mcp-session``, or your own).

The snapshot is a read-only directory (``PHOTOS_SESSION_DIR``, default ``/session``):

* ``cookies.txt``: Netscape format. Host-only rows have include-subdomains ``FALSE`` and
  no leading dot; domain rows have ``TRUE`` and a leading dot.
* ``meta.json``: ``{schema: 2, generation, email, account_index: 0, user_agent,
  chromium_major, verified_at, headers: {page, rpc, image}}``. Written last, so it is the
  commit point. ``headers.<kind>`` holds the ``[name, value]`` pairs Chrome sent for one
  real request of that kind while the exporter loaded the library (allowlisted names only); the
  reader puts them in Chrome's wire order (``CANONICAL_ORDER``), and a kind missing
  ``sec-fetch-site``/``-mode``/``-dest``, or whose dest contradicts the kind, uses the
  UA-synthesised template for that kind (logged once per generation). Schema 1
  (no ``headers``, for example a cookies.txt exported from a browser) is read too: its
  templates are synthesised from the User-Agent so they never contradict it.

``meta.json`` is stat-ed on every call; a snapshot is reloaded only when its generation
increases. Cookies live in memory only: allowlisted ``Set-Cookie`` updates are merged
there and are never written back.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import PhotosError

log = logging.getLogger(__name__)

SCHEMA = 2  # cookies plus the request headers the browser sent
SCHEMAS = frozenset({1, 2})  # 1: cookies only, headers synthesised from the User-Agent
HEADER_KINDS = ("page", "rpc", "image")
MAX_TEMPLATE_PAIRS = 32
MAX_HEADER_VALUE = 512
# Header names a template may carry (README "Session snapshot"). Anything else is ignored;
# request-specific headers (cookie, referer, accept-encoding) are added by the transport.
_TEMPLATE_NAMES = frozenset(
    {"user-agent", "accept", "accept-language", "content-type", "origin", "x-same-domain",
     "upgrade-insecure-requests", "priority"}
)  # fmt: skip
_TEMPLATE_PREFIXES = ("sec-ch-ua", "sec-fetch-")
_HEADER_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_CHROME_MAJOR = re.compile(r"\bChrome/(\d+)\.")
Template = tuple[tuple[str, str], ...]
# Cookie scopes the snapshot may carry; rows for any other domain are ignored.
COOKIE_DOMAINS = frozenset({"google.com", "photos.google.com", "photos.fife.usercontent.google.com"})
# The Google session cookies a snapshot carries; photos-mcp may also update them in memory
# from Set-Cookie.
SET_COOKIE_NAMES = frozenset(
    {
        "SID", "HSID", "SSID", "APISID", "SAPISID", "SIDCC", "NID",
        "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS",
        "__Secure-1PAPISID", "__Secure-3PAPISID", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
        "OSID", "__Secure-OSID",
    }
)  # fmt: skip
MAX_FILE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Cookie:
    domain: str  # without a leading dot
    host_only: bool
    path: str
    secure: bool
    expires: int  # epoch seconds, 0 = session cookie
    name: str
    value: str

    def matches(self, host: str, path: str, now: float) -> bool:
        if self.expires and self.expires < now:
            return False
        if self.host_only:
            if host != self.domain:
                return False
        elif host != self.domain and not host.endswith("." + self.domain):
            return False
        cookie_path = self.path or "/"
        return path == cookie_path or path.startswith(cookie_path.rstrip("/") + "/") or cookie_path == "/"


@dataclass
class CookieStore:
    cookies: list[Cookie] = field(default_factory=list)

    def header_for(self, host: str, path: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        chosen = [c for c in self.cookies if c.matches(host, path or "/", now)]
        chosen.sort(key=lambda c: len(c.path), reverse=True)  # RFC 6265 §5.4: longer paths first
        return "; ".join(f"{c.name}={c.value}" for c in chosen)

    def merge_set_cookie(self, host: str, headers: Iterable[str], now: float | None = None) -> int:
        """Merge allowlisted Set-Cookie updates from ``host``. Deletions are ignored."""
        now = time.time() if now is None else now
        merged = 0
        for header in headers:
            parsed = _parse_set_cookie(header, host, now)
            if parsed is None:
                continue
            for index, existing in enumerate(self.cookies):
                if (existing.name, existing.domain, existing.host_only, existing.path) == (
                    parsed.name,
                    parsed.domain,
                    parsed.host_only,
                    parsed.path,
                ):
                    self.cookies[index] = parsed
                    break
            else:
                self.cookies.append(parsed)
            merged += 1
        return merged

    def copy(self) -> CookieStore:
        return CookieStore(list(self.cookies))


def _parse_set_cookie(header: str, host: str, now: float) -> Cookie | None:
    parts = [p.strip() for p in header.split(";")]
    if not parts or "=" not in parts[0]:
        return None
    name, _, value = parts[0].partition("=")
    name = name.strip()
    if name not in SET_COOKIE_NAMES:
        return None
    attrs: dict[str, str] = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        attrs[key.strip().lower()] = val.strip()
    expires = 0
    if "max-age" in attrs:
        try:
            max_age = int(attrs["max-age"])
        except ValueError:
            return None
        if max_age <= 0:
            return None  # a deletion: keep the snapshot's value until the next generation
        expires = int(now) + max_age
    elif attrs.get("expires"):
        try:
            from email.utils import parsedate_to_datetime

            expires = int(parsedate_to_datetime(attrs["expires"]).timestamp())
        except (TypeError, ValueError):
            return None
        if expires <= now:
            return None
    domain_attr = attrs.get("domain", "").lstrip(".").lower()
    if domain_attr:
        if host != domain_attr and not host.endswith("." + domain_attr):
            return None
        domain, host_only = domain_attr, False
    else:
        domain, host_only = host, True
    if domain not in COOKIE_DOMAINS:
        return None
    path = attrs.get("path") or "/"
    return Cookie(domain, host_only, path, "secure" in attrs, expires, name, value.strip())


def parse_cookies_txt(text: str) -> list[Cookie]:
    """Netscape cookies.txt → cookies for the allowlisted domains. Raises ValueError on a bad row."""
    cookies: list[Cookie] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r\n")
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"line {number}: expected 7 tab-separated fields")
        domain_field, subdomains, path, secure, expires, name, value = fields
        if subdomains not in ("TRUE", "FALSE") or secure not in ("TRUE", "FALSE"):
            raise ValueError(f"line {number}: flags must be TRUE or FALSE")
        dotted = domain_field.startswith(".")
        if dotted != (subdomains == "TRUE"):
            raise ValueError(f"line {number}: host-only rows have no leading dot and FALSE, domain rows both")
        try:
            expiry = int(expires)
        except ValueError:
            raise ValueError(f"line {number}: expiry must be an integer") from None
        domain = domain_field.lstrip(".").lower()
        if domain not in COOKIE_DOMAINS or not name:
            continue
        cookies.append(Cookie(domain, not dotted, path or "/", secure == "TRUE", max(expiry, 0), name, value))
    return cookies


@dataclass(frozen=True)
class Snapshot:
    generation: int
    email: str
    account_index: int
    user_agent: str
    chromium_major: int
    verified_at: float  # epoch seconds
    cookies: CookieStore
    headers: dict[str, Template] = field(default_factory=dict)  # per kind, ordered
    schema: int = SCHEMA

    def describe(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        return {
            "generation": self.generation,
            "account": self.email,
            "verified_at": datetime.fromtimestamp(self.verified_at, UTC).isoformat(timespec="seconds"),
            "snapshot_age_s": max(0, int(now - self.verified_at)),
            "schema": self.schema,
        }


def parse_meta(raw: Any) -> dict[str, Any]:
    """Validate meta.json. Raises ValueError naming the field (never its value)."""
    if not isinstance(raw, dict):
        raise ValueError("meta.json is not an object")
    schema = raw.get("schema")
    if isinstance(schema, bool) or schema not in SCHEMAS:
        raise ValueError("unsupported schema")
    generation = raw.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("generation must be a positive integer")
    email = raw.get("email")
    if not isinstance(email, str) or "@" not in email:
        raise ValueError("email missing")
    if raw.get("account_index") != 0:
        raise ValueError("account_index must be 0")
    user_agent = raw.get("user_agent")
    if not isinstance(user_agent, str) or "Chrome/" not in user_agent:
        raise ValueError("user_agent must be a Chrome user agent")
    major = raw.get("chromium_major")
    if not isinstance(major, int) or isinstance(major, bool) or major < 1:
        raise ValueError("chromium_major must be a positive integer")
    verified = raw.get("verified_at")
    if isinstance(verified, str):
        try:
            verified_at = datetime.fromisoformat(verified.replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise ValueError("verified_at must be ISO 8601") from None
    elif isinstance(verified, int | float) and not isinstance(verified, bool):
        verified_at = float(verified)
    else:
        raise ValueError("verified_at missing")
    if schema == 1:
        headers = synthesise_templates(user_agent)
    else:
        ua_major = _CHROME_MAJOR.search(user_agent)
        if not ua_major or int(ua_major.group(1)) != major:
            raise ValueError("chromium_major must match the user_agent")
        headers = _parse_templates(raw.get("headers"), user_agent, generation)
    return {
        "generation": generation,
        "email": email.strip().lower(),
        "account_index": 0,
        "user_agent": user_agent,
        "chromium_major": major,
        "verified_at": verified_at,
        "headers": headers,
        "schema": schema,
    }


def template_name_allowed(name: str, kind: str) -> bool:
    if name == "content-type":
        return kind == "rpc"
    return name in _TEMPLATE_NAMES or name.startswith(_TEMPLATE_PREFIXES)


# The fetch metadata a real request of each kind carries; ``sec-fetch-dest`` must match it.
FETCH_METADATA = ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest")
KIND_DEST = {"page": "document", "rpc": "empty", "image": "image"}
_fallback_logged: set[tuple[int, str]] = set()
_fallback_lock = threading.Lock()


# Chrome's per-kind wire order, recorded from Chrome 149 with --log-net-log: HTTP/2 HEADERS
# frames of the app page, the app's batchexecute POSTs (the shape of 54 of 72; the rest, from a
# background caller, lack client hints) and the image CDN GETs. A CDP capture reports names
# sorted, so values come from the browser and order from here. The request-specific headers
# the transport adds (referer, accept-encoding, cookie) are part of the order: Chrome sends
# them last, just before priority. Not listed because never sent: pragma/cache-control (only
# there because the capture disables the cache), x-client-data and x-goog-* (README
# allowlist), content-length (curl's).
CANONICAL_ORDER: dict[str, tuple[str, ...]] = {
    "page": (
        "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-full-version", "sec-ch-ua-arch", "sec-ch-ua-platform",
        "sec-ch-ua-platform-version", "sec-ch-ua-model", "sec-ch-ua-bitness", "sec-ch-ua-wow64",
        "sec-ch-ua-full-version-list", "sec-ch-ua-form-factors", "upgrade-insecure-requests", "user-agent",
        "accept-language", "accept", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-user", "sec-fetch-dest",
        "referer", "accept-encoding", "cookie", "priority",
    ),
    "rpc": (
        "sec-ch-ua-full-version-list", "sec-ch-ua-platform", "sec-ch-ua", "sec-ch-ua-bitness", "sec-ch-ua-model",
        "sec-ch-ua-mobile", "x-same-domain", "sec-ch-ua-wow64", "sec-ch-ua-form-factors", "sec-ch-ua-arch",
        "sec-ch-ua-full-version", "content-type", "accept-language", "user-agent", "sec-ch-ua-platform-version",
        "accept", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "referer", "accept-encoding",
        "cookie", "priority",
    ),
    "image": (
        "sec-ch-ua-full-version-list", "sec-ch-ua-platform", "accept-language", "sec-ch-ua", "sec-ch-ua-bitness",
        "sec-ch-ua-model", "sec-ch-ua-mobile", "sec-ch-ua-form-factors", "sec-ch-ua-wow64", "sec-ch-ua-arch",
        "sec-ch-ua-full-version", "user-agent", "sec-ch-ua-platform-version", "accept", "sec-fetch-site",
        "sec-fetch-mode", "sec-fetch-dest", "referer", "accept-encoding", "cookie", "priority",
    ),
}  # fmt: skip
# Where Chrome put the headers it sends that the order does not know (x-client-data): an
# unknown name keeps its relative order just before the fetch metadata.
_UNKNOWN_SLOT = "sec-fetch-site"


def canonical_order(kind: str, headers: Iterable[tuple[str, str]]) -> Template:
    """``headers`` in Chrome's wire order for ``kind``; unknown names keep their relative
    order just before ``sec-fetch-site`` (a stable position)."""
    rank = {name: index for index, name in enumerate(CANONICAL_ORDER[kind])}
    anchor = rank[_UNKNOWN_SLOT]

    def key(item: tuple[int, tuple[str, str]]) -> tuple[int, int, int]:
        position, (name, _) = item
        return (rank[name], 0, 0) if name in rank else (anchor, -1, position)

    return tuple(pair for _, pair in sorted(enumerate(headers), key=key))


def _usable_capture(kind: str, template: Template) -> bool:
    """A captured template is replayed only with complete, consistent fetch metadata: a
    provisional renderer capture lacks it, and replaying that would mark every request odd."""
    values = dict(template)
    return all(values.get(name) for name in FETCH_METADATA) and values["sec-fetch-dest"] == KIND_DEST[kind]


def _parse_templates(raw: Any, user_agent: str, generation: int = 0) -> dict[str, Template]:
    """Validate ``headers``: all three kinds, ordered pairs, allowlisted names, sane values.

    Structural problems reject the snapshot; a name outside the allowlist is dropped (the
    exporter should never write one) and never sent. A kind whose fetch metadata is missing
    or contradicts the kind falls back to the User-Agent-synthesised template for that kind
    only (logged once per generation, without values).
    """
    if not isinstance(raw, dict):
        raise ValueError("headers missing")
    out: dict[str, Template] = {}
    for kind in HEADER_KINDS:
        pairs = raw.get(kind)
        if not isinstance(pairs, list) or not pairs:
            raise ValueError(f"headers.{kind} missing")
        if len(pairs) > MAX_TEMPLATE_PAIRS:
            raise ValueError(f"headers.{kind} has too many pairs")
        kept: list[tuple[str, str]] = []
        seen: set[str] = set()
        dropped = 0
        for pair in pairs:
            if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(v, str) for v in pair)):
                raise ValueError(f"headers.{kind} entries must be [name, value] strings")
            name, value = pair
            if not _HEADER_NAME.fullmatch(name):
                raise ValueError(f"headers.{kind} has a malformed or non-lower-case name")
            if len(value) > MAX_HEADER_VALUE or "\r" in value or "\n" in value or "\x00" in value:
                raise ValueError(f"headers.{kind} has an over-long or multi-line value")
            if name in seen:
                raise ValueError(f"headers.{kind} repeats a name")
            seen.add(name)
            if not template_name_allowed(name, kind):
                dropped += 1
                continue
            kept.append((name, value))
        if dropped:
            log.warning("headers.%s: ignored %d header(s) outside the allowlist", kind, dropped)
        if dict(kept).get("user-agent") != user_agent:
            raise ValueError(f"headers.{kind} user-agent must equal user_agent")
        out[kind] = canonical_order(kind, tuple(kept))
    unusable = [kind for kind in HEADER_KINDS if not _usable_capture(kind, out[kind])]
    if unusable:
        synthesised = synthesise_templates(user_agent)
        for kind in unusable:
            out[kind] = synthesised[kind]
            with _fallback_lock:
                if len(_fallback_logged) > 1000:
                    _fallback_logged.clear()
                first = (generation, kind) not in _fallback_logged
                _fallback_logged.add((generation, kind))
            if first:
                log.warning(
                    "generation %d headers.%s lacks complete fetch metadata; using the synthesised template",
                    generation,
                    kind,
                )
    return out


# -- schema 1: templates synthesised from the User-Agent ------------------------------------

# Chromium's GREASE brand (components/embedder_support/user_agent_utils.cc).
_GREASE_CHARS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_GREASE_VERSIONS = ("8", "99", "24")


def sec_ch_ua(major: int) -> str:
    """The ``sec-ch-ua`` Chromium sends for ``major``: Chromium plus the seeded GREASE brand."""
    grease = (
        f"Not{_GREASE_CHARS[major % len(_GREASE_CHARS)]}A{_GREASE_CHARS[(major + 1) % len(_GREASE_CHARS)]}Brand",
        _GREASE_VERSIONS[major % len(_GREASE_VERSIONS)],
    )
    brands = [grease, ("Chromium", str(major))]
    if major % 2:
        brands.reverse()
    return ", ".join(f'"{brand}";v="{version}"' for brand, version in brands)


def ua_platform(user_agent: str) -> tuple[str, bool]:
    """(``sec-ch-ua-platform`` value, mobile) implied by a Chrome User-Agent."""
    if "Android" in user_agent:
        return "Android", True
    if "CrOS" in user_agent:
        return "Chrome OS", False
    if "Windows" in user_agent:
        return "Windows", False
    if "Macintosh" in user_agent or "Mac OS X" in user_agent:
        return "macOS", False
    if "Linux" in user_agent or "X11" in user_agent:
        return "Linux", False
    return "Unknown", False


ACCEPT_LANGUAGE = "en-US,en;q=0.9"


def synthesise_templates(user_agent: str) -> dict[str, Template]:
    """Per-kind templates in Chrome's order, consistent with ``user_agent`` (schema 1)."""
    found = _CHROME_MAJOR.search(user_agent)
    major = int(found.group(1)) if found else 0
    platform, mobile = ua_platform(user_agent)
    ch = [
        ("sec-ch-ua", sec_ch_ua(major)),
        ("sec-ch-ua-mobile", "?1" if mobile else "?0"),
        ("sec-ch-ua-platform", f'"{platform}"'),
    ]
    page = (
        *ch,
        ("upgrade-insecure-requests", "1"),
        ("user-agent", user_agent),
        ("accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,"
                   "*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        ("sec-fetch-site", "none"),
        ("sec-fetch-mode", "navigate"),
        ("sec-fetch-user", "?1"),
        ("sec-fetch-dest", "document"),
        ("accept-language", ACCEPT_LANGUAGE),
        ("priority", "u=0, i"),
    )  # fmt: skip
    rpc = (
        ch[2],
        ("x-same-domain", "1"),
        ("user-agent", user_agent),
        ch[0],
        ("content-type", "application/x-www-form-urlencoded;charset=UTF-8"),
        ch[1],
        ("accept", "*/*"),
        ("origin", "https://photos.google.com"),
        ("sec-fetch-site", "same-origin"),
        ("sec-fetch-mode", "cors"),
        ("sec-fetch-dest", "empty"),
        ("accept-language", ACCEPT_LANGUAGE),
        ("priority", "u=1, i"),
    )
    image = (
        ch[2],
        ("user-agent", user_agent),
        ch[0],
        ch[1],
        ("accept", "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"),
        ("sec-fetch-site", "same-site"),
        ("sec-fetch-mode", "no-cors"),
        ("sec-fetch-dest", "image"),
        ("accept-language", ACCEPT_LANGUAGE),
        ("priority", "i"),
    )
    return {
        kind: canonical_order(kind, template) for kind, template in (("page", page), ("rpc", rpc), ("image", image))
    }


def _read_small(path: Path) -> str:
    with open(path, "rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} is too large")
    return data.decode("utf-8")


def read_snapshot(directory: Path) -> Snapshot:
    """Read one consistent snapshot; ``session_unavailable`` when it is missing or malformed."""
    meta_path, cookies_path = directory / "meta.json", directory / "cookies.txt"
    for _ in range(2):
        try:
            before = os.stat(meta_path)
            meta = parse_meta(json.loads(_read_small(meta_path)))
            cookies = parse_cookies_txt(_read_small(cookies_path))
            after = os.stat(meta_path)
        except FileNotFoundError:
            raise PhotosError("session_unavailable", log_detail="snapshot file missing") from None
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise PhotosError("session_unavailable", log_detail=f"snapshot unreadable: {_safe(exc)}") from None
        if _stat_key(before) != _stat_key(after):
            continue  # the exporter published while we read; read again
        if not cookies:
            raise PhotosError("session_unavailable", log_detail="snapshot has no usable cookies")
        return Snapshot(cookies=CookieStore(cookies), **meta)
    raise PhotosError("session_unavailable", log_detail="snapshot changed twice while reading")


def _safe(exc: BaseException) -> str:
    # Our parsers name fields and line numbers only; OS errors carry a path, not secrets.
    return f"{type(exc).__name__}: {exc}"[:200]


def _stat_key(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_ino, st.st_mtime_ns, st.st_size)


class SessionReader:
    """Loads the snapshot lazily, stat-ing meta.json per call and reloading on a newer generation."""

    def __init__(self, directory: Path, *, missing_code: str = "session_unavailable") -> None:
        self.directory = Path(directory)
        # A linked account without a snapshot is simply not signed in (yet, or any more): ``not_signed_in``.
        self.missing_code = missing_code
        self._lock = threading.Lock()
        self._stat: tuple[int, int, int] | None = None
        self._snapshot: Snapshot | None = None

    def current(self) -> Snapshot:
        with self._lock:
            try:
                key = _stat_key(os.stat(self.directory / "meta.json"))
            except OSError:
                self._stat = None
                raise PhotosError(self.missing_code, log_detail="meta.json missing") from None
            if self._snapshot is not None and key == self._stat:
                return self._snapshot
            fresh = read_snapshot(self.directory)
            self._stat = key
            if self._snapshot is None or fresh.generation > self._snapshot.generation:
                if self._snapshot is not None:
                    log.info("session generation %d -> %d", self._snapshot.generation, fresh.generation)
                self._snapshot = fresh
            return self._snapshot

    def loaded(self) -> Snapshot | None:
        return self._snapshot
