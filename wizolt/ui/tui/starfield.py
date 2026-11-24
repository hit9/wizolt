"""A configurable sparkle field that frames the idle input box, in the spirit of Codex's Astra.

Default on. Tiny braille dots sit in fixed, hash-scattered cells across the input box's blank
background and twinkle in place: each dot's brightness rides a slow sine pulse (4-7s period)
raised to a high power, so most dots stay dim and a few briefly flare, then fade. The dot color
blends from the background toward the foreground with brightness, so a dim dot melts into the
box and a bright one reads as light. Nothing moves cell to cell, and the prompt, placeholder and
cursor cells are left clear. The field hides on typing and fades out over the idle timeout.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.controls import UIContent, UIControl

# The TUI already refreshes the idle screen every 0.2s.
TIMEOUT = 15.0
# Subtle braille dots, as Codex uses -- not large star glyphs.
DOTS = ("⠁", "⠂", "⠄", "⠈", "⠐", "⠠", "⡀", "⢀")
_MASK64 = (1 << 64) - 1


def _hash(x: int, y: int) -> int:
    h = (y * 65537 + x) & _MASK64
    h = ((h ^ (h >> 16)) * 0x45D9F3B) & _MASK64
    h = ((h ^ (h >> 16)) * 0x45D9F3B) & _MASK64
    return h ^ (h >> 16)


def _rgb(hex_color: str) -> tuple[int, int, int] | None:
    text = hex_color.lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


class StarfieldControl(UIControl):
    """Paint a deterministic twinkling dot field over the blank cells of one background row band.

    `fg_fn`/`bg_fn` supply the foreground and background hex colors to blend between; `y_offset`
    distinguishes stacked rows so they do not share a pattern; `reserved_fn` is the width of the
    prompt/draft prefix to keep clear on the input row; `visibility_fn` fades the whole field out.
    """

    def __init__(
        self,
        fg_fn: Callable[[], str],
        bg_fn: Callable[[], str],
        *,
        y_offset: int = 0,
        active_fn: Callable[[], bool] = lambda: True,
        visibility_fn: Callable[[], float] = lambda: 1.0,
        reserved_fn: Callable[[], int] = lambda: 0,
    ) -> None:
        self.fg_fn = fg_fn
        self.bg_fn = bg_fn
        self.y_offset = y_offset
        self.active_fn = active_fn
        self.visibility_fn = visibility_fn
        self.reserved_fn = reserved_fn

    def create_content(self, width: int, height: int) -> UIContent:
        if not self.active_fn():
            return UIContent(get_line=lambda _i: [("", " " * width)], line_count=height)
        now = time.monotonic()
        visibility = self.visibility_fn()
        fg = _rgb(self.fg_fn())
        bg = _rgb(self.bg_fn())
        # A theme may name colors rather than give hex; without two RGB endpoints there is nothing
        # to blend, so leave the background plain rather than risk the render.
        if fg is None or bg is None:
            return UIContent(get_line=lambda _i: [("", " " * width)], line_count=height)
        reserved = self.reserved_fn()
        lines: list[StyleAndTextTuples] = []
        for y in range(height):
            row: StyleAndTextTuples = []
            for x in range(width):
                if x < reserved:
                    row.append(("", " "))
                    continue
                h = _hash(x, y + self.y_offset)
                if h % 5 != 0:
                    row.append(("", " "))
                    continue
                phase = (now / (4.0 + (h % 31) / 10.0) + (h % 997) / 997.0) % 1.0
                brightness = math.sin(phase * math.pi) ** 12 * 0.95 * visibility
                if brightness < 0.04:
                    row.append(("", " "))
                    continue
                r = round(fg[0] * brightness + bg[0] * (1 - brightness))
                g = round(fg[1] * brightness + bg[1] * (1 - brightness))
                b = round(fg[2] * brightness + bg[2] * (1 - brightness))
                # A few dots flare into a star glyph at their brightest, as an accent.
                glyph = "✦" if h % 13 == 0 and brightness > 0.6 else DOTS[h // 161 % 8]
                row.append((f"fg:#{r:02x}{g:02x}{b:02x}", glyph))
            lines.append(row)
        return UIContent(get_line=lambda i: lines[i], line_count=height)
