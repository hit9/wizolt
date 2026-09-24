"""Image recognition reads format and size from the file's own header, without Pillow.

wizolt sends image bytes to the provider as they are, so all it needs from a file is what kind of
image it is and how large -- facts every supported format keeps in its first bytes. Pillow is a
test-only dependency here: it writes the fixtures, and it is the reference the parser must agree
with on every one of them.
"""

import io
import struct
import subprocess
import sys
import zlib

import pytest
from PIL import Image

from wizolt.base import ModelError
from wizolt.image import ImageInputs


def recognized(tmp_path, name: str, data: bytes):
    (tmp_path / name).write_bytes(data)
    value = ImageInputs(cwd=str(tmp_path)).recognize(name)
    return value.images[0] if value.images else None


def encode(image: Image.Image, image_format: str, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def frames(mode: str, size: tuple[int, int], count: int) -> list[Image.Image]:
    return [Image.new(mode, size, color) for color in ((200, 30, 30), (30, 200, 30), (30, 30, 200))[:count]]


FIXTURES = {
    "png-rgb": lambda: encode(Image.new("RGB", (32, 24), (12, 34, 56)), "PNG"),
    "png-rgba": lambda: encode(Image.new("RGBA", (7, 5), (1, 2, 3, 4)), "PNG"),
    "png-gray": lambda: encode(Image.new("L", (1, 1), 128), "PNG"),
    "png-palette": lambda: encode(Image.new("P", (1000, 3)), "PNG"),
    "png-tall": lambda: encode(Image.new("RGB", (3, 1000)), "PNG"),
    "png-animated": lambda: encode(frames("RGB", (9, 6), 2)[0], "PNG", save_all=True, append_images=frames("RGB", (9, 6), 2)[1:]),
    "jpeg-baseline": lambda: encode(Image.new("RGB", (640, 480), (12, 34, 56)), "JPEG"),
    "jpeg-progressive": lambda: encode(Image.new("RGB", (333, 77)), "JPEG", progressive=True),
    "jpeg-gray": lambda: encode(Image.new("L", (17, 19)), "JPEG"),
    "jpeg-cmyk": lambda: encode(Image.new("CMYK", (21, 13)), "JPEG"),
    "jpeg-exif": lambda: encode(Image.new("RGB", (50, 40)), "JPEG", exif=b"Exif\x00\x00" + b"II*\x00\x08\x00\x00\x00\x00\x00" + b"\x00" * 3000),
    "webp-lossy": lambda: encode(Image.new("RGB", (123, 45), (12, 34, 56)), "WEBP"),
    "webp-lossless": lambda: encode(Image.new("RGB", (1, 16383)), "WEBP", lossless=True),
    "webp-alpha": lambda: encode(Image.new("RGBA", (64, 32), (1, 2, 3, 100)), "WEBP"),
    "webp-animated": lambda: encode(frames("RGB", (40, 30), 2)[0], "WEBP", save_all=True, append_images=frames("RGB", (40, 30), 2)[1:]),
    "gif-single": lambda: encode(Image.new("P", (48, 36)), "GIF"),
    "gif-comment": lambda: encode(Image.new("P", (5, 3)), "GIF", comment=b"made by a test", transparency=0),
    "gif-rgb": lambda: encode(Image.new("RGB", (13, 11), (200, 10, 10)), "GIF"),
}


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_format_and_size_agree_with_pillow(tmp_path, fixture):
    data = FIXTURES[fixture]()
    with Image.open(io.BytesIO(data)) as reference:
        expected = (f"image/{reference.format.lower()}", reference.size)

    image = recognized(tmp_path, "shot.bin", data)

    assert image is not None
    assert (image.media_type, (image.width, image.height)) == expected
    assert image.size == len(data)


def test_the_extension_does_not_decide_the_format(tmp_path):
    """A JPEG named .png is a JPEG: the header decides, as it did under Pillow."""
    image = recognized(tmp_path, "actually-a-jpeg.png", FIXTURES["jpeg-baseline"]())

    assert image is not None and image.media_type == "image/jpeg"


def test_an_animated_gif_is_refused(tmp_path):
    data = encode(frames("RGB", (4, 4), 2)[0], "GIF", save_all=True, append_images=frames("RGB", (4, 4), 2)[1:], duration=10, loop=0)

    assert recognized(tmp_path, "animated.gif", data) is None
    with pytest.raises(ModelError, match="single-frame GIF"):
        ImageInputs._inspect(str(tmp_path / "animated.gif"))


@pytest.mark.parametrize("image_format", ["BMP", "TIFF", "ICO"])
def test_formats_providers_do_not_take_are_refused(tmp_path, image_format):
    data = encode(Image.new("RGB", (16, 16)), image_format)

    assert recognized(tmp_path, "picture.img", data) is None
    with pytest.raises(ModelError, match="Cannot read image"):
        ImageInputs._inspect(str(tmp_path / "picture.img"))


def pillow_accepts(data: bytes) -> bool:
    """What the Pillow-based inspection accepted: open, size, frame count, and verify."""
    try:
        with Image.open(io.BytesIO(data)) as opened:
            _ = opened.size, getattr(opened, "n_frames", 1)
            opened.verify()
    except Exception:  # noqa: BLE001 - any failure is a refusal, as it was in wizolt
        return False
    return True


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_a_file_cut_short_is_refused_wherever_pillow_refused_it(tmp_path, fixture):
    """A truncated copy or download is the common damage. Nothing Pillow refused may now pass:
    every cut point Pillow rejected, the header reader rejects too."""
    data = FIXTURES[fixture]()
    cuts = sorted(cut for cut in {*range(40), len(data) // 2, len(data) - 12, len(data) - 1} if 0 <= cut < len(data))
    for cut in cuts:
        if not pillow_accepts(data[:cut]):
            assert recognized(tmp_path, f"cut{cut}.bin", data[:cut]) is None, f"accepted {fixture} cut at {cut} of {len(data)} bytes"


@pytest.mark.parametrize("junk", [b"", b"not pixels", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4, b"GIF89a", b"RIFF\x00\x00\x00\x00WEBPVP8 ", b"\xff\xd8\xff\xd9"])
def test_garbage_behind_a_plausible_signature_is_refused(tmp_path, junk):
    assert recognized(tmp_path, "junk.png", junk) is None


def test_an_image_too_large_to_be_real_is_refused(tmp_path):
    """Pillow refused decompression bombs; a header claiming 20000x20000 pixels still is."""
    header = struct.pack(">IIBBBBB", 20000, 20000, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    data = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + chunk + struct.pack(">I", zlib.crc32(chunk))

    assert recognized(tmp_path, "bomb.png", data) is None
    with pytest.raises(ModelError, match="Cannot read image"):
        ImageInputs._inspect(str(tmp_path / "bomb.png"))


def test_recognizing_images_does_not_load_pillow(tmp_path):
    """Guards the dependency's removal: wizolt reads image headers itself, and Pillow is only a
    test fixture writer. A runtime import of PIL would fail here, and fail for users, who no
    longer install it."""
    for name in ("png-rgb", "jpeg-baseline", "webp-lossy", "gif-single"):
        (tmp_path / f"{name}.img").write_bytes(FIXTURES[name]())
    probe = (
        "import sys; from wizolt.image import ImageInputs;"
        f"value = ImageInputs(cwd={str(tmp_path)!r}).recognize('png-rgb.img jpeg-baseline.img webp-lossy.img gif-single.img');"
        "assert len(value.images) == 4, value.images;"
        "assert 'PIL' not in sys.modules, 'PIL was imported'"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True)
