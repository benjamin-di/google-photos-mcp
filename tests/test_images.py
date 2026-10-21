"""Image decoding guards (magic bytes, bombs, draft) and the contact-sheet layout."""

import warnings
from io import BytesIO

import pytest
from conftest import make_image
from PIL import Image

from photos_mcp import images
from photos_mcp.errors import PhotosError
from photos_mcp.images import MAX_PX, Tile, contact_sheet, grid_shape, sniff, tile_size, to_jpeg


def test_downscales_to_longest_side():
    jpeg, width, height = to_jpeg(make_image(3000, 1000), 512)
    assert jpeg[:3] == b"\xff\xd8\xff" and (width, height) == (512, 171)


def test_small_images_are_not_upscaled_but_re_encoded():
    jpeg, width, height = to_jpeg(make_image(100, 80, "WEBP"), 1024)
    assert (width, height) == (100, 80) and jpeg[:3] == b"\xff\xd8\xff"


def test_alpha_is_flattened():
    jpeg, width, height = to_jpeg(make_image(64, 64, mode="RGBA"), 64)
    assert (width, height) == (64, 64)


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"\xff\xd8\xff\xe0rest", "jpeg"),
        (b"\x89PNG\r\n\x1a\nrest", "png"),
        (b"GIF89a....", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"\x00\x00\x00\x18ftypheic....", "heic"),
        (b"\x00\x00\x00\x18ftypavif....", "avif"),
        (b"<html>login</html>", None),
        (b"", None),
    ],
)
def test_magic_bytes(data, kind):
    assert sniff(data) == kind


def test_non_image_is_item_unavailable_without_reaching_pillow(monkeypatch):
    monkeypatch.setattr(images.Image, "open", lambda *a, **k: pytest.fail("Pillow must not see it"))
    with pytest.raises(PhotosError) as caught:
        to_jpeg(b"<html>Sign in</html>", 512)
    assert caught.value.code == "item_unavailable"


def test_truncated_image_is_item_unavailable():
    with pytest.raises(PhotosError) as caught:
        to_jpeg(make_image(200, 200, "JPEG")[:40], 128)
    assert caught.value.code == "item_unavailable"


def test_decompression_bomb_warning_is_an_error(monkeypatch):
    # A 7000 x 7000 PNG is over the 40 MP warning threshold but under the hard limit.
    monkeypatch.setattr(images.Image, "MAX_IMAGE_PIXELS", 1000)
    data = make_image(40, 40)  # 1600 px > 1000 → warning; < 2000 → no hard error
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(PhotosError) as caught:
            to_jpeg(data, 64)
    assert caught.value.code == "item_unavailable"
    data = make_image(50, 50)  # over twice the limit: Pillow raises outright
    with pytest.raises(PhotosError):
        to_jpeg(data, 64)


def test_jpeg_is_drafted_before_load(monkeypatch):
    from PIL import JpegImagePlugin

    calls = []
    original = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        calls.append((mode, size, self._im is None))
        return original(self, mode, size)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    to_jpeg(make_image(2000, 1500, "JPEG"), 256)
    assert calls[0] == ("RGB", (256, 256), True)  # before load(): nothing decoded yet


@pytest.mark.parametrize(("count", "shape"), [(1, (1, 1)), (4, (2, 2)), (5, (3, 2)), (24, (5, 5))])
def test_grid_shape(count, shape):
    assert grid_shape(count) == shape
    cols, rows = shape
    size = tile_size(count)
    assert cols * size + 6 * (cols + 1) <= MAX_PX and rows * size + 6 * (rows + 1) <= MAX_PX


def test_contact_sheet_draws_numbered_tiles_and_placeholders():
    thumb, _, _ = to_jpeg(make_image(300, 200), 256)
    tiles = [Tile(n, thumb if n != 3 else None, is_video=n == 2) for n in range(1, 25)]
    data, width, height = contact_sheet(tiles)
    image = Image.open(BytesIO(data))
    assert image.size == (width, height) and max(image.size) <= MAX_PX
    # Tile 1's number badge is black-on-white text at its top-left corner.
    badge = image.crop((6, 6, 30, 30)).convert("L")
    assert min(badge.get_flattened_data()) < 60 and max(badge.get_flattened_data()) > 180


def test_contact_sheet_needs_tiles():
    with pytest.raises(ValueError):
        contact_sheet([])
