"""Local image input lifecycle and protocol payloads."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import struct
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar, Self

from wizolt.base import Json, ModelError, run_blocking
from wizolt.paste import PASTE_MARKER, PasteRef

if TYPE_CHECKING:
    from wizolt.session import Session


IMAGE_MARKER = "\ufffc"
IMAGE_REFS_KEY = "_images"
# Durable image references available to local tools but never projected as provider image blocks.
# A bridged attachment has already replaced its pixels with text; this marker keeps that projection
# irrevocable while letting the semantic message own the stored asset across resume.
IMAGE_TEXT_ONLY_KEY = "_images_text_only"
TOOL_IMAGE_OBSERVATION_KEY = "_tool_image_observation"
TOOL_IMAGE_QUESTION_KEY = "_tool_image_question"
TOOL_IMAGE_OBSERVATION_PREFIX = "[Tool image observation]"
# Durable plain-text block a text-only route receives after the configured [vision] provider
# perceives an attachment. Never projected as provider image blocks; the refs stay on the
# message only for asset ownership.
ATTACHMENT_VISION_OBSERVATION_PREFIX = "[Attachment image observation]"
FAILED_IMAGE_CONTEXT_PREFIX = "[Image input failed; local assets remain available through ViewImage]"
IMAGE_ASSET_CONTEXT_PREFIX = "[Attached image assets]"
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


@dataclass(frozen=True)
class ImageRef:
    _DIGEST_RE: ClassVar[re.Pattern] = re.compile(r"[0-9a-f]{64}")
    _CONTROL_CHAR_RE: ClassVar[re.Pattern] = re.compile(r"[\x00-\x1f\x7f]")
    ref: str
    name: str
    media_type: str
    width: int
    height: int
    size: int
    source_text: str = ""
    source_path: str = ""

    def to_json(self) -> Json:
        return {
            "ref": self.ref,
            "name": self.name,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "size": self.size,
            "source_text": self.source_text,
        }

    @classmethod
    def from_json(cls, value: object) -> ImageRef | None:
        if not isinstance(value, dict):
            return None
        try:
            ref = str(value["ref"])
            name = str(value["name"])
            media_type = str(value["media_type"])
            width = int(value["width"])
            height = int(value["height"])
            size = int(value["size"])
        except (KeyError, TypeError, ValueError):
            return None
        name = cls._safe_name(name)
        if not ImageRef._DIGEST_RE.fullmatch(ref) or not name or media_type not in SUPPORTED_FORMATS.values() or width <= 0 or height <= 0 or size <= 0:
            return None
        return cls(ref, name, media_type, width, height, size, str(value.get("source_text") or ""))

    @staticmethod
    def _safe_name(name: str) -> str:
        return ImageRef._CONTROL_CHAR_RE.sub("\ufffd", os.path.basename(name))


class UserInput(str):
    """A draft string whose one-cell markers map to immutable image or paste references."""

    images: tuple[ImageRef, ...]
    pastes: tuple[PasteRef, ...]

    def __new__(cls, text: str, images: tuple[ImageRef, ...] = (), pastes: tuple[PasteRef, ...] = ()) -> Self:
        value = super().__new__(cls, text)
        value.images = images
        value.pastes = pastes
        if text.count(IMAGE_MARKER) != len(images):
            raise ValueError("image marker count does not match image references")
        if text.count(PASTE_MARKER) != len(pastes):
            raise ValueError("paste marker count does not match paste references")
        return value

    def display_text(self) -> str:
        """What the user sees echoed: image labels and folded paste chips."""
        return self._expanded(
            lambda index, image: f"[Image #{index} \u00b7 {image.name}]",
            lambda index, paste: paste.label(index),
        )

    def original_text(self) -> str:
        """What the user actually typed: image source text and every paste opened in full."""
        return self._expanded(
            lambda _index, image: image.source_text or image.name,
            lambda _index, paste: paste.text,
        )

    def history_text(self) -> str:
        """What Ctrl-R should recall: source text back, pastes kept folded as chips."""
        return self._expanded(
            lambda _index, image: image.source_text or image.name,
            lambda index, paste: paste.label(index),
        )

    def model_text(self) -> str:
        """What reaches the model: pastes open into their full text, images stay labels."""
        return self._expanded(
            lambda index, image: f"[Image #{index} \u00b7 {image.name}]",
            lambda _index, paste: paste.text,
        )

    def queue_draft(self) -> str:
        """Projection for a queue/snapshot entry: image markers stay (they pair with the entry's
        image refs on resume), pastes open into full text because queue entries never carry paste refs."""
        return self._expanded(
            lambda _index, _image: IMAGE_MARKER,
            lambda _index, paste: paste.text,
        )

    def _expanded(
        self,
        image_replacement: Callable[[int, ImageRef], str],
        paste_replacement: Callable[[int, PasteRef], str],
    ) -> str:
        output: list[str] = []
        images = iter(enumerate(self.images, 1))
        pastes = iter(enumerate(self.pastes, 1))
        for char in str(self):
            if char == IMAGE_MARKER:
                index, image = next(images)
                output.append(image_replacement(index, image))
            elif char == PASTE_MARKER:
                index, paste = next(pastes)
                output.append(paste_replacement(index, paste))
            else:
                output.append(char)
        return "".join(output)


class ImageInputs:
    """Own image recognition, storage, semantic references, and wire projection for a session."""

    _TOKEN_RE = re.compile(r"(?:'[^'\n]*'|\"(?:\\.|[^\"\n])*\"|(?:\\.|[^\s])+)")
    _LEADING_PUNCTUATION = "([{<"
    _TRAILING_PUNCTUATION = ",;:!?)]}>"

    def __init__(self, session: Session | None = None, *, cwd: str = "") -> None:
        self.session = session
        self.cwd = session.cwd if session is not None else cwd or os.getcwd()
        self.retained_refs: set[str] = set()

    @staticmethod
    def refs(message: Json) -> tuple[ImageRef, ...]:
        raw = message.get(IMAGE_REFS_KEY)
        if not isinstance(raw, list):
            return ()
        return tuple(image for value in raw if (image := ImageRef.from_json(value)) is not None)

    @classmethod
    def input_refs(cls, message: Json) -> tuple[ImageRef, ...]:
        """Image refs that belong on the provider wire, excluding text-only local assets."""

        return () if message.get(IMAGE_TEXT_ONLY_KEY) is True else cls.refs(message)

    @classmethod
    def label_text(cls, message: Json) -> str:
        content = str(message.get("content") or "")
        images = cls.refs(message)
        if not images:
            return content
        # Current messages store labels in content. Keep older or hand-written structured
        # messages readable when their metadata has no visible labels.
        if any(f"[Image #{index}" in content for index in range(1, len(images) + 1)):
            return content
        labels = " ".join(f"[Image #{index} \u00b7 {image.name}]" for index, image in enumerate(images, 1))
        return " ".join(part for part in (labels, content) if part)

    def recognize(self, text: str, existing: tuple[ImageRef, ...] = (), existing_pastes: tuple[PasteRef, ...] = ()) -> UserInput:
        """Replace readable local image path tokens with markers, preserving existing markers and pastes."""

        if text.lstrip().startswith("/") and "\n" not in text:
            return UserInput(text, existing, existing_pastes)
        replacements: list[tuple[int, int, ImageRef]] = []
        known_refs = {image.ref for image in existing}
        for match in self._TOKEN_RE.finditer(text):
            raw = match.group(0)
            # A token that swallowed a neighbouring chip is not a path: recognizing one would
            # replace a span containing that marker and leave the reference tuples unbalanced.
            if IMAGE_MARKER in raw or PASTE_MARKER in raw:
                continue
            left_trimmed = raw.lstrip(self._LEADING_PUNCTUATION)
            candidate_raw = left_trimmed.rstrip(self._TRAILING_PUNCTUATION)
            leading = len(raw) - len(left_trimmed)
            trailing = len(left_trimmed) - len(candidate_raw)
            if not candidate_raw:
                continue
            decoded = self._decode_path_token(candidate_raw)
            if not decoded:
                continue
            decoded = os.path.expanduser(decoded)
            path = os.path.abspath(decoded if os.path.isabs(decoded) else os.path.join(self.cwd, decoded))
            image = self._inspect(path, source_text=candidate_raw, strict=False)
            if image is None or image.ref in known_refs:
                continue
            known_refs.add(image.ref)
            replacements.append((match.start() + leading, match.end() - trailing, image))
        if not replacements:
            return UserInput(text, existing, existing_pastes)
        by_start = {start: (end, image) for start, end, image in replacements}
        old_images = iter(existing)
        found: list[ImageRef] = []
        output: list[str] = []
        position = 0
        while position < len(text):
            if replacement := by_start.get(position):
                end, image = replacement
                output.append(IMAGE_MARKER)
                found.append(image)
                position = end
                continue
            char = text[position]
            output.append(char)
            if char == IMAGE_MARKER:
                found.append(next(old_images))
            position += 1
        return UserInput("".join(output), tuple(found), existing_pastes)

    async def admit(self, value: str | UserInput) -> UserInput:
        """Verify and store one submitted draft's images; the runtime's admission step.

        Copying each recognized image into the session's assets -- re-validating the source file
        against what was recognized -- runs on the executor, so a busy loop never copies bytes or
        opens images. Nothing is queued or snapshotted until this returns, and a failure leaves
        the draft untouched for the caller to hand back to the editor. Already-stored refs make
        this a cheap idempotent re-check."""

        if not isinstance(value, UserInput) or not value.images or self.session is None:
            return self._without_images(value)
        return await run_blocking(lambda: self.prepare(value))

    @staticmethod
    def _without_images(value: str | UserInput) -> UserInput:
        """The draft with nothing stored. Paste chips are not image state and must survive here:
        dropping them would leave their markers in the text with no reference to reopen."""

        return UserInput(str(value), (), value.pastes) if isinstance(value, UserInput) else UserInput(str(value))

    def prepare(self, value: str | UserInput) -> UserInput:
        """Validate and store a draft's images as session-owned assets."""

        if not isinstance(value, UserInput) or not value.images:
            return self._without_images(value)
        if self.session is None:
            return value
        return UserInput(str(value), tuple(self._store(image) for image in value.images), value.pastes)

    def message(self, value: str | UserInput) -> Json:
        stored = self.prepare(value)
        if not stored.images and not stored.pastes:
            return {"role": "user", "content": str(stored)}
        message: Json = {"role": "user", "content": stored.model_text()}
        if stored.images:
            self.retained_refs.difference_update(image.ref for image in stored.images)
            message[IMAGE_REFS_KEY] = [image.to_json() for image in stored.images]
        return message

    async def load(self, path: str, *, source_text: str = "") -> ImageRef:
        """Validate and store one explicit local image for model input, off the loop.

        The header inspection and the copy into the session's assets run on the executor; the loop
        only awaits. Cancellation waits for that worker (`run_blocking`), which cleans up its own
        staging file, so the request either publishes the completed content-addressed asset or
        nothing at all."""

        return await run_blocking(lambda: self._load_sync(path, source_text=source_text or path))

    def _load_sync(self, path: str, *, source_text: str) -> ImageRef:
        image = self._inspect(path, source_text=source_text)
        assert image is not None
        return self.prepare(UserInput(IMAGE_MARKER, (image,))).images[0]

    def tool_observation(self, images: tuple[ImageRef, ...], question: str = "") -> Json:
        """Build a durable multimodal user-role observation produced by a tool batch."""

        markers = " ".join(IMAGE_MARKER for _ in images)
        message = self.message(UserInput(TOOL_IMAGE_OBSERVATION_PREFIX + "\n" + markers, images))
        message[TOOL_IMAGE_OBSERVATION_KEY] = True
        if question:
            message[TOOL_IMAGE_QUESTION_KEY] = question
        return message

    @classmethod
    def is_tool_observation(cls, message: Json) -> bool:
        return (
            message.get("role") == "user"
            and message.get(TOOL_IMAGE_OBSERVATION_KEY) is True
            and str(message.get("content") or "").startswith(TOOL_IMAGE_OBSERVATION_PREFIX)
        )

    @staticmethod
    def tool_observation_question(message: Json) -> str:
        question = message.get(TOOL_IMAGE_QUESTION_KEY)
        return question if isinstance(question, str) else ""

    def retain(self, images: tuple[ImageRef, ...]) -> None:
        self.retained_refs.update(image.ref for image in images)

    def chat_content(self, message: Json, *, text_only: bool = False, payloads: dict[str, bytes] | None = None) -> str | list[Json]:
        return self._protocol_content(message, self._chat_image_part, "text", text_only=text_only, payloads=payloads)

    def responses_content(self, message: Json, *, text_only: bool = False, payloads: dict[str, bytes] | None = None) -> str | list[Json]:
        return self._protocol_content(message, self._responses_image_part, "input_text", text_only=text_only, payloads=payloads)

    def anthropic_content(self, message: Json, *, text_only: bool = False, payloads: dict[str, bytes] | None = None) -> str | list[Json]:
        return self._protocol_content(message, self._anthropic_image_part, "text", text_only=text_only, payloads=payloads)

    def _chat_image_part(self, image: ImageRef, payloads: dict[str, bytes] | None = None) -> Json:
        return {"type": "image_url", "image_url": {"url": self._data_url(image, payloads=payloads)}}

    def _responses_image_part(self, image: ImageRef, payloads: dict[str, bytes] | None = None) -> Json:
        return {"type": "input_image", "image_url": self._data_url(image, payloads=payloads)}

    def _anthropic_image_part(self, image: ImageRef, payloads: dict[str, bytes] | None = None) -> Json:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": base64.b64encode(self._bytes(image, payloads=payloads)).decode("ascii"),
            },
        }

    def vision_content(self, images: tuple[ImageRef, ...], api: str, text: str, *, payloads: dict[str, bytes] | None = None) -> list[Json]:
        """Content blocks for one explicit vision-provider request.

        The [vision] entry always carries the images, and the blocks are pre-built so projection leaves the
        request untouched (the message carries no IMAGE_REFS_KEY). Perception only: `text` is the
        question or the default observation instruction, never the coding task."""

        if api == "anthropic":
            parts = [self._anthropic_image_part(image, payloads) for image in images]
            text_type = "text"
        elif api == "responses":
            parts = [self._responses_image_part(image, payloads) for image in images]
            text_type = "input_text"
        else:
            parts = [self._chat_image_part(image, payloads) for image in images]
            text_type = "text"
        if text:
            parts.append({"type": text_type, "text": text})
        return parts

    def text_observation(self, images: tuple[ImageRef, ...], observation: str, question: str = "") -> Json:
        """Build a durable text-only observation produced by the [vision] provider.

        Same shape as `tool_observation` (a user-role tool observation), but the content is the
        plain vision text instead of image markers, and the occurrence-level text-only marker
        keeps those refs from ever being projected as provider image blocks — on this route and
        on any later one, including after a resume that lost the learned evidence.
        """

        message = self.tool_observation(images, question)
        message["content"] = f"{TOOL_IMAGE_OBSERVATION_PREFIX}\n{observation}"
        message[IMAGE_TEXT_ONLY_KEY] = True
        return message

    async def observe_current(
        self,
        messages: list[Json],
        current: list[Json],
        observe: Callable[[tuple[ImageRef, ...], str], Awaitable[str]],
    ) -> list[Json]:
        """Convert each current raw image occurrence in `messages` to a durable text observation.

        `current` lists the exact semantic message objects that entered the active turn since the
        last accepted main-model request (opening attachment, claimed queued attachment, ViewImage
        observation). Each is observed through `observe(refs, question)` — the vision provider —
        exactly once, with its own question (ViewImage) or the bounded default perception
        question (attachments). Older successful image history is never redescribed.
        """

        result = list(messages)
        for index, message in enumerate(result):
            if not any(message is item for item in current):
                continue
            images = self.input_refs(message)
            if not images:
                continue
            question = self.tool_observation_question(message)
            observation = await observe(images, question)
            if message.get(TOOL_IMAGE_OBSERVATION_KEY):
                result[index] = self.text_observation(images, observation, question)
            else:
                converted = dict(message)
                converted["content"] = f"{self.label_text(message)}\n\n{ATTACHMENT_VISION_OBSERVATION_PREFIX}\n{observation}"
                converted[IMAGE_TEXT_ONLY_KEY] = True
                result[index] = converted
        return result

    def settle_failed_messages(self, messages: list[Json]) -> None:
        """Make image occurrences in one failed turn safe to replay as text.

        The refs remain on the semantic message to retain their session-owned assets, while the
        text-only marker prevents those same failed occurrences from being resent as image blocks.
        New images in later turns are unaffected.
        """

        for message in messages:
            images = self.input_refs(message)
            if not images:
                continue
            paths = "\n".join(f"- {image.name}: {self.asset_path(image)}" for image in images)
            message["content"] = f"{self.label_text(message)}\n\n{FAILED_IMAGE_CONTEXT_PREFIX}\n{paths}"
            message[IMAGE_TEXT_ONLY_KEY] = True

    def asset_context(self, images: tuple[ImageRef, ...]) -> str:
        """Deterministic model-facing mapping from image labels to session-owned tool paths."""

        rows = [IMAGE_ASSET_CONTEXT_PREFIX]
        rows.extend(
            "- " + json.dumps({"image": index, "name": image.name, "path": self.asset_path(image)}, ensure_ascii=False) for index, image in enumerate(images, 1)
        )
        return "\n".join(rows)

    def _protocol_content(
        self,
        message: Json,
        image_part: Callable[[ImageRef, dict[str, bytes] | None], Json],
        text_type: str,
        *,
        text_only: bool = False,
        payloads: dict[str, bytes] | None = None,
    ) -> str | list[Json]:
        images = self.input_refs(message)
        if not images:
            # A pre-built content list for the explicit vision provider is already in wire shape.
            if isinstance(message.get("content"), list):
                return message["content"]
            return self.label_text(message)
        text = self.label_text(message)
        if text_only:
            # Route-specific projection for a static/learned text-only route: raw blocks are
            # suppressed but readable labels and the stable asset paths stay, so the model can
            # still ViewImage the stored files. The semantic message is not mutated.
            asset_context = self.asset_context(images)
            block: Json = {"type": text_type, "text": "\n\n".join(part for part in (text, asset_context) if part)}
            return [block]
        parts = [image_part(image, payloads) for image in images]
        asset_context = self.asset_context(images)
        parts.append({"type": text_type, "text": "\n\n".join(part for part in (text, asset_context) if part)})
        return parts

    def estimated_tokens(self, messages: list[Json]) -> int:
        session = self.session
        cap = session.policy.max_tokens_per_image(session.config.provider) if session is not None else 0
        total = 0
        for message in messages:
            images = self.input_refs(message)
            total += sum(self._estimated_tokens(image, cap) for image in images)
            if images:
                total += (len("\n\n" + self.asset_context(images)) + 3) // 4
        return total

    def assets_dir(self) -> str:
        session = self._session()
        from wizolt.session import SessionSnapshotStore  # local import: session is built on top of image

        path = SessionSnapshotStore.session_path(session.config.data_dir, session.cwd, session.uid)
        return path[: -len(".jsonl")] + ".assets"

    @staticmethod
    def _inspect(path: str, *, source_text: str = "", strict: bool = True) -> ImageRef | None:
        try:
            if not os.path.isfile(path):
                raise OSError("not a regular file")
            with open(path, "rb") as file:
                data = file.read()
            image_format, width, height, frames = ImageHeader(data).read()
            media_type = SUPPORTED_FORMATS.get(image_format)
            if media_type is None or (image_format == "GIF" and frames != 1):
                raise ValueError(UNSUPPORTED_IMAGE)
            ref = hashlib.sha256(data).hexdigest()
            return ImageRef(ref, ImageRef._safe_name(os.path.basename(path)), media_type, width, height, len(data), source_text, path)
        except (OSError, ValueError) as error:
            if strict:
                raise ModelError(f"Cannot read image {source_text or path}: {error}") from error
            return None

    def _store(self, image: ImageRef) -> ImageRef:
        if image.source_path:
            current = self._inspect(image.source_path, source_text=image.source_text)
            assert current is not None
            image = replace(current, source_text=image.source_text)
            destination = self.asset_path(image)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            if not self._asset_matches(destination, image.ref):
                fd, temporary = tempfile.mkstemp(prefix=".image-", dir=os.path.dirname(destination))
                os.close(fd)
                try:
                    shutil.copyfile(image.source_path, temporary)
                    with open(temporary, "rb") as file:
                        copied_ref = hashlib.file_digest(file, "sha256").hexdigest()
                    if copied_ref != image.ref:
                        raise ModelError(f"Image changed while it was being read: {image.source_text or image.name}")
                    os.replace(temporary, destination)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        elif not os.path.isfile(self.asset_path(image)):
            raise ModelError(f"Stored image is missing: {image.name} ({image.ref[:12]})")
        return replace(image, source_path="")

    def _payloads_sync(self, images: tuple[ImageRef, ...]) -> dict[str, bytes]:
        """Read and verify one request's distinct stored assets. The worker side of payload loading.

        Runs on an executor thread: one open + sha256 check per distinct ref, so a batch that
        mentions the same image in several messages reads its file exactly once. A corrupt or
        missing asset raises the same domain error the per-image read did."""

        payloads: dict[str, bytes] = {}
        for image in images:
            if image.ref in payloads:
                continue
            payloads[image.ref] = self._bytes(image)
        return payloads

    async def load_payloads(self, messages: list[Json]) -> dict[str, bytes]:
        """Load every distinct image referenced by `messages` once, off the loop.

        Returns a request-local mapping from ref to verified bytes; the caller builds wire parts
        from it and drops it when the request is done. Never cached on the session."""

        refs: dict[str, ImageRef] = {}
        for message in messages:
            for image in self.input_refs(message):
                refs.setdefault(image.ref, image)
        if not refs:
            return {}
        return await run_blocking(lambda: self._payloads_sync(tuple(refs.values())))

    async def payloads_for(self, refs: tuple[ImageRef, ...]) -> dict[str, bytes]:
        """Request-local bytes for one explicit ref set (a vision observation). Off the loop."""

        if not refs:
            return {}
        return await run_blocking(lambda: self._payloads_sync(refs))

    def _bytes(self, image: ImageRef, *, payloads: dict[str, bytes] | None = None) -> bytes:
        if payloads is not None:
            try:
                return payloads[image.ref]
            except KeyError:
                raise ModelError(f"Stored image is missing: {image.name} ({image.ref[:12]})") from None
        path = self.asset_path(image)
        try:
            with open(path, "rb") as file:
                data = file.read()
        except OSError as error:
            raise ModelError(f"Stored image is missing: {image.name} ({image.ref[:12]})") from error
        if hashlib.sha256(data).hexdigest() != image.ref:
            raise ModelError(f"Stored image is corrupt: {image.name} ({image.ref[:12]})")
        return data

    def _data_url(self, image: ImageRef, *, payloads: dict[str, bytes] | None = None) -> str:
        return f"data:{image.media_type};base64,{base64.b64encode(self._bytes(image, payloads=payloads)).decode('ascii')}"

    def asset_path(self, image: ImageRef) -> str:
        """Stable path of one stored image, suitable for a later ViewImage call."""

        return os.path.join(self.assets_dir(), image.ref)

    def _session(self) -> Session:
        if self.session is None:
            raise ModelError("Image input is not attached to a session")
        return self.session

    @staticmethod
    def _decode_path_token(token: str) -> str:
        try:
            values = shlex.split(token)
        except ValueError:
            return ""
        return values[0] if len(values) == 1 else ""

    @staticmethod
    def _asset_matches(path: str, ref: str) -> bool:
        try:
            with open(path, "rb") as file:
                return hashlib.file_digest(file, "sha256").hexdigest() == ref
        except OSError:
            return False

    @staticmethod
    def _estimated_tokens(image: ImageRef, cap: int = 0) -> int:
        """Use the common 512px-tile estimate without putting encoded bytes in context.

        `cap` is the route's documented per-image ceiling (0 when it has none): a provider that
        rescales every image to a fixed budget bills that ceiling no matter how large the original.
        """

        tiles = max(1, (image.width + 511) // 512) * max(1, (image.height + 511) // 512)
        estimate = 85 + 170 * tiles
        return min(estimate, cap) if cap else estimate
