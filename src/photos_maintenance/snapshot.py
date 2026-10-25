"""Netscape cookie snapshot: export, staging, and atomic publication.

The published directory is the contract with the reader:

``cookies.txt``
    Netscape format, one cookie per line:
    ``domain<TAB>include_subdomains<TAB>path<TAB>secure<TAB>expires<TAB>name<TAB>value``.
    A host-only cookie has a domain without a leading dot and
    ``include_subdomains`` ``FALSE``; a domain cookie has a leading dot and
    ``TRUE``. Session cookies have ``expires`` 0.
``meta.json``
    ``schema`` 2, a monotonic ``generation``, ``email``, ``account_index`` 0,
    ``user_agent``, ``chromium_major``, ``verified_at`` (ISO 8601 UTC) and
    ``headers``: for each request kind (``page``, ``rpc``, ``image``) the
    ``[name, value]`` pairs Chrome really sent for one request of that kind
    during the warm-up (from CDP, so names are sorted; the reader applies
    Chrome's wire order), reduced to :data:`HEADER_ALLOWLIST`. Never a
    cookie, credential, referer, id or token.

Publication replaces ``cookies.txt`` first and ``meta.json`` last; the reader
treats a new ``meta.json`` generation as the commit point.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from signin_window.state import PrivateStateError, atomic_private_write

COOKIES_FILE = "cookies.txt"
META_FILE = "meta.json"
SNAPSHOT_SCHEMA = 2
#: Request kinds whose real header templates schema 2 requires.
HEADER_KINDS = ("page", "rpc", "image")
#: Exact header names a template may carry (plus every ``sec-ch-ua*`` and
#: ``sec-fetch-*``); ``content-type`` only for ``rpc``.
HEADER_ALLOWLIST = frozenset(
    {
        "user-agent",
        "accept",
        "accept-language",
        "origin",
        "x-same-domain",
        "upgrade-insecure-requests",
        "priority",
    }
)
_HEADER_PREFIXES = ("sec-ch-ua", "sec-fetch-")
_RPC_ONLY_HEADERS = frozenset({"content-type"})
_REQUIRED_FETCH_METADATA = ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest")
_FETCH_DEST_BY_KIND = {"page": "document", "rpc": "empty", "image": "image"}
_MAX_HEADER_VALUE = 512
_MAX_HEADER_PAIRS = 32
_HEADER_NAME = re.compile(r"^[a-z0-9-]+$")
_HEADER_VALUE_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")

#: An ordered ``(name, value)`` request-header template.
HeaderTemplate = tuple[tuple[str, str], ...]
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
_STAGING_PREFIX = ".staging-"
_MAX_SNAPSHOT_BYTES = 256 * 1024
_FORBIDDEN_VALUE = re.compile(r"[\t\r\n]")


class SnapshotError(RuntimeError):
    """A snapshot could not be rendered, read or published."""


@dataclass(frozen=True, slots=True)
class ExportCookie:
    domain: str
    host_only: bool
    path: str
    secure: bool
    expires: int
    name: str
    value: str

    @property
    def netscape_domain(self) -> str:
        return self.domain if self.host_only else f".{self.domain}"


@dataclass(frozen=True, slots=True)
class SnapshotMeta:
    generation: int
    email: str
    user_agent: str
    chromium_major: int
    verified_at: datetime
    headers: Mapping[str, HeaderTemplate]
    account_index: int = 0
    schema: int = SNAPSHOT_SCHEMA

    def to_json(self) -> dict[str, object]:
        validate_header_templates(self.headers, user_agent=self.user_agent)
        if chromium_major(self.user_agent) != self.chromium_major:
            raise SnapshotError("chromium_major must match the user agent")
        return {
            "schema": self.schema,
            "generation": self.generation,
            "email": self.email,
            "account_index": self.account_index,
            "user_agent": self.user_agent,
            "chromium_major": self.chromium_major,
            "verified_at": format_utc(self.verified_at),
            "headers": {kind: [[name, value] for name, value in self.headers[kind]] for kind in HEADER_KINDS},
        }


def _header_allowed(kind: str, name: str) -> bool:
    return (
        name in HEADER_ALLOWLIST or (kind == "rpc" and name in _RPC_ONLY_HEADERS) or name.startswith(_HEADER_PREFIXES)
    )


def filter_header_template(kind: str, pairs: Iterable[tuple[str, str]]) -> HeaderTemplate:
    """Reduce raw request headers to the allowlist, keeping their input order.

    Names are lower-cased; pseudo-headers, cookies, referers, ids and tokens
    never pass. A repeated name keeps its first value.
    """

    kept: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_name, value in pairs:
        name = str(raw_name).strip().lower()
        if name in seen or not _header_allowed(kind, name):
            continue
        seen.add(name)
        kept.append((name, str(value)))
    return tuple(kept)


def validate_header_template(kind: str, template: HeaderTemplate, *, user_agent: str) -> None:
    """Enforce the schema-2 rules for one kind; raise ``SnapshotError``."""

    if kind not in HEADER_KINDS:
        raise SnapshotError("unknown header template kind")
    if not template or len(template) > _MAX_HEADER_PAIRS:
        raise SnapshotError("header template must have 1 to 32 pairs")
    names = [name for name, _value in template]
    if len(set(names)) != len(names):
        raise SnapshotError("header template names must be unique")
    for name, value in template:
        if not isinstance(name, str) or not isinstance(value, str):
            raise SnapshotError("header template pairs must be strings")
        if not _HEADER_NAME.match(name) or not _header_allowed(kind, name):
            raise SnapshotError("header template carries a forbidden name")
        if len(value) > _MAX_HEADER_VALUE or _HEADER_VALUE_FORBIDDEN.search(value):
            raise SnapshotError("header template value is not a short single line")
    values = dict(template)
    if values.get("user-agent") != user_agent:
        raise SnapshotError("header template user-agent must equal user_agent")
    # Fetch metadata is present on every real request Chrome sends; its
    # absence means renderer-side provisional headers, never the wire.
    if any(not values.get(name) for name in _REQUIRED_FETCH_METADATA):
        raise SnapshotError("header template lacks sec-fetch-site/mode/dest")
    if values["sec-fetch-dest"] != _FETCH_DEST_BY_KIND[kind]:
        raise SnapshotError("header template sec-fetch-dest contradicts its kind")


def validate_header_templates(headers: Mapping[str, HeaderTemplate], *, user_agent: str) -> None:
    if set(headers) != set(HEADER_KINDS):
        raise SnapshotError("header templates must cover page, rpc and image")
    for kind in HEADER_KINDS:
        validate_header_template(kind, headers[kind], user_agent=user_agent)


def format_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise SnapshotError("snapshot timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_export_cookies(
    cookies: Iterable[Mapping[str, object]],
    *,
    hosts: frozenset[str],
    names: frozenset[str],
) -> list[ExportCookie]:
    """Filter browser-context cookies by exact host and name allowlists.

    Playwright reports a host-only cookie with a bare domain and a domain
    cookie with a leading dot; that is the only host-only signal it exposes.
    """

    selected: dict[tuple[str, bool, str, str], ExportCookie] = {}
    for raw in cookies:
        name = raw.get("name")
        value = raw.get("value")
        domain = raw.get("domain")
        if not isinstance(name, str) or name not in names:
            continue
        if not isinstance(value, str) or not isinstance(domain, str):
            continue
        host_only = not domain.startswith(".")
        bare = domain.lstrip(".").lower()
        if bare not in hosts:
            continue
        path = raw.get("path")
        expires = raw.get("expires")
        cookie = ExportCookie(
            domain=bare,
            host_only=host_only,
            path=path if isinstance(path, str) and path.startswith("/") else "/",
            secure=bool(raw.get("secure", False)),
            expires=(
                int(expires)
                if isinstance(expires, int | float) and not isinstance(expires, bool) and expires > 0
                else 0
            ),
            name=name,
            value=value,
        )
        selected[(cookie.domain, cookie.host_only, cookie.path, cookie.name)] = cookie
    return sorted(
        selected.values(),
        key=lambda item: (item.domain, item.host_only, item.path, item.name),
    )


def render_cookies_txt(cookies: Iterable[ExportCookie]) -> bytes:
    lines = [NETSCAPE_HEADER]
    for cookie in cookies:
        fields = (
            cookie.netscape_domain,
            "FALSE" if cookie.host_only else "TRUE",
            cookie.path,
            "TRUE" if cookie.secure else "FALSE",
            str(cookie.expires),
            cookie.name,
            cookie.value,
        )
        if any(_FORBIDDEN_VALUE.search(field) for field in fields):
            raise SnapshotError("cookie fields cannot contain tabs or newlines")
        lines.append("\t".join(fields))
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_cookies_txt(payload: bytes) -> list[ExportCookie]:
    """Read back a rendered ``cookies.txt`` (strictly, as the reader should)."""

    cookies: list[ExportCookie] = []
    for line in payload.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise SnapshotError("cookies.txt row must have seven fields")
        domain, subdomains, path, secure, expires, name, value = fields
        if subdomains not in {"TRUE", "FALSE"} or secure not in {"TRUE", "FALSE"}:
            raise SnapshotError("cookies.txt flags must be TRUE or FALSE")
        host_only = subdomains == "FALSE"
        if host_only == domain.startswith("."):
            raise SnapshotError("cookies.txt domain dot must match subdomain flag")
        try:
            expiry = int(expires)
        except ValueError as error:
            raise SnapshotError("cookies.txt expiry must be an integer") from error
        cookies.append(
            ExportCookie(
                domain=domain.lstrip("."),
                host_only=host_only,
                path=path,
                secure=secure == "TRUE",
                expires=expiry,
                name=name,
                value=value,
            )
        )
    return cookies


def render_meta(meta: SnapshotMeta) -> bytes:
    """Two-space JSON with one ``[name, value]`` pair per line.

    The layout is part of the contract: it is byte-identical to the sample
    the reader checks in (tests/fixtures/session/meta.json).
    """

    payload = meta.to_json()
    headers = payload.pop("headers")
    if not isinstance(headers, dict):
        raise SnapshotError("snapshot headers must be a mapping")
    lines = ["{"]
    for key, value in payload.items():
        lines.append(f"  {json.dumps(key)}: {json.dumps(value)},")
    lines.append('  "headers": {')
    kinds = list(headers.items())
    for index, (kind, pairs) in enumerate(kinds):
        lines.append(f"    {json.dumps(kind)}: [")
        rows = [f"      {json.dumps(pair)}" for pair in pairs]
        lines.append(",\n".join(rows))
        lines.append("    ]" + ("," if index < len(kinds) - 1 else ""))
    lines.append("  }")
    lines.append("}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def published_generation(export_dir: Path) -> int:
    """The generation of the currently published snapshot, or 0."""

    path = export_dir / META_FILE
    try:
        if path.is_symlink():
            return 0
        raw = path.read_bytes()
    except OSError:
        return 0
    if len(raw) > _MAX_SNAPSHOT_BYTES:
        return 0
    try:
        payload = json.loads(raw)
        value = payload["generation"]
    except (ValueError, KeyError, TypeError):
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def prepare_export_dir(export_dir: Path) -> None:
    if export_dir.is_symlink():
        raise SnapshotError("export directory cannot be a symlink")
    try:
        export_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISDIR(export_dir.lstat().st_mode):
            raise SnapshotError("export path is not a directory")
    except OSError as error:
        raise SnapshotError("export directory is unavailable") from error


def discard_staging(export_dir: Path) -> None:
    """Remove staging directories left by this command (never anything else)."""

    try:
        candidates = list(export_dir.iterdir())
    except OSError:
        return
    for candidate in candidates:
        if not candidate.name.startswith(_STAGING_PREFIX):
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        shutil.rmtree(candidate, ignore_errors=True)


def stage_snapshot(
    export_dir: Path,
    cookies: list[ExportCookie],
    meta: SnapshotMeta,
) -> Path:
    staging = export_dir / f"{_STAGING_PREFIX}{uuid4().hex}"
    try:
        staging.mkdir(mode=0o700)
        atomic_private_write(staging / COOKIES_FILE, render_cookies_txt(cookies))
        atomic_private_write(staging / META_FILE, render_meta(meta))
    except (OSError, PrivateStateError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise SnapshotError("snapshot staging failed") from error
    return staging


def publish_snapshot(
    export_dir: Path,
    staging: Path,
    *,
    write: Callable[[Path, bytes], None] = atomic_private_write,
) -> None:
    """Move a verified staging pair into place: cookies first, meta last.

    Each file is replaced through a temporary file, fsync and rename inside
    ``export_dir`` itself, so a reader never sees a partial file and only sees
    a new ``meta.json`` once its ``cookies.txt`` is already durable.
    """

    try:
        payloads = {name: (staging / name).read_bytes() for name in (COOKIES_FILE, META_FILE)}
        for name in (COOKIES_FILE, META_FILE):
            write(export_dir / name, payloads[name])
    except (OSError, PrivateStateError) as error:
        raise SnapshotError("snapshot publication failed") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def chromium_major(user_agent: str) -> int:
    match = re.search(r"Chrome/(\d{2,4})\.", user_agent)
    if match is None:
        raise SnapshotError("browser user agent has no Chromium version")
    return int(match.group(1))


__all__ = [
    "COOKIES_FILE",
    "HEADER_ALLOWLIST",
    "HEADER_KINDS",
    "META_FILE",
    "NETSCAPE_HEADER",
    "SNAPSHOT_SCHEMA",
    "ExportCookie",
    "HeaderTemplate",
    "SnapshotError",
    "SnapshotMeta",
    "chromium_major",
    "discard_staging",
    "filter_header_template",
    "format_utc",
    "parse_cookies_txt",
    "prepare_export_dir",
    "publish_snapshot",
    "published_generation",
    "render_cookies_txt",
    "render_meta",
    "select_export_cookies",
    "stage_snapshot",
    "validate_header_template",
    "validate_header_templates",
]
