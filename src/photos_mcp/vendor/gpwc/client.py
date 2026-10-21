"""batchexecute framing and parsing (gpwc ``client.py`` at 651aa268; see README.md).

No network I/O: ``photos_mcp.transport`` sends what ``Client.build_request`` returns over
curl_cffi and hands the body back to ``parse_api_response``.
"""

import json
import urllib.parse
from collections.abc import Sequence
from html.parser import HTMLParser
from typing import Any

from .models import ApiResponse
from .payloads import Payload


class Client:
    """Frames read-only RPC payloads for ``/data/batchexecute`` with the page's global data."""

    def __init__(self, global_data: dict[str, Any], account_index: int = 0) -> None:
        self.global_data = global_data
        self.account_index = account_index
        self.base_path = f"/u/{account_index}/" if account_index else "/"

    def prepare_payload(self, payload: Payload) -> list[Any]:
        """Prepare payload for api request"""
        return [payload.rpcid, json.dumps(payload.data, separators=(",", ":")), None, payload.payload_id]

    def build_request(self, payloads: Sequence[Payload]) -> tuple[str, dict[str, str], str]:
        """Return (url, query, urlencoded body) for one batchexecute POST."""
        if len(payloads) == 1:
            payloads[0].payload_id = "generic"
        else:
            for index, payload in enumerate(payloads, start=1):
                payload.payload_id = str(index)
        querystring = {
            "rpcids": ",".join(payload.rpcid for payload in payloads),
            "source-path": self.base_path,
            "f.sid": self.global_data["FdrFJe"],
            "bl": self.global_data["cfb2h"],
            "rt": "c",
        }
        body = {
            "f.req": json.dumps([[self.prepare_payload(payload) for payload in payloads]], separators=(",", ":")),
            "at": self.global_data["SNlM0e"],
        }
        body_encoded = "&".join(f"{key}={urllib.parse.quote(value, safe='')}" for key, value in body.items())
        url = f"https://photos.google.com{self.global_data.get('Im6cmf') or '/_/PhotosUi'}/data/batchexecute"
        return url, querystring, body_encoded


def parse_envelopes(response_body: str) -> list[list[Any]]:
    """Every ``wrb.fr`` and ``er`` entry of every chunk.

    Upstream read only ``response[0]`` of each line, which drops the second envelope when
    Google packs two into one chunk.
    """
    entries: list[list[Any]] = []
    for line in response_body.split("\n"):
        line = line.strip()
        if not line.startswith("[") or ('"wrb.fr"' not in line and '"er"' not in line):
            continue
        try:
            chunk = json.loads(line)
        except ValueError:
            continue
        if not isinstance(chunk, list):
            continue
        for entry in chunk:
            if isinstance(entry, list) and entry and entry[0] in ("wrb.fr", "er"):
                entries.append(entry)
    return entries


def parse_api_response(response_body: str, payloads: Sequence[Payload]) -> list[ApiResponse]:
    """Parse api response: one ApiResponse per ``wrb.fr`` entry, data still raw (decoded JSON)."""
    by_id = {payload.payload_id: payload for payload in payloads}
    parsed_responses = []
    for entry in parse_envelopes(response_body):
        if entry[0] != "wrb.fr":
            continue
        rpcid = entry[1] if len(entry) > 1 and isinstance(entry[1], str) else ""
        raw = entry[2] if len(entry) > 2 else None
        response_id = entry[6] if len(entry) > 6 and isinstance(entry[6], str) else ""
        if response_id not in by_id:
            # Match by RPC id when the echo is missing and the id is unambiguous.
            same = [p for p in payloads if p.rpcid == rpcid]
            response_id = same[0].payload_id if len(same) == 1 else response_id
        success = False
        data = None
        if isinstance(raw, str) and raw:
            data = json.loads(raw)
            success = True
        parsed_responses.append(ApiResponse(rpcid=rpcid, success=success, response_id=response_id, data=data))
    return parsed_responses


class _GdScript(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside = False
        self.text: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and ("data-id", "_gd") in attrs and self.text is None:
            self._inside = True
            self.text = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside and self.text is not None:
            self.text += data


def _first_object(text: str, start: int) -> dict[str, Any] | None:
    brace = text.find("{", start)
    if brace < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, brace)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_main_page(page_body: str) -> dict[str, Any] | None:
    """``window.WIZ_global_data`` from ``<script data-id="_gd">``; ``None`` when absent.

    Falls back to the first ``WIZ_global_data = {...}`` assignment anywhere in the page.
    """
    finder = _GdScript()
    try:
        finder.feed(page_body)
        finder.close()
    except Exception:  # malformed markup: fall through to the regex-free fallback
        finder.text = None
    if finder.text:
        found = _first_object(finder.text, max(0, finder.text.find("WIZ_global_data")))
        if found is not None:
            return found
    marker = page_body.find("WIZ_global_data")
    while marker >= 0:
        equals = page_body.find("=", marker)
        if equals >= 0 and page_body[marker + len("WIZ_global_data") : equals].strip() == "":
            found = _first_object(page_body, equals)
            if found is not None:
                return found
        marker = page_body.find("WIZ_global_data", marker + 1)
    return None
