"""Thumbnail disk cache: ``<cache>/<id>/<px>.jpg`` (+ ``<px>.json`` metadata), LRU by mtime.

Writes use unique temp names and rename; a hit refreshes the mtime. When the total size
passes the cap, the least recently used files go until it is back under 90 %.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KEY_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")


class ThumbCache:
    def __init__(self, directory: Path, max_bytes: int) -> None:
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._total: int | None = None

    def _paths(self, key: str, px: int) -> tuple[Path, Path]:
        if not KEY_RE.fullmatch(key):
            raise ValueError("bad cache key")
        base = self.directory / key
        return base / f"{int(px)}.jpg", base / f"{int(px)}.json"

    def get(self, key: str, px: int) -> tuple[bytes, dict[str, Any]] | None:
        if self.max_bytes <= 0:
            return None
        jpeg_path, meta_path = self._paths(key, px)
        try:
            jpeg = jpeg_path.read_bytes()
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            return None
        if not jpeg or not isinstance(meta, dict):
            return None
        try:
            os.utime(jpeg_path)
        except OSError:
            pass
        return jpeg, meta

    def put(self, key: str, px: int, jpeg: bytes, meta: dict[str, Any]) -> None:
        if self.max_bytes <= 0:
            return
        jpeg_path, meta_path = self._paths(key, px)
        try:
            jpeg_path.parent.mkdir(parents=True, exist_ok=True)
            meta_bytes = json.dumps(meta, separators=(",", ":")).encode()
            _atomic_write(meta_path, meta_bytes)
            _atomic_write(jpeg_path, jpeg)
        except OSError as exc:
            log.warning("thumbnail cache write failed (%s)", type(exc).__name__)
            return
        with self._lock:
            if self._total is None:
                self._total = self._scan_total()
            else:
                self._total += len(jpeg) + len(meta_bytes)
            if self._total > self.max_bytes:
                self._evict()

    def size(self) -> int:
        with self._lock:
            if self._total is None:
                self._total = self._scan_total()
            return self._total

    def _files(self) -> list[tuple[float, int, Path]]:
        out = []
        if not self.directory.is_dir():
            return out
        for entry in self.directory.iterdir():
            if not entry.is_dir():
                continue
            for file in entry.iterdir():
                try:
                    st = file.stat()
                except OSError:
                    continue
                out.append((st.st_mtime, st.st_size, file))
        return out

    def _scan_total(self) -> int:
        return sum(size for _, size, _ in self._files())

    def _evict(self) -> None:
        target = int(self.max_bytes * 0.9)
        files = sorted(f for f in self._files() if f[2].suffix == ".jpg")
        total = self._scan_total()
        for _, _, jpg in files:
            if total <= target:
                break
            for path in (jpg, jpg.with_suffix(".json")):
                try:
                    total -= path.stat().st_size
                    path.unlink()
                except OSError:
                    pass
            try:
                jpg.parent.rmdir()  # only succeeds when empty
            except OSError:
                pass
        self._total = total


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
