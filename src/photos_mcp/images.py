"""Turn what the image CDN served into bounded JPEGs, and lay out contact sheets.

Untrusted bytes: magic bytes are checked before Pillow sees them, a decompression-bomb
*warning* is an error, and JPEGs are ``draft()``-reduced before ``load()``.
"""

from __future__ import annotations

import logging
import math
import threading
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .errors import PhotosError

log = logging.getLogger(__name__)

# Above this Pillow warns (and we refuse); at twice this it raises DecompressionBombError.
Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_PX = 1568  # Claude downsizes anything with a longer edge
JPEG_QUALITY = 82
_DECODE_LOCK = threading.Lock()  # warnings.catch_warnings is process-global


def sniff(data: bytes) -> str | None:
    """Image format from magic bytes, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "heic"
    return None


def to_jpeg(data: bytes, max_px: int, quality: int = JPEG_QUALITY) -> tuple[bytes, int, int]:
    """(jpeg, width, height) with the longest side ≤ ``max_px``; ``item_unavailable`` otherwise."""
    kind = sniff(data)
    if kind is None:
        raise PhotosError("item_unavailable", log_detail="not an image (magic bytes)")
    with _DECODE_LOCK, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(BytesIO(data)) as source:
                if source.format == "JPEG":
                    source.draft("RGB", (max_px, max_px))
                source.load()
                image = ImageOps.exif_transpose(source) or source
                image = _flatten(image)
                image.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
                out = BytesIO()
                image.save(out, format="JPEG", quality=quality, optimize=True)
                return out.getvalue(), image.width, image.height
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise PhotosError("item_unavailable", log_detail="decompression bomb refused") from None
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
            raise PhotosError("item_unavailable", log_detail=f"undecodable {kind}") from None


def _flatten(image: Image.Image) -> Image.Image:
    """JPEG has no alpha: composite transparent images onto white."""
    if image.mode in ("RGB", "L"):
        return image
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


@dataclass(frozen=True)
class Tile:
    number: int
    jpeg: bytes | None  # None draws a placeholder
    is_video: bool = False


def grid_shape(count: int) -> tuple[int, int]:
    cols = max(1, math.ceil(math.sqrt(count)))
    return cols, max(1, math.ceil(count / cols))


TILE_MAX = 256
GAP = 6


def tile_size(count: int) -> int:
    cols, _ = grid_shape(count)
    return min(TILE_MAX, (MAX_PX - GAP * (cols + 1)) // cols)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # Pillow without FreeType: fixed bitmap font
        return ImageFont.load_default()


def contact_sheet(tiles: Sequence[Tile]) -> tuple[bytes, int, int]:
    """One JPEG grid (≤ 1568 px each way): numbered tiles, a play badge on videos."""
    if not tiles:
        raise ValueError("no tiles")
    cols, rows = grid_shape(len(tiles))
    size = tile_size(len(tiles))
    width = cols * size + GAP * (cols + 1)
    height = rows * size + GAP * (rows + 1)
    sheet = Image.new("RGB", (width, height), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = _font(max(14, size // 8))
    for index, tile in enumerate(tiles):
        col, row = index % cols, index // cols
        x0, y0 = GAP + col * (size + GAP), GAP + row * (size + GAP)
        draw.rectangle((x0, y0, x0 + size - 1, y0 + size - 1), fill=(48, 48, 48))
        if tile.jpeg is not None:
            with _DECODE_LOCK, warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(tile.jpeg)) as thumb:
                    thumb.draft("RGB", (size, size))
                    picture = thumb.convert("RGB")
            picture.thumbnail((size, size), Image.Resampling.LANCZOS)
            sheet.paste(picture, (x0 + (size - picture.width) // 2, y0 + (size - picture.height) // 2))
        else:
            draw.line((x0 + 8, y0 + 8, x0 + size - 9, y0 + size - 9), fill=(110, 110, 110), width=3)
            draw.line((x0 + 8, y0 + size - 9, x0 + size - 9, y0 + 8), fill=(110, 110, 110), width=3)
        label = str(tile.number)
        box = draw.textbbox((0, 0), label, font=font)
        pad = 4
        draw.rectangle((x0, y0, x0 + box[2] - box[0] + 2 * pad, y0 + box[3] - box[1] + 2 * pad), fill=(0, 0, 0))
        draw.text((x0 + pad - box[0], y0 + pad - box[1]), label, font=font, fill=(255, 255, 255))
        if tile.is_video:
            r = max(10, size // 10)
            cx, cy = x0 + size - r - 6, y0 + size - r - 6
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 0, 0))
            draw.polygon(
                [(cx - r // 3, cy - r // 2), (cx - r // 3, cy + r // 2), (cx + r // 2, cy)], fill=(255, 255, 255)
            )
    out = BytesIO()
    sheet.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), width, height
