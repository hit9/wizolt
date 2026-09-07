"""Completed output goes into native scrollback without the live app taking part.

The terminal holds two kinds of row that look identical to it: transcript rows, which are
append-only history, and the rows of the prompt-toolkit application, which are repainted
constantly. Printing above the app the ordinary way -- suspend, erase, print, repaint --
puts the application's rows into the terminal's text flow over and over, and once tmux
reflows the pane nothing can tell whose rows are whose. That is what leaves blank rows and
stale fragments behind.

Two things fix it, and both are needed:

* A DEC scroll region strictly above the application (`CSI 1;<top> r`) makes printed lines
  scroll the transcript -- and only the transcript -- off the top into native scrollback.
  The app's rows never move and are never erased, so prompt-toolkit's own bookkeeping stays
  valid and no row enters the text flow twice.

* A width change invalidates every row the terminal holds, because it rewraps all of them.
  Nothing can identify which reflowed rows belong to the app, so this module stops trying:
  it keeps the transcript in memory, purges the terminal, and re-emits. The terminal is a
  projection of the transcript, not the place the transcript lives.

The second half is a real trade, not a free win: purging takes the user's pre-existing shell
history with it. It is accepted deliberately, because the alternative -- guessing how many
physical rows to delete -- destroys transcript instead, which is worse.

Both steps run from inside the render cycle. The application's absolute position is derived
state that any resize invalidates, so deriving it from a timer races every resize and lands
lines on rows that belong to something else.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from prompt_toolkit.renderer import HeightIsUnknownError

if TYPE_CHECKING:
    from prompt_toolkit.application import Application
    from prompt_toolkit.renderer import Renderer


def app_top_row(renderer: Renderer) -> int:
    """0-based absolute row of the application's first row.

    Mirrors `Renderer.rows_above_layout`, but refuses to answer in the two states where the
    position is not actually known: before the first render has established it, and after a
    resize the renderer has not drawn at yet. Answering in either case means writing a scroll
    region computed from a screen that no longer exists, which corrupts the whole pane.
    """
    size = renderer.output.get_size()
    if renderer._min_available_height <= 0 or renderer._last_size != size:
        raise HeightIsUnknownError
    screen = renderer.last_rendered_screen
    if screen is None:
        raise HeightIsUnknownError
    return size.rows - max(renderer._min_available_height, screen.height)


class ScrollbackRegion:
    """Owns the transcript and the escape sequences that project it onto the terminal."""

    # Replaying is linear in rows, so a long session rebuilds only its tail. Beyond this the
    # oldest rows are dropped: they are already gone from the terminal's own history too.
    MAX_REPLAY = 5000

    def __init__(self) -> None:
        self.transcript: list[str] = []
        self._pending: list[str] = []
        self._width: int | None = None

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    def enqueue(self, text: str) -> None:
        """Accept one rendered write. Nothing reaches the terminal until the next render."""
        if text:
            self._pending.append(text)

    def write_direct(self, text: str) -> None:
        """Record and print one write with no live application to place it above.

        Startup banners, restored transcript and the writes that land while the runtime is
        unwinding all come through here. They must still be recorded: a later width change
        rebuilds the terminal from the transcript, and whatever is missing from it is gone.
        """
        if not text:
            return
        self.transcript.append(text)
        del self.transcript[: max(0, len(self.transcript) - self.MAX_REPLAY)]
        sys.stdout.write(text)
        sys.stdout.flush()

    def note_width(self, columns: int) -> bool:
        """Record the width being rendered at, and report whether it changed."""
        changed = self._width is not None and self._width != columns
        self._width = columns
        return changed

    def flush(self, app: Application) -> None:
        """Write queued transcript above the app. Call only from inside the render cycle."""
        if not self._pending:
            return
        renderer = app.renderer
        try:
            top = app_top_row(renderer)
        except HeightIsUnknownError:
            return
        screen = renderer.last_rendered_screen
        assert screen is not None  # app_top_row already refused a renderer without one
        out = renderer.output
        rows = out.get_size().rows
        text = "".join(self._pending)

        # Scrolling the app down first, when it has not reached the pane bottom yet, is what
        # lets a session that started mid-screen grow into the bottom instead of pinning the
        # transcript to the few rows above wherever the app first appeared.
        moved = 0
        bottom = top + screen.height
        if bottom < rows:
            moved = min(text.count("\n"), rows - bottom)
            if moved:
                out.write_raw(f"\x1b[{top + 1};{rows}r")
                out.write_raw(f"\x1b[{top + 1};1H")
                out.write_raw("\x1bM" * moved)
                out.write_raw("\x1b[r")
                top += moved
                renderer._min_available_height = rows - top

        if top == 0:
            # The app fills the pane; there is no row above it to write into. Hold the lines
            # until it has somewhere to go rather than overwriting the app with them.
            return

        cursor_top = top - moved - 1 if moved else top - 1
        out.write_raw(f"\x1b[1;{top}r")
        out.write_raw(f"\x1b[{cursor_top + 1};1H")
        # The cursor sits on the last row of the region, which already holds a line, so each
        # write has to scroll before it prints. Recorded text ends with its own newline; that
        # trailing one would scroll a blank row in, so it moves to the front instead.
        out.write_raw("\r\n" + _strip_trailing_newline(text))
        out.write_raw("\x1b[r")
        out.write_raw(f"\x1b[{top + renderer._cursor_pos.y + 1};{renderer._cursor_pos.x + 1}H")
        out.flush()

        self.transcript.append(text)
        del self.transcript[: max(0, len(self.transcript) - self.MAX_REPLAY)]
        self._pending.clear()

    def rebuild(self, app: Application) -> None:
        """Purge the terminal and re-emit the transcript at the current width.

        Called when the width changed, which rewrote every row in the pane. See the module
        docstring for why purging is the deliberate choice here rather than a last resort.
        """
        renderer = app.renderer
        out = renderer.output
        rows = out.get_size().rows
        self.transcript.extend(self._pending)
        self._pending.clear()
        del self.transcript[: max(0, len(self.transcript) - self.MAX_REPLAY)]

        # Reset the scroll region and styles, home the cursor, clear the screen, then purge
        # scrollback. Order matches a shell's `clear` followed by `printf '\e[3J'`.
        out.write_raw("\x1b[r\x1b[0m\x1b[H\x1b[2J\x1b[3J\x1b[H")
        replayed = 0
        for text in self.transcript:
            out.write_raw(text)
            replayed += text.count("\n")
        out.flush()

        renderer.reset(leave_alternate_screen=False)
        # Every replayed line ended with a newline, so the cursor sits one row below the last
        # of them, or on the bottom row once the screen has scrolled. Telling the renderer
        # directly beats a CPR: the answer to a CPR describes a screen that the next resize in
        # a drag has already replaced.
        cursor_row = min(replayed, rows - 1)
        renderer._min_available_height = rows - cursor_row


def _strip_trailing_newline(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text
