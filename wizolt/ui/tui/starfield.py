"""A configurable sparkle field that frames the idle input box.

Default off. When enabled it draws sparse stars in the padding rows above and below the input
line -- never in the input row itself, so it cannot cover the draft, the cursor or a selection.
It hides the moment you type and again after a short idle timeout, so a quiet terminal stays
quiet (the failure mode that made Codex's always-on Astra sparkles unpopular).
"""

from __future__ import annotations

import random
import time

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.controls import UIContent, UIControl

# Seconds between animation frames, and seconds of idle input before the field hides itself.
INTERVAL = 0.25
TIMEOUT = 15.0
# Fraction of blank cells that carry a star, and the glyphs they draw from. Sparse on purpose.
DENSITY = 0.05
GLYPHS = "✦✧·•*+"


class StarfieldControl(UIControl):
    """Render `height` rows of `width` cells as a time-seeded sparse star field.

    The middle row -- the one aligned with the input line -- is left fully transparent so the
    input shows through untouched; only the rows above and below it carry stars. A transparent
    float lets an unstyled space pass the underlying cell through, so blank cells keep the input
    box's background and only the star glyphs themselves are painted.
    """

    def __init__(self, style: str = "class:input.sparkle") -> None:
        self.style = style

    def create_content(self, width: int, height: int) -> UIContent:
        rng = random.Random(int(time.monotonic() / INTERVAL))
        middle = height // 2
        lines: list[StyleAndTextTuples] = []
        for y in range(height):
            if y == middle:
                lines.append([("", " " * width)])
                continue
            row: StyleAndTextTuples = []
            for _ in range(width):
                if rng.random() < DENSITY:
                    row.append((self.style, rng.choice(GLYPHS)))
                else:
                    row.append(("", " "))
            lines.append(row)
        return UIContent(get_line=lambda i: lines[i], line_count=height)
