"""The installed application artifact's identity, read locally without runtime overrides."""

import json
import os
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from uuid import uuid4

from . import __version__


@dataclass(frozen=True)
class BuildIdentity:
    service: str
    version: str
    source_revision: str


def _source_development() -> bool:
    try:
        installed = distribution("google-photos-mcp")
    except PackageNotFoundError:
        return True
    try:
        direct = json.loads(installed.read_text("direct_url.json") or "{}")
    except (TypeError, ValueError):
        return False
    return (
        isinstance(direct, dict)
        and isinstance(direct.get("dir_info"), dict)
        and direct["dir_info"].get("editable") is True
    )


def load(path: Path | None = None) -> BuildIdentity:
    artifact = path if path is not None else Path(__file__).with_name("build_identity.json")
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not _source_development():
            raise ValueError("Missing embedded application build identity in installed package") from None
        return BuildIdentity("photos-mcp", __version__, "unknown")
    if (
        not isinstance(data, dict)
        or set(data) != {"service", "version", "source_revision"}
        or any(not isinstance(value, str) for value in data.values())
        or data["service"] != "photos-mcp"
        or data["version"] != __version__
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", data["source_revision"])
    ):
        raise ValueError("Invalid embedded application build identity")
    return BuildIdentity(**data)


BUILD = load()


_INSTANCE = uuid4().hex


def _after_fork() -> None:
    global _INSTANCE
    _INSTANCE = uuid4().hex


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def log_context() -> dict[str, str]:
    return {
        "service": BUILD.service,
        "version": BUILD.version,
        "source_revision": BUILD.source_revision,
        "instance": _INSTANCE,
    }
