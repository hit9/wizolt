"""Resize re-anchoring: a multiplexer reflow (tmux zoom/unzoom) must not make the prompt climb.

Ending a tmux zoom moves the pane content upward before prompt-toolkit's size poll notices the
resize. The stock handler then erases from the row it remembers and trusts the next cursor
position report, so the drifted answer inflates the renderer's available height: the
application creeps toward the top of the pane and the transcript scrolls out of view. These
tests run the real TuiApp against a terminal model that tracks its cursor, scrolls at the
bottom edge, and answers CPR from wherever the cursor physically is, then repeat the reflow
cycle and require the prompt to stay anchored at the bottom.
"""

import re

from prompt_toolkit.data_structures import Size
from tui_harness import ResizableOutput, run_interactive_tui, wait_until

from wizolt.render import UiPrinter
from wizolt.tui.app import TuiApp

ROWS = 30
DRIFT = 3  # rows the pane content travels up on each unzoom
CYCLES = 6
TRANSCRIPT_ROWS = ROWS - 4  # the app's four bottom-anchored rows replace four transcript rows at startup


class ReflowingTerminal(ResizableOutput):
    """A terminal model that answers CPR from its tracked cursor row.

    Implements the output surface prompt-toolkit drives on the primary screen: cursor moves,
    newline writes that scroll once the cursor sits on the last row, erase operations, and a
    cursor position report delivered like a real terminal answers it — from the cursor's
    actual position, including wherever a reflow has just moved it.
    """

    def __init__(self, rows, columns):
        super().__init__(rows=rows, columns=columns)
        self.lines = [f"transcript {index:02d}" for index in range(1, rows + 1)]
        self.row = rows  # the session starts at the bottom of the scrollback
        self.column = 0
        self.report_cursor_row = None

    def get_rows_below_cursor_position(self):
        raise NotImplementedError

    @property
    def responds_to_cpr(self):
        return True

    def ask_for_cpr(self):
        if self.report_cursor_row is not None:
            self.report_cursor_row(self.row)

    def cursor_goto(self, row, column):
        self.row, self.column = row, column

    def cursor_up(self, amount):
        self.row -= amount

    def cursor_down(self, amount):
        self.row += amount

    def cursor_forward(self, amount):
        self.column += amount

    def cursor_backward(self, amount):
        self.column -= amount

    def write(self, data):
        for char in data:
            if char == "\r":
                self.column = 0
            elif char == "\n":
                if self.row >= self.size.rows:
                    del self.lines[0]
                    self.lines.append("")
                else:
                    self.row += 1
            elif char.isprintable():
                line = self.lines[self.row - 1]
                line = line[: self.column].ljust(self.column) + char + line[self.column + 1 :]
                self.lines[self.row - 1] = line
                self.column += 1

    def erase_down(self):
        self.lines[self.row - 1] = self.lines[self.row - 1][: self.column]
        for index in range(self.row, self.size.rows):
            self.lines[index] = ""

    def erase_end_of_line(self):
        self.lines[self.row - 1] = self.lines[self.row - 1][: self.column]

    def write_raw(self, data):
        """Interpret the escape sequences the scrollback path depends on.

        `DummyOutput.write_raw` discards, which would let this model pass a rebuild it never
        performed. Only the sequences wizolt emits are handled: purging scrollback and clearing
        the screen, which is what a width change does before replaying the transcript.
        """
        for match in re.finditer(r"\x1b\[([?0-9;]*)([A-Za-z])|([^\x1b]+)", data):
            params, final, text = match.groups()
            if text is not None:
                self.write(text)
            elif final == "H":
                row, _, column = params.partition(";")
                self.cursor_goto(int(row or 1), int(column or 1))
            elif final == "J" and params in ("2", "3"):
                # 2J clears the visible screen; 3J purges scrollback. This model has no
                # scrollback, so both come to the same thing here.
                self.lines = [""] * self.size.rows
            elif final == "J":
                self.erase_down()
            elif final == "m":
                pass  # styling has no effect on which rows exist

    def reflow_up(self, count):
        """The multiplexer moves the pane content up; the cursor travels with it."""
        del self.lines[:count]
        self.lines.extend([""] * count)
        self.row = max(1, self.row - count)


def test_resize_reflow_keeps_prompt_anchored_at_bottom(monkeypatch):
    output = ReflowingTerminal(ROWS, 80)
    app = TuiApp()
    printer = UiPrinter()
    printer.color = True
    printer.transcript_sink = app.record_scrollback
    prompt_rows = []
    transcript_rows = []

    def drive(_pipe_input):
        wait_until(lambda: any(line.startswith(UiPrinter.PROMPT_PREFIX) for line in output.lines))
        # Put the transcript on screen the way the runtime does, through the recorder. Rows that
        # reach the terminal any other way are not wizolt's to replay, and a width change drops
        # them -- that is the trade this design takes, and seeding them directly would assert the
        # opposite of what ships.
        for index in range(1, TRANSCRIPT_ROWS + 1):
            app.app.loop.call_soon_threadsafe(printer.emit, f"transcript {index:02d}")
        # Emits are batched into shared writes, so wait on the content rather than a count.
        wait_until(lambda: f"transcript {TRANSCRIPT_ROWS:02d}" in "".join(app.scrollback.transcript))

        def run_resize_cycle():
            finished = []
            app.app.loop.call_soon_threadsafe(lambda: (app.app._on_resize(), finished.append(True)))
            wait_until(lambda: finished)

        for cycle in range(CYCLES):
            output.reflow_up(DRIFT)
            output.size = Size(rows=ROWS, columns=50 if cycle % 2 else 100)
            run_resize_cycle()
            prompt_rows.append(tuple(index + 1 for index, line in enumerate(output.lines) if line.startswith(UiPrinter.PROMPT_PREFIX)))
            transcript_rows.append(sum(1 for line in output.lines if line.startswith("transcript")))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(
        monkeypatch,
        app,
        drive=drive,
        output=output,
        on_application=lambda app: setattr(output, "report_cursor_row", app.renderer.report_absolute_cursor_row),
    )

    # The prompt never moves, and only one copy of it is ever visible: every cycle re-anchors at
    # the same bottom row and erases the reflowed copy instead of leaving ghosts.
    assert len(set(prompt_rows)) == 1
    assert all(len(rows) == 1 for rows in prompt_rows)
    assert prompt_rows[0][0] >= ROWS - 6
    # The transcript does not bleed away cycle by cycle. Every width change replays it from the
    # recorder, so what is on screen is a full pane of transcript rather than whatever survived
    # the last reflow — this is the assertion the shipped erase-and-hope path cannot pass.
    assert min(transcript_rows) >= ROWS - 6, transcript_rows
    assert transcript_rows[-1] >= transcript_rows[0] - 1, transcript_rows
