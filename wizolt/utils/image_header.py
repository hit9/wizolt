"""Image format, size, and frame count, read from a file's own header."""

from __future__ import annotations

import struct

SUPPORTED_FORMATS = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}

# The pixel count past which a header is refused as a decompression bomb rather than a real image:
# the limit Pillow refused at, kept when the header reading below replaced it.
MAX_IMAGE_PIXELS = 2 * 89_478_485
UNSUPPORTED_IMAGE = "supported formats are PNG, JPEG, WebP, and single-frame GIF"


class ImageHeader:
    """Format, size, and frame count, read from a file's own header.

    wizolt sends image bytes to the provider as they are, so this is all it needs to know, and
    each supported format keeps it within its first bytes. Nothing is decoded: a valid header over
    damaged pixel data is the provider's to refuse. A file cut short -- the usual damage, a copy or
    download that did not finish -- is refused wherever Pillow refused it: a PNG without its IEND
    chunk, a WebP shorter than its RIFF size, a JPEG that ends before its scan starts, a GIF whose
    frame data runs out. Every read is bounds-checked, so a short or garbled file raises
    ValueError like an unsupported one."""

    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> tuple[str, int, int, int]:
        data = self.data
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            parsed = self.png()
        elif data.startswith((b"GIF87a", b"GIF89a")):
            parsed = self.gif()
        elif data.startswith(b"\xff\xd8"):
            parsed = self.jpeg()
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            parsed = self.webp()
        else:
            raise ValueError(UNSUPPORTED_IMAGE)
        _, width, height, _ = parsed
        if width <= 0 or height <= 0:
            raise ValueError("image has no pixels")
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError(f"image is {width}x{height}, larger than {MAX_IMAGE_PIXELS} pixels")
        return parsed

    def take(self, start: int, length: int) -> bytes:
        chunk = self.data[start : start + length]
        if start < 0 or len(chunk) != length:
            raise ValueError("image file is truncated")
        return chunk

    def png(self) -> tuple[str, int, int, int]:
        if self.take(12, 4) != b"IHDR":
            raise ValueError("PNG has no IHDR chunk")
        width, height = struct.unpack(">II", self.take(16, 8))
        if self.data.rfind(b"IEND") < 24:  # the closing chunk; a cut-short file lost it
            raise ValueError("image file is truncated")
        return "PNG", width, height, 1

    def gif(self) -> tuple[str, int, int, int]:
        width, height = struct.unpack("<HH", self.take(6, 4))
        flags = self.take(10, 1)[0]
        position = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)  # global color table
        frames = 0
        while frames < 2:  # one frame or more is all that matters
            if frames and position == len(self.data):
                break  # a complete frame and no trailer byte: Pillow took these, and so do we
            block = self.take(position, 1)[0]
            if block == 0x2C:  # image descriptor, then its optional local color table and data
                frames += 1
                local = self.take(position + 9, 1)[0]
                position += 10 + (3 * (2 << (local & 7)) if local & 0x80 else 0) + 1
                position = self.skip_sub_blocks(position)
            elif block == 0x21:  # extension: label, then data sub-blocks
                position = self.skip_sub_blocks(position + 2)
            elif block == 0x3B:  # trailer
                break
            else:
                raise ValueError("GIF block structure is corrupt")
        if not frames:
            raise ValueError("GIF has no image")
        return "GIF", width, height, frames

    def skip_sub_blocks(self, position: int) -> int:
        while length := self.take(position, 1)[0]:
            position += 1 + length
        return position + 1

    # Start-of-frame markers carry the size; C4, C8, and CC share the range but are not frames.
    JPEG_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

    def jpeg(self) -> tuple[str, int, int, int]:
        """The size is in the frame header; the walk goes on to the scan header after it, as
        Pillow's did, so a file that ends before its image data starts is refused."""
        position = 2
        size: tuple[int, int] | None = None
        while True:
            if self.take(position, 1) != b"\xff":
                raise ValueError("JPEG marker structure is corrupt")
            while self.take(position, 1) == b"\xff":  # fill bytes may pad any marker
                position += 1
            marker = self.take(position, 1)[0]
            position += 1
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone markers carry no length
                continue
            if marker == 0xD9:
                raise ValueError("JPEG ends before its image data")
            (length,) = struct.unpack(">H", self.take(position, 2))
            if length < 2:
                raise ValueError("JPEG segment length is corrupt")
            segment = self.take(position, length)
            if marker in self.JPEG_FRAME_MARKERS:
                height, width = struct.unpack(">HH", segment[3:7])
                size = (width, height)
            if marker == 0xDA:  # start of scan: the headers are complete
                if size is None:
                    raise ValueError("JPEG has no frame header")
                return "JPEG", size[0], size[1], 1
            position += length

    def webp(self) -> tuple[str, int, int, int]:
        (riff_size,) = struct.unpack("<I", self.take(4, 4))
        if len(self.data) < 8 + riff_size:  # the container states its own length
            raise ValueError("image file is truncated")
        chunk = self.take(12, 4)
        if chunk == b"VP8 ":  # lossy: the keyframe header after a 3-byte tag and start code
            if self.take(23, 3) != b"\x9d\x01\x2a":
                raise ValueError("WebP VP8 frame is corrupt")
            width, height = struct.unpack("<HH", self.take(26, 4))
            return "WEBP", width & 0x3FFF, height & 0x3FFF, 1
        if chunk == b"VP8L":  # lossless: 14-bit size fields packed after a signature byte
            if self.take(20, 1) != b"\x2f":
                raise ValueError("WebP VP8L header is corrupt")
            (bits,) = struct.unpack("<I", self.take(21, 4))
            return "WEBP", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, 1
        if chunk == b"VP8X":  # extended: the canvas size, 24-bit little-endian minus one
            size = self.take(24, 6)
            return "WEBP", int.from_bytes(size[:3], "little") + 1, int.from_bytes(size[3:], "little") + 1, 1
        raise ValueError("WebP has no image chunk")
