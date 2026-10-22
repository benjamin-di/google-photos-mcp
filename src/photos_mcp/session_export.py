"""photos-mcp-session: write the session snapshot photos-mcp reads (``PHOTOS_SESSION_DIR``).

Two ways to get one:

``import``
    A ``cookies.txt`` (Netscape format) exported from your own signed-in browser, plus the
    account e-mail and that browser's User-Agent. Writes a schema-1 snapshot: photos-mcp
    synthesises the request headers from the User-Agent. Before exporting, open
    photos.google.com and one photo, so the image host's own cookies exist.

``browser install`` / ``login`` / ``export`` (needs the ``session`` extra: Playwright)
    A dedicated browser profile: ``install`` downloads Playwright's Chromium once; sign in
    by hand once (``login``), then ``export`` loads the
    library in that profile, opens one photo and writes a schema-2 snapshot with the
    cookies and the request headers the browser really sent. Run ``export`` again (for
    example hourly) to keep the snapshot fresh; it never signs in by itself.

The snapshot carries full authority over the Google account: the directory is created
0700 and the files 0600. ``cookies.txt`` is written first and ``meta.json`` last (the
reader's commit point), each atomically, with ``generation`` one above the previous one.
Every snapshot is read back with photos-mcp's own reader before it is published.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .session import (
    COOKIE_DOMAINS,
    FETCH_METADATA,
    HEADER_KINDS,
    KIND_DEST,
    SET_COOKIE_NAMES,
    Cookie,
    Template,
    parse_cookies_txt,
    parse_meta,
    synthesise_templates,
    template_name_allowed,
)

APP_URL = "https://photos.google.com/"
APP_HOST = "photos.google.com"
IMAGE_HOST = "photos.fife.usercontent.google.com"
SIGN_IN_HOST = "accounts.google.com"
SESSION_COOKIES = frozenset({"SID", "__Secure-1PSID"})
TILE_SELECTOR = "a[href*='/photo/']"
_CHROME_MAJOR = re.compile(r"\bChrome/(\d+)\.")
_GD = re.compile(r"<script\b[^>]*\bdata-id=[\"']_gd[\"']", re.IGNORECASE)
_EMAIL_KEY = re.compile(r"\"oPEP7c\"\s*:\s*(\"(?:[^\"\\]|\\.)*\")")


class ExportError(Exception):
    """The snapshot cannot be written; the message says what to do. ``code`` says why, for the login service's
    keeper: ``signed_out`` (Google's sign-in page or signed-out landing page), ``challenge`` (Google wants the person
    to prove it is them again), ``interstitial`` (an account notice), ``no_account``, ``account_mismatch``,
    ``empty_library`` or ``failed``."""

    def __init__(self, message: str, code: str = "failed") -> None:
        super().__init__(message)
        self.code = code


# -- the snapshot (pure) ----------------------------------------------------------------------


def select_cookies(cookies: Iterable[Cookie]) -> list[Cookie]:
    """Only the Google session cookies photos-mcp uses, for the hosts it talks to."""
    chosen = {
        (c.domain, c.host_only, c.path, c.name): c
        for c in cookies
        if c.domain in COOKIE_DOMAINS and c.name in SET_COOKIE_NAMES
    }
    found = sorted(chosen.values(), key=lambda c: (c.domain, c.host_only, c.path, c.name))
    if not any(c.name in SESSION_COOKIES for c in found):
        raise ExportError("no Google sign-in cookie (SID or __Secure-1PSID): sign in to photos.google.com first")
    if not any(c.domain == IMAGE_HOST for c in found):
        raise ExportError(f"no cookie for {IMAGE_HOST}: open one photo on photos.google.com, then export again")
    return found


def browser_cookies(raw: Iterable[Mapping[str, Any]]) -> list[Cookie]:
    """Playwright's cookie dicts as cookies; a leading dot marks a domain cookie."""
    cookies = []
    for item in raw:
        domain, name, value = item.get("domain"), item.get("name"), item.get("value")
        if not (isinstance(domain, str) and isinstance(name, str) and isinstance(value, str)):
            continue
        expires = item.get("expires")
        cookies.append(
            Cookie(
                domain=domain.lstrip(".").lower(),
                host_only=not domain.startswith("."),
                path=item.get("path") if isinstance(item.get("path"), str) else "/",
                secure=bool(item.get("secure")),
                expires=int(expires) if isinstance(expires, int | float) and expires > 0 else 0,
                name=name,
                value=value,
            )
        )
    return cookies


def render_cookies_txt(cookies: Iterable[Cookie]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        fields = (
            c.domain if c.host_only else f".{c.domain}",
            "FALSE" if c.host_only else "TRUE",
            c.path or "/",
            "TRUE" if c.secure else "FALSE",
            str(c.expires),
            c.name,
            c.value,
        )
        if any(ch in field for field in fields for ch in "\t\r\n"):
            raise ExportError("a cookie field contains a tab or a line break")
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"


def chromium_major(user_agent: str) -> int:
    if "Headless" in user_agent:
        raise ExportError("the User-Agent is a headless browser's; Google treats those differently")
    found = _CHROME_MAJOR.search(user_agent)
    if not found:
        raise ExportError("the User-Agent must be a Chrome or Chromium one (it names Chrome/<version>)")
    return int(found.group(1))


def build_meta(
    *,
    generation: int,
    email: str,
    user_agent: str,
    headers: Mapping[str, Template] | None,
    verified_at: datetime,
) -> dict[str, Any]:
    """meta.json: schema 2 with ``headers`` (one ordered template per kind), else schema 1."""
    meta: dict[str, Any] = {
        "schema": 2 if headers else 1,
        "generation": generation,
        "email": email.strip().lower(),
        "account_index": 0,
        "user_agent": user_agent,
        "chromium_major": chromium_major(user_agent),
        "verified_at": verified_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if headers:
        meta["headers"] = {kind: [list(pair) for pair in headers[kind]] for kind in HEADER_KINDS}
    return meta


# Google's own sign-in pages: what a signed-out profile is sent to.
_SIGN_IN_PATHS = re.compile(
    r"/(?:ServiceLogin|AccountChooser|InteractiveLogin|signin/v2/identifier|v3/signin/identifier|"
    r"v3/signin/accountchooser|signin/identifier|signin/chooser)(?:/|$)",
    re.IGNORECASE,
)


def signed_out_page(url: str) -> str | None:
    """How a page Google sent the profile to reads: ``signed_out`` (its sign-in page, or the signed-out landing page
    google.com/photos/about), ``challenge`` (any other accounts.google.com page: Google wants the person to prove it
    is them), ``interstitial`` (an account notice under accounts.google.com/web/ or gds.google.com: not a sign-out),
    or None (anything else, Google Photos included)."""
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path or "/"
    if host == SIGN_IN_HOST:
        if path.startswith("/web/"):
            return "interstitial"
        return "signed_out" if _SIGN_IN_PATHS.match(path) else "challenge"
    if host == "gds.google.com":
        return "interstitial"
    if host in ("google.com", "www.google.com") and path.startswith("/photos/about"):
        return "signed_out"
    return None


def settled_url(page: Any, *, seconds: float = 15.0) -> str:
    """The page's address once Google's redirects have stopped (the same for 2 s, or after ``seconds``)."""
    last, stable_since = page.url, time.monotonic()
    deadline = stable_since + seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if page.url != last:
            last, stable_since = page.url, time.monotonic()
        elif time.monotonic() - stable_since >= 2.0:
            break
    return last


def published_generation(out_dir: Path) -> int:
    try:
        value = json.loads((out_dir / "meta.json").read_text()).get("generation")
    except (OSError, ValueError, AttributeError):
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def unpublish(out_dir: Path) -> None:
    """Withdraw a snapshot whose profile is signed out: meta.json first (the reader's commit point), then the
    cookies. The next snapshot must still count its generation on from the last one (``min_generation``)."""
    for name in ("meta.json", "cookies.txt"):
        (out_dir / name).unlink(missing_ok=True)


def publish(out_dir: Path, cookies: list[Cookie], meta: dict[str, Any]) -> Path:
    """Check the pair with photos-mcp's reader, then write cookies.txt and meta.json last."""
    cookies_txt = render_cookies_txt(cookies)
    meta_json = json.dumps(meta, indent=2) + "\n"
    try:
        parse_meta(json.loads(meta_json))
        if not parse_cookies_txt(cookies_txt):
            raise ValueError("no usable cookies")
    except ValueError as exc:
        raise ExportError(f"the snapshot would not be readable: {exc.args[0] if exc.args else exc}") from None
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(out_dir / "cookies.txt", cookies_txt)
    _write_private(out_dir / "meta.json", meta_json)
    return out_dir


def _write_private(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.chmod(tmp, 0o600)  # mkstemp creates 0600 on POSIX already; os.fchmod is POSIX-only before 3.13
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def page_email(html: str) -> str | None:
    """The signed-in account's e-mail from the app page (``WIZ_global_data.oPEP7c``)."""
    if not _GD.search(html):
        return None
    found = _EMAIL_KEY.search(html)
    if not found:
        return None
    try:
        email = json.loads(found.group(1))
    except ValueError:
        return None
    return email if isinstance(email, str) and "@" in email else None


# -- request headers as the browser sent them ---------------------------------------------------


class WireHeaders:
    """Request headers from CDP ``Network.requestWillBeSentExtraInfo`` (what really went on the
    wire; Playwright's own request headers can be the renderer's provisional ones), paired with
    ``Network.requestWillBeSent`` for the url, method and resource type. Only allowlisted
    names are kept, so cookies and tokens are never held."""

    def __init__(self) -> None:
        self._sent: dict[str, list[tuple[str, str, str]]] = {}
        self._wire: dict[str, list[Template]] = {}

    def on_sent(self, params: Mapping[str, Any]) -> None:
        try:
            request = params["request"]
            hop = (str(request["url"]), str(request["method"]), str(params.get("type", "")))
            self._sent.setdefault(str(params["requestId"]), []).append(hop)
        except (KeyError, TypeError):
            pass

    def on_wire(self, params: Mapping[str, Any]) -> None:
        try:
            raw = params["headers"].items()
            request_id = str(params["requestId"])
        except (KeyError, TypeError, AttributeError):
            return
        kept = tuple((str(n).lower(), str(v)) for n, v in raw if template_name_allowed(str(n).lower(), "rpc"))
        self._wire.setdefault(request_id, []).append(kept)

    def templates(self, user_agent: str) -> tuple[dict[str, Template], list[str]]:
        """The first complete template per kind; kinds never seen get the synthesised one.
        Returns (templates, synthesised kinds)."""
        captured: dict[str, Template] = {}
        for request_id, hops in self._sent.items():
            wire = self._wire.get(request_id, [])
            if len(wire) != len(hops):
                continue  # ambiguous redirect pairing: skip
            for (url, method, resource_type), headers in zip(hops, wire, strict=True):
                kind = request_kind(url, method, resource_type)
                if kind is None or kind in captured:
                    continue
                template = tuple((n, v) for n, v in headers if template_name_allowed(n, kind))
                values = dict(template)
                if values.get("user-agent") != user_agent:
                    continue
                if all(values.get(name) for name in FETCH_METADATA) and values["sec-fetch-dest"] == KIND_DEST[kind]:
                    captured[kind] = template
        synthesised = [kind for kind in HEADER_KINDS if kind not in captured]
        fallback = synthesise_templates(user_agent)
        return {kind: captured.get(kind, fallback[kind]) for kind in HEADER_KINDS}, synthesised


def request_kind(url: str, method: str, resource_type: str) -> str | None:
    parts = urlsplit(url)
    host, method = (parts.hostname or "").lower(), method.upper()
    if host == IMAGE_HOST and method == "GET":
        return "image"
    if host != APP_HOST:
        return None
    if method == "POST" and parts.path.endswith("/data/batchexecute"):
        return "rpc"
    if method == "GET" and resource_type == "Document":
        return "page"
    return None


# -- commands -------------------------------------------------------------------------------


def import_cookies(cookies_file: Path, *, email: str, user_agent: str, out_dir: Path) -> dict[str, Any]:
    if "@" not in email:
        raise ExportError("--email must be the Google account's e-mail address")
    try:
        cookies = parse_cookies_txt(cookies_file.read_text())
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError(f"cannot read {cookies_file}: {type(exc).__name__}") from None
    except ValueError as exc:
        raise ExportError(f"{cookies_file} is not a Netscape cookies.txt: {exc.args[0]}") from None
    meta = build_meta(
        generation=published_generation(out_dir) + 1,
        email=email,
        user_agent=user_agent,
        headers=None,
        verified_at=datetime.now(UTC),
    )
    publish(out_dir, select_cookies(cookies), meta)
    return {"out": str(out_dir), "schema": 1, "generation": meta["generation"]}


def _launch(playwright: Any, profile: Path, *, channel: str | None) -> Any:
    # Always headed: a headless browser names itself HeadlessChrome, which Google treats
    # differently and which chromium_major() refuses. On a server, run under xvfb-run.
    options: dict[str, Any] = {"headless": False, "ignore_default_args": ["--enable-automation"]}
    if channel:
        options["channel"] = channel
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(str(profile), **options)


def _playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ExportError(
            "the browser commands need Playwright: install 'photos-mcp[session]', "
            "then run photos-mcp-session browser install"
        ) from None
    return sync_playwright()


def browser_install(*, with_deps: bool = False) -> dict[str, Any]:
    """Download the Chromium build this Playwright version drives (once per version); with
    ``with_deps`` also its Linux system libraries (needs root)."""
    _playwright()  # a clear message when the extra is missing
    command = [sys.executable, "-m", "playwright", "install", *(["--with-deps"] if with_deps else []), "chromium"]
    done = subprocess.run(command, check=False)
    if done.returncode:
        raise ExportError("playwright install chromium failed; see its output above")
    return {"installed": "chromium"}


def browser_login(profile: Path, *, channel: str | None, timeout_s: int = 600) -> dict[str, Any]:
    """Open the profile's browser on Google Photos and wait until you have signed in."""
    with _playwright() as playwright:
        context = _launch(playwright, profile, channel=channel)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
            print("Sign in to Google in the browser window (this tool never sees your password).", file=sys.stderr)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if (urlsplit(page.url).hostname or "") == APP_HOST and (email := page_email(page.content())):
                    return {"profile": str(profile), "account": email}
                page.wait_for_timeout(1000)
            raise ExportError("not signed in before the timeout; run login again")
        except ExportError:
            raise
        except Exception as exc:  # the window was closed, a navigation failed, ...
            raise ExportError(f"the browser stopped before the sign-in finished ({type(exc).__name__})") from None
        finally:
            try:
                context.close()
            except Exception:
                pass


def browser_export(
    profile: Path,
    out_dir: Path,
    *,
    expected_email: str | None,
    channel: str | None,
) -> dict[str, Any]:
    """Load the library in the signed-in profile, open one photo, write a schema-2 snapshot."""
    with _playwright() as playwright:
        context = _launch(playwright, profile, channel=channel)
        try:
            return export_from_context(context, out_dir, expected_email=expected_email)
        finally:
            try:
                context.close()
            except Exception:
                pass


def export_from_context(
    context: Any,
    out_dir: Path,
    *,
    expected_email: str | None,
    min_generation: int = 0,
) -> dict[str, Any]:
    """In an open browser context (Playwright's API: the CLI's own, or the login service's window): load the
    library, open one photo, and write a schema-2 snapshot with a generation above both the published one and
    ``min_generation``. Raises ExportError with its ``code``."""
    wire = WireHeaders()
    images: list[int] = []
    try:
        page = context.pages[0] if context.pages else context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.on("Network.requestWillBeSent", wire.on_sent)
        cdp.on("Network.requestWillBeSentExtraInfo", wire.on_wire)
        cdp.send("Network.enable")
        # Cache hits carry no wire headers: fetch for real during the export.
        cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})

        def on_response(response: Any) -> None:
            host = urlsplit(response.url).hostname or ""
            if host == IMAGE_HOST and response.status == 200:
                images.append(response.status)

        page.on("response", on_response)
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
        out = signed_out_page(settled_url(page))
        if out == "signed_out":
            raise ExportError("this profile is signed out: run 'photos-mcp-session browser login' first", out)
        if out == "challenge":
            raise ExportError("Google wants the person to sign in again in this profile ('browser login')", out)
        if out == "interstitial":
            raise ExportError("Google shows an account notice in this profile: open it once and answer it", out)
        email = None
        for _ in range(20):
            if email := page_email(page.content()):
                break
            page.wait_for_timeout(1000)
        if not email:
            raise ExportError("the library page never showed a signed-in account", "no_account")
        if expected_email and email.strip().lower() != expected_email.strip().lower():
            raise ExportError(
                "the profile is signed in to a different Google account than --expected-email", "account_mismatch"
            )
        before = len(images)
        try:
            page.locator(TILE_SELECTOR).first.click(timeout=20_000)
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError":  # Playwright's (and patchright's) own TimeoutError
                raise
            raise ExportError("the library shows no photo to open; is it empty?", "empty_library") from None
        for _ in range(20):
            if len(images) > before:
                break
            page.wait_for_timeout(1000)
        else:
            raise ExportError("opening a photo loaded no image; is the library empty?", "empty_library")
        page.wait_for_timeout(2000)  # let the image host's cookies settle
        user_agent = page.evaluate("() => navigator.userAgent")
        headers, synthesised = wire.templates(user_agent)
        cookies = select_cookies(browser_cookies(context.cookies()))
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"the browser export failed ({type(exc).__name__}); try again") from None
    meta = build_meta(
        generation=max(published_generation(out_dir), min_generation) + 1,
        email=email,
        user_agent=user_agent,
        headers=headers,
        verified_at=datetime.now(UTC),
    )
    publish(out_dir, cookies, meta)
    return {
        "out": str(out_dir),
        "schema": 2,
        "generation": meta["generation"],
        "synthesised_headers": synthesised,
        "email": meta["email"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="photos-mcp-session", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import", help="write a snapshot from a cookies.txt exported from your browser")
    imp.add_argument("--cookies", type=Path, required=True, help="Netscape cookies.txt")
    imp.add_argument("--email", required=True, help="the Google account's e-mail address")
    imp.add_argument("--user-agent", required=True, help="the exporting browser's User-Agent (navigator.userAgent)")
    imp.add_argument("--out", type=Path, required=True, help="snapshot directory (PHOTOS_SESSION_DIR)")
    browser = commands.add_parser("browser", help="sign in once in a dedicated profile, then export from it")
    actions = browser.add_subparsers(dest="action", required=True)
    install = actions.add_parser("install", help="download the Chromium build Playwright drives (once)")
    install.add_argument("--with-deps", action="store_true", help="also install its Linux system libraries (root)")
    for name in ("login", "export"):
        action = actions.add_parser(
            name,
            help="sign in by hand in the profile's browser window"
            if name == "login"
            else "write a snapshot from the signed-in profile (needs a display; xvfb-run on a server)",
        )
        action.add_argument("--profile", type=Path, required=True, help="browser profile directory (keep it private)")
        action.add_argument("--channel", help="a Playwright browser channel, e.g. chrome (an installed Google Chrome)")
        if name == "export":
            action.add_argument("--out", type=Path, required=True, help="snapshot directory (PHOTOS_SESSION_DIR)")
            action.add_argument("--expected-email", help="refuse to export another account")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            result = import_cookies(args.cookies, email=args.email, user_agent=args.user_agent, out_dir=args.out)
        elif args.action == "install":
            result = browser_install(with_deps=args.with_deps)
        elif args.action == "login":
            result = browser_login(args.profile, channel=args.channel)
        else:
            result = browser_export(
                args.profile,
                args.out,
                expected_email=args.expected_email,
                channel=args.channel,
            )
    except ExportError as exc:
        print("photos-mcp-session:", *exc.args, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
