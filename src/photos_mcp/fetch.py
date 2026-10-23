"""A photo or video a caller names by link (``photos_upload(url=...)``): the one request photos-mcp makes to a host it
does not know. It must never become a way into the network the server runs in (README "Safety and Google posture"):

* https on port 443 only, and no user name or password in the link;
* the host is resolved here and every address it resolves to must be public (``ipaddress.is_global``); the request
  then goes to that address only (curl's resolve pin), so no second DNS answer can point it elsewhere;
* redirects are followed by hand, at most ``MAX_REDIRECTS``, each one checked the same way;
* no cookies, no credentials, no proxy (not even one from the environment): a browser's ordinary request;
* the body streams into the upload's ``Staging`` (its size cap, refused as soon as it is not a photo or video), and
  the whole fetch, headers included, must finish within ``FETCH_DEADLINE`` (curl's total timeout on each hop).

Logs carry the host and the status only, never the link (its query may hold a token).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from curl_cffi import CurlOpt
from curl_cffi import requests as curl_requests

from .errors import PhotosError, invalid

log = logging.getLogger(__name__)

MAX_REDIRECTS = 3
CONNECT_TIMEOUT, READ_TIMEOUT = 10.0, 30.0
FETCH_DEADLINE = 600.0
ACCEPT = "image/avif,image/webp,image/apng,image/*,video/*;q=0.9,*/*;q=0.5"
URL_MAX = 4096

Resolver = Callable[[str], list[str]]


def resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to for HTTPS."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise PhotosError("fetch_failed", log_detail="host not found") from None
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


# IPv6 prefixes that carry an IPv4 address in their last 32 bits (NAT64, IPv4-compatible): the IPv4 one decides.
_EMBEDDED_V4 = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("::/96"),
)


def is_public(address: str) -> bool:
    """A public unicast address (``is_global``), also for the IPv4 address inside a NAT64, IPv4-compatible, mapped,
    6to4 or Teredo IPv6 one, which ``is_global`` alone takes as public."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.is_multicast or not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = [ip.ipv4_mapped, ip.sixtofour, ip.teredo[1] if ip.teredo else None]
        inner += [ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) for net in _EMBEDDED_V4 if ip in net]
        return all(is_public(str(v4)) for v4 in inner if v4 is not None)
    return True


def public_address(host: str, resolver: Resolver = resolve) -> str:
    """The address to connect to: only when every address of ``host`` is public (an IPv4 one first)."""
    addresses = resolver(host)
    try:
        public = bool(addresses) and all(is_public(a) for a in addresses)
    except ValueError:
        public = False
    if not public:
        raise invalid("the link must point to a public web address")
    return next((a for a in addresses if ":" not in a), addresses[0])


def check_link(url: str) -> tuple[str, str]:
    """``(host, link)`` for a link ``fetch`` may follow at all: the host in lower case and IDNA, and the link rebuilt
    on exactly that host (so curl connects to the host that was checked and pinned, never its own reading of it)."""
    if not isinstance(url, str) or len(url) > URL_MAX:
        raise invalid("the link must be an https address")
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        port = -1
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password or not host:
        raise invalid("the link must be an https address (port 443, no user name)")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise invalid("the link's host name is not valid") from None
    if not re.fullmatch(r"[a-z0-9.-]{1,253}|[0-9a-f:.]{2,45}", host):
        raise invalid("the link's host name is not valid")
    netloc = f"[{host}]" if ":" in host else host
    return host, urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


def fetch(
    url: str,
    sink: Callable[[bytes], None],
    *,
    resolver: Resolver = resolve,
    session_factory: Callable[..., curl_requests.Session] = curl_requests.Session,
    monotonic: Callable[[], float] = time.monotonic,
) -> str | None:
    """Stream the file at ``url`` into ``sink``; return the file name the answer or the link gives, if any."""
    deadline = monotonic() + FETCH_DEADLINE
    current = url.strip()
    for _ in range(MAX_REDIRECTS + 1):
        host, current = check_link(current)
        address = public_address(host, resolver)
        left = deadline - monotonic()
        if left <= 0:
            raise PhotosError("fetch_failed", log_detail=f"{host}: over the deadline")
        pin = f"{host}:443:{f'[{address}]' if ':' in address else address}"
        session = session_factory(
            impersonate="chrome",
            curl_options={
                CurlOpt.RESOLVE: [pin],
                CurlOpt.PROTOCOLS_STR: "https",
                CurlOpt.REDIR_PROTOCOLS_STR: "https",
                # libcurl reads the proxy environment variables itself (trust_env covers only curl_cffi's own
                # lookup); an empty proxy turns that off, so nothing between here and the pinned address re-resolves.
                CurlOpt.PROXY: "",
                # The whole hop, headers included (the read timeout below only limits a stall).
                CurlOpt.TIMEOUT_MS: max(1, int(left * 1000)),
            },
            trust_env=False,
            allow_redirects=False,
            discard_cookies=True,
        )
        try:
            try:
                response = session.get(
                    current,
                    headers={"Accept": ACCEPT},
                    stream=True,
                    allow_redirects=False,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
            except curl_requests.RequestsError as exc:
                raise PhotosError("fetch_failed", log_detail=f"{host}: {type(exc).__name__}") from None
            try:
                status = response.status_code
                location = response.headers.get("location")
                if 300 <= status < 400 and location:
                    log.info("fetch: %s redirected (%s)", host, status)
                    current = urljoin(current, location)
                    continue
                if status != 200:
                    raise PhotosError("fetch_failed", log_detail=f"{host}: HTTP {status}")
                try:
                    for chunk in response.iter_content():
                        if monotonic() > deadline:
                            raise PhotosError("fetch_failed", log_detail=f"{host}: over the deadline")
                        sink(chunk)
                except curl_requests.RequestsError as exc:
                    raise PhotosError("fetch_failed", log_detail=f"{host}: {type(exc).__name__}") from None
                log.info("fetch: %s answered 200", host)
                return _name(response.headers.get("content-disposition"), current)
            finally:
                response.close()
        finally:
            session.close()
    raise PhotosError("fetch_failed", log_detail="too many redirects")


def _name(disposition: str | None, url: str) -> str | None:
    """``filename=`` of a Content-Disposition, else the link path's last segment."""
    if disposition:
        for part in disposition.split(";"):
            key, _, value = part.strip().partition("=")
            if key.lower() == "filename*" and "''" in value:
                return unquote(value.split("''", 1)[1].strip('"'))
            if key.lower() == "filename" and value.strip('"'):
                return value.strip('"')
    segment = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return segment or None
