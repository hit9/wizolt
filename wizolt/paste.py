"""Folded bracketed-paste input: the chip reference and the folding policy.

A long paste is a plain-text input concern, not an image one: nothing is stored, nothing reaches
the provider as anything but text. The reference and the threshold that decides when to fold live
here so `image.py` keeps owning images and the key binding keeps owning keys.
"""

from __future__ import annotations

from dataclasses import dataclass

# A bracketed paste longer than either bound stays a one-cell chip on the input line instead of
# filling it; the full text is restored by the projections that build messages and feed the editor.
PASTE_FOLD_MIN_LINES = 25
PASTE_FOLD_MIN_CHARS = 3000
PASTE_MARKER = "\ufffb"


@dataclass(frozen=True)
class PasteRef:
    """One bracketed paste folded to a one-cell chip while editing.

    The full text stays in memory for the projection that rebuilds the real message, so nothing
    extra is written down and no session schema changes."""

    text: str
    lines: int
    chars: int

    @classmethod
    def fold(cls, text: str) -> PasteRef | None:
        """The folding policy: a reference for a paste worth folding, None to insert it verbatim."""

        lines = text.count("\n") + 1
        if lines < PASTE_FOLD_MIN_LINES and len(text) < PASTE_FOLD_MIN_CHARS:
            return None
        return cls(text, lines, len(text))

    def label(self, index: int) -> str:
        size = f"{self.chars / 1024:.1f} KB" if self.chars >= 1024 else f"{self.chars} B"
        noun = "line" if self.lines == 1 else "lines"
        return f"[Pasted text #{index} \u00b7 {self.lines} {noun}, {size}]"
