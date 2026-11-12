#!/usr/bin/env python3
"""Validate publisher inputs and embed offline identity before application installation."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path


def embed(root: Path, version: str, revision: str) -> None:
    installed_version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    normalized = version.strip().removeprefix("v")
    if not normalized or normalized != installed_version:
        raise ValueError("VERSION must match the application version in pyproject.toml")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", revision):
        raise ValueError("VCS_REF must be a full 40- or 64-character hexadecimal source revision")
    target = root / "src/photos_mcp/build_identity.json"
    target.write_text(
        json.dumps({"service": "photos-mcp", "version": normalized, "source_revision": revision.lower()}) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    embed(Path.cwd(), *sys.argv[1:])
