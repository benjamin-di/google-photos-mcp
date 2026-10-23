"""Adding a photo or video to a library: what photos.google.com's own uploader sends (docs/PAYLOADS.md "Uploads").

Recorded from the web app's "Import photos from your computer" (2026-10): no third-party client documents it.

1. ``swbisb`` (``payloads.GetRemoteMatchesByHash``): is a file with this SHA-1 already in the library? The web app
   skips the upload when it is, and so does photos-mcp.
2. ``POST /_/upload/uploadmedia/interactive`` (Google's resumable upload protocol, ``X-Goog-Upload-Command: start``,
   with a small protobuf body naming the size), then the bytes to the ``X-Goog-Upload-URL`` it answers with, in
   chunks, the last one ``upload, finalize``. The final answer is an upload token. Nothing is in the library yet.
3. ``mdpdU`` (``CommitUpload``): make the media item from the token. This is the change; it goes through
   ``GoogleTransport.write`` like the album changes.

This module holds the pure parts (sniffing, names, the start body, the ``mdpdU`` builder and parser); the requests are
in ``transport.GoogleTransport.upload_bytes`` and the flow in ``client.PhotosService.upload``.
"""

from __future__ import annotations

import base64
import hashlib
import re
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from . import rpc
from .errors import PhotosError, invalid
from .vendor.gpwc.payloads import Payload

UPLOAD_PATH = "/_/upload/uploadmedia/interactive"
# Chunks of the resumable upload: a multiple of Google's chunk granularity (256 KiB, X-Goog-Upload-Chunk-Granularity).
GRANULARITY = 256 * 1024
CHUNK = 64 * GRANULARITY  # 16 MiB
FILENAME_MAX = 200
SNIFF_BYTES = 512

# ISO base media files (HEIF, AVIF, MP4, MOV, 3GP, CR3): the major brands that are still images; any other is a video.
_IMAGE_BRANDS = {
    b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis", b"crx ",
}  # fmt: skip
_IMAGE_EXT = {b"heic": "heic", b"heix": "heic", b"avif": "avif", b"avis": "avif", b"crx ": "cr3"}
_VIDEO_EXT = {b"qt  ": "mov", b"3gp4": "3gp", b"3gp5": "3gp", b"3gp6": "3gp", b"3g2a": "3g2", b"M4V ": "m4v"}


@dataclass(frozen=True)
class Media:
    type: str  # "photo" or "video"
    ext: str  # the usual file extension of the format, without the dot


def sniff(head: bytes) -> Media | None:
    """The file's format from its first bytes (``SNIFF_BYTES``): only formats Google Photos keeps, else None."""
    if head[:3] == b"\xff\xd8\xff":
        return Media("photo", "jpg")
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return Media("photo", "png")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return Media("photo", "gif")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Media("photo", "webp")
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return Media("video", "avi")
    if head[:2] == b"BM" and len(head) >= 26:
        return Media("photo", "bmp")
    if head[:4] in (b"II*\x00", b"MM\x00*", b"IIRO", b"IIRS", b"IIU\x00"):
        return Media("photo", "tif")  # TIFF, and the raw formats built on it (DNG, CR2, NEF, ARW, ORF, RW2)
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _IMAGE_BRANDS:
            return Media("photo", _IMAGE_EXT.get(brand, "heic"))
        return Media("video", _VIDEO_EXT.get(brand, "mp4"))
    if head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip"):
        return Media("video", "mov")
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return Media("video", "webm" if b"webm" in head[:64] else "mkv")
    if head[:16] == b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c":
        return Media("video", "wmv")
    if head[:4] == b"\x00\x00\x01\xba":
        return Media("video", "mpg")
    if len(head) > 376 and head[0] == 0x47 and head[188] == 0x47:
        return Media("video", "ts")
    if len(head) > 384 and head[4] == 0x47 and head[196] == 0x47:
        return Media("video", "m2ts")
    return None


NOT_MEDIA = (
    "this is not a photo or video Google Photos takes (JPEG, PNG, GIF, WebP, HEIC, AVIF, BMP, TIFF and raw; MP4, MOV, "
    "3GP, AVI, MKV, WebM, WMV, MPEG)"
)


def size_text(size: int) -> str:
    return f"{size / 1024**3:g} GiB" if size >= 1024**3 else f"{size / 1024**2:g} MiB"


@dataclass(frozen=True)
class Staged:
    """A received file, ready to send: its bytes (from the start), size, SHA-1, format and library name."""

    file: IO[bytes]
    size: int
    sha1: str  # base64, as Google writes it
    media: Media
    name: str
    modified_ms: int


class Staging:
    """A file as it arrives (an upload link's PUT, a fetched link, inline data): written to an anonymous file in
    ``directory`` (gone when closed, or if the process dies), hashed, held to ``max_bytes``, and refused as soon as its
    first bytes show it is not a photo or video."""

    def __init__(self, directory: Path, max_bytes: int) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Closed by close() (the owner's with block): its life is the Staging's, not one block's.
        self._file = tempfile.TemporaryFile(dir=directory)  # noqa: SIM115
        self._sha1 = hashlib.sha1(usedforsecurity=False)
        self._head = b""
        self.max_bytes = max_bytes
        self.size = 0

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > self.max_bytes:
            raise invalid(f"the file is larger than {size_text(self.max_bytes)}")
        if len(self._head) < SNIFF_BYTES:
            self._head += chunk[: SNIFF_BYTES - len(self._head)]
            if len(self._head) >= SNIFF_BYTES and sniff(self._head) is None:
                raise invalid(NOT_MEDIA)
        self._sha1.update(chunk)
        self._file.write(chunk)

    def finish(self, raw_name: str | None, modified_ms: int | None, now: float) -> Staged:
        if not self.size:
            raise invalid("the file is empty")
        media = sniff(self._head)
        if media is None:
            raise invalid(NOT_MEDIA)
        self._file.flush()
        self._file.seek(0)
        return Staged(
            self._file,
            self.size,
            sha1_b64(self._sha1.digest()),
            media,
            filename(raw_name, media, now),
            modified_ms if modified_ms is not None else int(now * 1000),
        )

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Staging:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class Gate:
    """Files being received or sent at once on a server, by links and tools alike: as many as the transport's upload
    slots, so a file that has arrived never waits for one, and at most that many files are held on disk."""

    def __init__(self, slots: int) -> None:
        self._slots = threading.BoundedSemaphore(slots)

    def acquire(self, timeout: float) -> bool:
        return self._slots.acquire(timeout=timeout)

    def try_acquire(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release(self) -> None:
        self._slots.release()


def modified_ms(raw: Any, now: float) -> int | None:
    """A last-modified time a client sent (ms since 1970), if it is a plausible one: after 1990, not in the future."""
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        raw = int(raw)
    if isinstance(raw, int) and not isinstance(raw, bool) and 631_152_000_000 <= raw <= (now + 86_400) * 1000:
        return raw
    return None


def filename(raw: str | None, media: Media, now: float) -> str:
    """The name the item gets in the library: the given one reduced to a safe base name, with the format's extension
    when it has none; ``photo-<date>-<time>.<ext>`` (or video-...) when none is given."""
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%d-%H%M%S")
        return f"{media.type}-{stamp}.{media.ext}"
    stem, dot, ext = name.rpartition(".")
    if not dot or not re.fullmatch(r"[A-Za-z0-9]{1,5}", ext):
        stem, ext = name, media.ext
    stem = stem[: FILENAME_MAX - len(ext) - 1].rstrip(" .") or media.type
    return f"{stem}.{ext}"


def header_filename(name: str) -> str:
    """The name for the ``X-Goog-Upload-File-Name`` header: HTTP header values are Latin-1, so other characters become
    "_" there (``mdpdU`` carries the real name)."""
    return "".join(ch if 0x20 <= ord(ch) < 0x7F else "_" for ch in name)


def sha1_b64(digest: bytes) -> str:
    """A SHA-1 as Google writes it in ``X-Goog-Hash`` and ``swbisb``: standard base64 with padding."""
    return base64.b64encode(digest).decode("ascii")


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def start_body(size: int) -> bytes:
    """The protobuf the web app sends with ``X-Goog-Upload-Command: start``: fields 1-4 and 8 as recorded (their meaning
    is not known), field 7 the file's size in bytes."""
    return b"\x08\x02\x10\x01\x18\x01\x20\x01\x38" + _varint(size) + b"\x40\x01"


class CommitUpload(Payload):
    """``mdpdU``: make a media item from an upload token. ``modified_ms`` is the file's last-modified time (the web app
    sends ``File.lastModified``); Google uses the file's own date (EXIF) when it has one. The two trailing 2s are the
    web app's, meaning unknown."""

    def __init__(self, upload_token: str, name: str, modified_ms: int):
        super().__init__()
        self.rpcid = "mdpdU"
        self.data = [[[upload_token, name, modified_ms, 2, 2]]]


@dataclass(frozen=True)
class Stored:
    """A media item as ``swbisb`` and ``mdpdU`` describe it."""

    media_key: str
    width: int | None
    height: int | None
    taken_ts: int | None
    tz_offset: int | None
    uploaded_ts: int | None


def parse_stored(entry: Any) -> Stored | None:
    """One entry of a ``swbisb`` or ``mdpdU`` answer: ``[hash or token, [media_key, [thumb, w, h, ...], taken_ts,
    dedup_key, tz_offset_ms, uploaded_ts, ...], ...]`` (gpwc ``parser.RemoteMatch``; the same shape in ``mdpdU``)."""
    record = rpc.field(entry, 1)
    key = rpc.field(record, 0)
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
        return None

    def number(*steps: int) -> int | None:
        value = rpc.path(record, *steps)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return Stored(key, number(1, 1), number(1, 2), number(2), number(4), number(5))


def parse_matches(data: Any) -> list[Stored]:
    """``swbisb``'s answer: ``[]`` when no item has the hash, else ``[[entry, ...]]``."""
    entries = rpc.field(data, 0)
    if not isinstance(entries, list):
        return []
    return [stored for stored in (parse_stored(entry) for entry in entries) if stored is not None]


def parse_created(data: Any) -> Stored:
    """``mdpdU``'s answer, ``[[[token, record, 0]]]``: the new item, or schema_changed (it may exist all the same)."""
    found = parse_matches(data)
    if not found:
        raise PhotosError("schema_changed", log_detail="mdpdU returned no media key")
    return found[0]
