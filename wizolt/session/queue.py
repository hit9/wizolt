"""User input queued while a turn is working: the text, its image refs, and the draft.

`Session.pending_user_inputs` holds these. The runtime loop owns queue mutation, and a snapshot
round-trips entries through `to_json` / `from_json`.
"""

from __future__ import annotations

from dataclasses import dataclass

from wizolt.base import Json
from wizolt.image import IMAGE_REFS_KEY, ImageRef, UserInput


@dataclass(eq=False)
class QueuedInput:
    text: str
    images: tuple[ImageRef, ...] = ()
    draft: str = ""
    inflight: bool = False
    # Input the user held back with Tab: `claim_user_inputs` skips it, so the engine never sees it
    # mid-turn and the runtime starts it as a fresh turn instead. Persisted so a resumed queue keeps
    # one held input per turn instead of merging them into the first one.
    next_turn: bool = False

    def to_json(self) -> str | Json:
        if not self.images and not self.next_turn:
            return self.text
        data: Json = {"text": self.text, "draft": self.draft}
        if self.images:
            data[IMAGE_REFS_KEY] = [image.to_json() for image in self.images]
        if self.next_turn:
            data["next_turn"] = True
        return data

    @classmethod
    def from_json(cls, value: object) -> QueuedInput | None:
        if isinstance(value, str):
            return cls(value) if value.strip() else None
        if not isinstance(value, dict):
            return None
        text = str(value.get("text") or "")
        raw_images = value.get(IMAGE_REFS_KEY)
        images = tuple(image for raw in raw_images if (image := ImageRef.from_json(raw)) is not None) if isinstance(raw_images, list) else ()
        draft = str(value.get("draft") or text)
        next_turn = value.get("next_turn") is True  # absent in snapshots written before the flag
        if not text.strip():
            return None
        if draft.count("\ufffc") != len(images):
            return cls(text, next_turn=next_turn)
        return cls(text, images, draft, next_turn=next_turn)

    def user_input(self) -> UserInput:
        return UserInput(self.draft or self.text, self.images)

    def message(self, prefix: str = "") -> Json:
        message: Json = {"role": "user", "content": prefix + self.text}
        if self.images:
            message[IMAGE_REFS_KEY] = [image.to_json() for image in self.images]
        return message
