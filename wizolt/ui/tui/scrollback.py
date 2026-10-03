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
  The app stays anchored at the pane bottom. New output fills the gap after the transcript's
  tail before scrolling this region; it never moves or repaints the live input rows.

* A resize invalidates the projection: width changes rewrap rows, and height changes can
  move live rows into scrollback or clip transcript while the app is repainting.
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

import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prompt_toolkit.renderer import HeightIsUnknownError
from prompt_toolkit.utils import get_cwidth

if TYPE_CHECKING:
    from prompt_toolkit.application import Application
    from prompt_toolkit.renderer import Renderer

    from wizolt.ui.render import ScrollbackText


_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def physical_rows(text: str, columns: int) -> int:
    """How many terminal rows `text` occupies at `columns` wide.

    Counting newlines is not the same thing: a line wider than the pane takes several rows, and
    a rebuild that miscounts places the application at the wrong row. Counted per logical line,
    because that is how a terminal wraps -- and a line exactly as wide as the pane stays on one
    row, since the newline that follows cancels the pending wrap. Verified against tmux at 20,
    40, 60, 80 and 100 columns over plain, wide-character, styled and box-drawing text.
    """
    text = _SGR.sub("", text)
    if not text:
        return 0
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the newline that ended the last row, not a row of its own
    columns = max(1, columns)
    rows = 0
    for line in lines:
        rows += 1
        used = 0
        for char in line:
            if char == "\r":
                used = 0
                continue
            if char == "\b":
                used = max(0, used - 1)
                continue
            if char == "\t":
                if used < columns:
                    used = min((used // 8 + 1) * 8, columns - 1)
                continue
            width = max(0, get_cwidth(char))
            if not width:
                continue
            # A double-width glyph cannot occupy the last remaining cell. The terminal
            # wraps it as a whole, leaving that cell unused; dividing total width misses it.
            if used and used + width > columns:
                rows += 1
                used = 0
            used += width
    return rows


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


@dataclass
class _ReplayLayout:
    entries: dict[int, tuple[str, int]] = field(default_factory=dict)
    characters: int = 0


class ScrollbackRegion:
    """Owns the transcript and the escape sequences that project it onto the terminal."""

    # Bound retained writes, not physical rows (a batch can contain many lines). Older entries
    # cannot be restored after a purge, even if the terminal retained a larger native history.
    MAX_REPLAY = 5000
    # Two recently visited widths, at most ~8 MiB of Unicode text plus bounded entry metadata.
    MAX_CACHED_LAYOUT_CHARS = 1_000_000

    def __init__(self) -> None:
        # Number of rows above the next transcript line; None means the tail is unknown and
        # writes conservatively start immediately above the live app.
        self.tail_row: int | None = None
        self.transcript: list[ScrollbackText] = []
        self._pending: list[ScrollbackText] = []
        self._size: tuple[int, int] | None = None
        self._rebuild_owed = False
        self._transcript_start = 0
        self._layouts: dict[int, _ReplayLayout] = {}

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    def pending_rows(self, columns: int) -> int:
        """Space needed by accepted writes before placing a live preview after their tail."""
        text = "".join(item(columns) if callable(item) else item for item in self._pending)
        return physical_rows(text, columns) if text else 0

    def enqueue(self, text: ScrollbackText) -> None:
        """Accept text or a frozen width-dependent rendering. Project it on the next render."""
        if text:
            self._pending.append(text)

    def write_direct(self, text: ScrollbackText = "") -> None:
        """Drain accepted output and optionally print another write with no live application.

        Startup/restored output and unwinding writes must be recorded for later width changes.
        At shutdown there may also be accepted writes whose render never ran. Drain those first,
        before newer direct output, and allow the app to drain them even when no final write comes.
        """
        pending, self._pending = self._pending, []
        if text:
            pending.append(text)
        if not pending:
            return
        columns = shutil.get_terminal_size((80, 24)).columns
        for item in pending:
            sys.stdout.write(item(columns) if callable(item) else item)
        sys.stdout.flush()
        self._retain(pending)

    def note_size(self, rows: int, columns: int) -> bool:
        """Record projection dimensions independently of the renderer's disposable screen.

        The debt is sticky. A size change that happens while an exclusive viewer owns the
        alternate screen cannot be paid off then -- the transcript is not on screen, and purging
        would destroy the primary screen's scrollback for a rebuild nobody can see -- so it is
        carried until the app is back on the primary screen.
        """
        size = (rows, columns)
        self._rebuild_owed = self._rebuild_owed or (self._size is not None and self._size != size)
        self._size = size
        return self._rebuild_owed

    def recolor(self) -> None:
        """Owe a rebuild that re-renders every retained write in the active theme.

        The cached layouts hold rows drawn in the old colors, so they go; the rebuild itself is
        the one a width change runs, and is paid on the next render like that one.
        """
        self._layouts.clear()
        self._rebuild_owed = True

    def reanchor(self) -> None:
        """Replay retained rows after a resize invalidates the live preview's transcript anchor."""
        self._rebuild_owed = True

    def flush(self, app: Application) -> None:
        """Write queued transcript above the anchored app without repainting its live rows."""
        if not self._pending:
            return
        renderer = app.renderer
        if renderer.full_screen:
            # An exclusive viewer owns the alternate screen; there is no region above it that
            # belongs to the transcript. The lines wait for the primary screen to come back.
            return
        try:
            top = app_top_row(renderer)
        except HeightIsUnknownError:
            return
        screen = renderer.last_rendered_screen
        assert screen is not None  # app_top_row already refused a renderer without one
        out = renderer.output
        columns = out.get_size().columns
        text = "".join(item(columns) if callable(item) else item for item in self._pending)

        if top <= 0:
            return
        tail = min(top, self.tail_row) if self.tail_row is not None else top
        out.write_raw(f"\x1b[1;{top}r")
        out.write_raw(f"\x1b[{max(1, tail)};1H")
        out.write_raw(("\r\n" if tail else "") + _strip_trailing_newline(text))
        out.write_raw("\x1b[r")
        out.write_raw(f"\x1b[{top + renderer._cursor_pos.y + 1};{renderer._cursor_pos.x + 1}H")
        out.flush()
        self.tail_row = min(top, tail + physical_rows(text, columns))

        self._retain(self._pending)
        self._pending.clear()

    def _retain(self, writes: list[ScrollbackText]) -> None:
        self.transcript.extend(writes)
        dropped = max(0, len(self.transcript) - self.MAX_REPLAY)
        if dropped:
            del self.transcript[:dropped]
            for layout in self._layouts.values():
                for index in range(self._transcript_start, self._transcript_start + dropped):
                    entry = layout.entries.pop(index, None)
                    if entry is not None:
                        layout.characters -= len(entry[0])
            self._transcript_start += dropped

    def _replay_layout(self, columns: int) -> tuple[str, int]:
        """Reuse retained writes independently, even when the whole transcript exceeds the budget.

        Cache admission never evicts an earlier entry to fit a later one: a sequential replay
        would otherwise evict every upcoming hit and turn an oversized transcript into all misses.
        History eviction frees that space. Absolute write indices survive shifting the window.
        """
        layout = self._layouts.pop(columns, None)
        if layout is None:
            layout = _ReplayLayout()
            if len(self._layouts) == 2:
                del self._layouts[next(iter(self._layouts))]
        self._layouts[columns] = layout
        replayed: list[str] = []
        rows = 0
        for index, item in enumerate(self.transcript, self._transcript_start):
            entry = layout.entries.get(index)
            if entry is None:
                text = item(columns) if callable(item) else item
                entry = (text, physical_rows(text, columns))
                if layout.characters + len(text) <= self.MAX_CACHED_LAYOUT_CHARS:
                    layout.entries[index] = entry
                    layout.characters += len(text)
            replayed.append(entry[0])
            rows += entry[1]
        return "".join(replayed), rows

    def rebuild(self, app: Application) -> None:
        """Purge the terminal and re-emit the transcript at the current width.

        Called when projection geometry or colors change. See the module docstring for why
        purging is the deliberate choice here rather than a last resort.
        """
        renderer = app.renderer
        out = renderer.output
        rows, columns = out.get_size()
        # Where the app sits now, so the rebuild can put it back there. A rebuild that replays
        # from the top of the pane moves the app to wherever the transcript happens to end, which
        # reads as the whole session jumping upward on every resize.
        try:
            anchor = app_top_row(renderer)
        except HeightIsUnknownError:
            anchor = None
        self._rebuild_owed = False
        self.tail_row = None
        self._retain(self._pending)
        self._pending.clear()

        # Reset the scroll region and styles, home the cursor, clear the screen, then purge
        # scrollback. Order matches a shell's `clear` followed by `printf '\e[3J'`.
        out.write_raw("\x1b[r\x1b[0m\x1b[H\x1b[2J\x1b[3J\x1b[H")
        # Rows, not newlines: a wrapped line takes several, and the count decides where the app
        # is told it starts.
        replayed, written = self._replay_layout(columns)
        if anchor is not None and written < anchor:
            # Too little transcript to reach the app's old row. Open the gap above it rather than
            # below, so the app stays put instead of rising to meet a short transcript.
            out.write_raw("\r\n" * (anchor - written))
            written = anchor
        out.write_raw(replayed)
        out.flush()

        renderer.reset(leave_alternate_screen=False)
        # Every replayed line ended with a newline, so the cursor sits one row below the last
        # of them, or on the bottom row once the screen has scrolled. Telling the renderer
        # directly beats a CPR: the answer to a CPR describes a screen that the next resize in
        # a drag has already replaced.
        cursor_row = min(written, rows - 1)
        self.tail_row = cursor_row
        renderer._min_available_height = rows - cursor_row


def _strip_trailing_newline(text: str) -> str:
    # Captured terminal output ends in an SGR reset after its newline. Keep those styles,
    # but remove the newline itself or every flush scrolls an extra blank row into history.
    return re.sub(r"\r?\n((?:\x1b\[[0-9;]*m)*)$", r"\1", text, count=1)
