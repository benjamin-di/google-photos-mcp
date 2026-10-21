"""Checked-in fixtures carry nothing about a real account.

* ``recorded/``: real answers, anonymised by ``photos_mcp.capture``: every string is a
  placeholder and every number is small (a flag or enum) or one the anonymiser hands out.
* the parser fixtures (``*.json``): derived from Google-Photos-Toolkit's anonymised public
  fixtures (``README.md``); pinned by hash so a change is a deliberate, reviewed one.
* ``session/``: a fake snapshot (example.com account, ``fake-`` cookie values).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from conftest import FIXTURES

from photos_mcp.capture import FIXED_E7, KEEP_INT, SYNTH_EPOCH_MS, SYNTH_INT_BASE, SYNTH_OFFSET_MS, SYNTH_STEP_MS

RECORDED = sorted((FIXTURES / "recorded").glob("*.json"))
PLACEHOLDER = re.compile(
    r"ANON_\d+|AF1QipREC\d{6}|file_\d+\.[a-z0-9]{2,5}|user\d+@example\.com|80*[1-9]\d{0,3}|"
    r"https://(photos\.google\.com|photos\.fife\.usercontent\.google\.com|lh\d\.googleusercontent\.com|"
    r"example\.invalid)/ANON_\d+|"
)
# What the anonymiser hands out: minutes of January 2020, the offset, the fixed point and
# SYNTH_INT_BASE + n (a range no real number survives into: it is zeroed as coordinate-like).
TIMESTAMPS = range(SYNTH_EPOCH_MS + SYNTH_STEP_MS, SYNTH_EPOCH_MS + 10_000 * SYNTH_STEP_MS, SYNTH_STEP_MS)
SYNTHETIC_INTS = range(SYNTH_INT_BASE + 1, SYNTH_INT_BASE + 10_000)
# The reviewed GPTK derivatives, in `sha256sum` format.
PARSER_FIXTURES = FIXTURES / "parser-fixtures.sha256"


def _leaves(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def _handed_out(n: int | float) -> bool:
    if isinstance(n, float):
        return n.is_integer() and abs(n) <= KEEP_INT or n == 0.0
    return abs(n) <= KEEP_INT or n in TIMESTAMPS or n in (SYNTH_OFFSET_MS, *FIXED_E7) or abs(n) in SYNTHETIC_INTS


@pytest.mark.parametrize("path", RECORDED, ids=[p.name for p in RECORDED])
def test_recordings_hold_only_placeholders_and_synthetic_numbers(path: Path):
    record = json.loads(path.read_text())
    assert set(record) <= {"format", "kind", "rpcid", "label", "data", "captured_at", "request", "cookies",
                           "status", "content_type", "location", "has_gd", "expect"}  # fmt: skip
    for leaf in _leaves(record.get("data")):
        if isinstance(leaf, str):
            assert PLACEHOLDER.fullmatch(leaf), leaf
        elif isinstance(leaf, int | float) and not isinstance(leaf, bool):
            assert _handed_out(leaf), leaf
    if record["kind"] == "http":
        assert record["location"] in (None, "https://www.google.com/photos/about/", "https://consent.google.com/ml")


def test_the_recordings_were_checked():
    assert len(RECORDED) >= 10


def test_parser_fixtures_are_the_reviewed_gptk_derivatives():
    found = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURES.glob("*.json")}
    found.pop("chrome_wire_order.json")  # header names only
    pinned = dict(reversed(line.split("  ", 1)) for line in PARSER_FIXTURES.read_text().splitlines())
    assert found == pinned


def test_session_sample_is_fake():
    for meta in ("meta.json", "meta.schema1.json"):
        assert json.loads((FIXTURES / "session" / meta).read_text())["email"].endswith("@example.com")
    rows = [line.split("\t") for line in (FIXTURES / "session" / "cookies.txt").read_text().splitlines()]
    values = [row[6] for row in rows if len(row) == 7]
    assert values and all(value.startswith("fake-") for value in values)
