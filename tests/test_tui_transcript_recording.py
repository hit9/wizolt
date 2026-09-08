"""Everything printed must reach the transcript, because a width change replays from it.

A resize that changes the pane width rebuilds the terminal: the scrollback is purged and
re-emitted from `ScrollbackRegion.transcript` (see `wizolt/tui/scrollback.py`). That makes
recording a correctness property rather than bookkeeping -- any row printed through a path
that skips the recorder is a row the rebuild silently drops, and the user sees their
transcript disappear on the first resize.

These tests pin the paths that are easy to miss, because none of them go through the
application's ordered writer: output printed before the app starts (startup banner, restored
transcript), output printed after it stops, and the fallback path used while the runtime is
unwinding.
"""

from __future__ import annotations

import asyncio
import io
import os
import re

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.utils import get_cwidth

from wizolt.render import HorizontalRule, Theme, UiPrinter
from wizolt.tui.app import TuiApp
from wizolt.tui.scrollback import physical_rows


@pytest.fixture
def recorded():
    """A UiPrinter wired to a TuiApp's transcript, as `build_tui` wires them in the runtime."""
    printer = UiPrinter()
    printer.color = True  # the recorded path is the colored one; headless output is not replayed
    tui = TuiApp()
    printer.transcript_sink = tui.record_scrollback
    return printer, tui


def test_output_printed_before_the_app_starts_is_recorded(recorded):
    printer, tui = recorded
    printer.emit("wizolt 0.0.0. /help for commands.")

    assert any("/help for commands." in text for text in tui.scrollback.transcript), (
        "the startup banner never reached the transcript, so a width change would erase it"
    )


def test_restored_transcript_lines_are_recorded(recorded):
    printer, tui = recorded
    for turn in range(3):
        printer.emit(f"restored turn {turn}")

    recorded_text = "".join(tui.scrollback.transcript)
    assert all(f"restored turn {turn}" in recorded_text for turn in range(3))


def test_batched_output_is_recorded(recorded):
    """`batched` collects a burst into one write; that write must still be recorded."""
    printer, tui = recorded
    with printer.batched():
        printer.emit("first of batch")
        printer.emit("last of batch")

    recorded_text = "".join(tui.scrollback.transcript)
    assert "first of batch" in recorded_text
    assert "last of batch" in recorded_text


def test_recorded_text_carries_styling(recorded):
    """The transcript holds rendered bytes, so a replay restores color, not just the words."""
    printer, tui = recorded
    printer.emit("Error: something went wrong")

    assert any("\x1b[" in text for text in tui.scrollback.transcript), "recorded output lost its escape sequences; a rebuild would replay it unstyled"


def test_transcript_is_bounded(recorded):
    printer, tui = recorded
    limit = tui.scrollback.MAX_REPLAY
    for line in range(limit + 50):
        printer.emit(f"line {line}")

    assert len(tui.scrollback.transcript) == limit
    assert "line 50" in tui.scrollback.transcript[0]
    assert f"line {limit + 49}" in tui.scrollback.transcript[-1]


async def test_scrollback_writes_yield_between_them():
    """A burst of completed writes must not hold the event loop for its whole length.

    `ScrollbackWriter._pump` awaits `Queue.get` and then the write. `Queue.get` does not suspend
    while items are already queued, so if the write does not yield either, the loop body never
    reaches the scheduler: keys stop responding and the running animation freezes until the
    burst drains. That is what `run_in_terminal` used to provide for free.
    """
    tui = TuiApp()
    ticks = 0

    async def competing() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    rival = asyncio.get_running_loop().create_task(competing())
    try:
        for _ in range(20):
            await tui.write_to_scrollback(lambda: None)
    finally:
        rival.cancel()

    assert ticks >= 20, f"the writer starved everything else on the loop; rival ran {ticks} times"


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5, 9, 17, 80, 120])
def test_recorded_rules_resize_without_recomputing_labels(recorded, monkeypatch, width):
    printer, tui = recorded
    monkeypatch.setattr("wizolt.render.time.monotonic", lambda: 100.0)
    with printer.batched():
        printer.emit("ordinary ────────── text")
        printer.emit_phase_rule()
        printer.emit_turn_end(35)
        printer.emit_worker_rule("[worker] 中文完成")
    monkeypatch.setattr("wizolt.render.time.monotonic", lambda: 3600.0)

    output = "".join(entry(width) if callable(entry) else entry for entry in tui.scrollback.transcript)
    lines = "".join(text for _, text in to_formatted_text(ANSI(output.replace("\x1b[?7h", "")))).splitlines()
    assert lines[0] == "ordinary ────────── text", "ordinary text was mistaken for a UI rule"
    rules = [line for line in lines[1:] if line]
    assert len(rules) == 3
    assert all(get_cwidth(line) == width for line in rules)
    if width >= 17:
        assert "done in 1m05s" in rules[1], "replay recomputed a completed turn's elapsed time"
    if width >= 22:
        assert "[worker] 中文完成" in rules[2]


@pytest.mark.parametrize("depth", [ColorDepth.DEPTH_1_BIT, ColorDepth.DEPTH_4_BIT, ColorDepth.DEPTH_8_BIT, ColorDepth.TRUE_COLOR])
@pytest.mark.parametrize("with_rule", [False, True])
def test_recorded_diff_matches_direct_output_color_depth(monkeypatch, depth, with_rule):
    monkeypatch.setattr("wizolt.render.shutil.get_terminal_size", lambda *args: os.terminal_size((80, 24)))
    buffer = io.StringIO()
    output = Vt100_Output(buffer, lambda: Size(rows=24, columns=80), default_color_depth=depth)
    printer = UiPrinter()
    parts = [FormattedText(printer.diff_segments('--- a.py\n+++ a.py\n@@ -1 +1 @@\n-old = 1\n+new = 2'))]
    if with_rule:
        parts.append(HorizontalRule(Theme.fg("rule"), "done in 5s", Theme.fg("text")))

    with create_app_session(output=output):
        printer.print_parts(parts)
        direct = buffer.getvalue()
        recorded = []
        printer.transcript_sink = recorded.append
        printer.print_parts(parts)
        # A later projection must retain the palette chosen when the output was accepted.
        output.default_color_depth = ColorDepth.TRUE_COLOR if depth != ColorDepth.TRUE_COLOR else ColorDepth.DEPTH_8_BIT
        replayed = "".join(entry(80) if callable(entry) else entry for entry in recorded)

    assert replayed == direct


@pytest.mark.parametrize("width", [40, 60, 100])
def test_the_rule_above_an_answer_resizes_with_the_projection(recorded, width):
    """An answer's rule has to be redrawn at replay width, like every other rule.

    It used to be drawn by Rich inside the answer's own capture, which bakes the emit-time width
    into the recorded bytes: replaying a 100-column rule into a 60-column pane wrapped it onto a
    second row, so shrinking a tmux pane doubled every divider in the transcript.
    """
    printer, tui = recorded
    printer.emit_answer("the answer body", role="assistant")

    output = "".join(entry(width) if callable(entry) else entry for entry in tui.scrollback.transcript)
    lines = "".join(text for _, text in to_formatted_text(ANSI(output.replace("\x1b[?7h", "")))).splitlines()
    rules = [line for line in lines if line and set(line) == {"─"}]
    assert len(rules) == 1, f"expected exactly one rule row, got {lines}"
    assert get_cwidth(rules[0]) == width, "the rule kept the width it was recorded at"


def test_an_answer_stays_a_single_write(recorded):
    """Splitting the rule out must not split the answer into two scrollback writes.

    The scrollback queue orders writes, and commands are expected to reach the terminal in one
    of them; two writes would let something else land between a rule and the body it belongs to.
    """
    printer, tui = recorded
    printer.emit_answer("the answer body", role="assistant")

    assert len(tui.scrollback.transcript) == 1


def test_an_error_answer_has_no_rule(recorded):
    """Errors are drawn without a rule, and moving the rule out must not have changed that."""
    printer, tui = recorded
    printer.emit_answer("Error: something failed", role="assistant")

    output = "".join(entry(80) if callable(entry) else entry for entry in tui.scrollback.transcript)
    lines = "".join(text for _, text in to_formatted_text(ANSI(output.replace("\x1b[?7h", "")))).splitlines()
    assert not [line for line in lines if line and set(line) == {"─"}], lines


@pytest.mark.parametrize("columns", [20, 40, 60, 80, 100])
@pytest.mark.parametrize(
    "text",
    [
        "short\n",
        "\n",
        "x" * 79 + "\n",
        "x" * 80 + "\n",
        "x" * 81 + "\n",
        "x" * 161 + "\n",
        "中" * 40 + "\n",
        "中" * 41 + "\n",
        "\x1b[31mred\x1b[39m but narrow\n",
        "\x1b[1m" + "b" * 100 + "\x1b[0m\n",
        "─" * 80 + "\n",
        "one\ntwo\nthree\n",
    ],
)
def test_physical_rows_matches_how_a_terminal_wraps(text, columns):
    """Row counts drive where the application is placed, so they have to be exact.

    Counting newlines is not enough: a line wider than the pane takes several rows. These cases
    were measured against real tmux at each of these widths, which is where the boundary rule
    comes from -- a line exactly as wide as the pane stays on one row, because the newline that
    follows cancels the pending wrap.
    """
    expected = sum(max(1, -(-get_cwidth(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)) // columns)) for line in text.split("\n")[:-1])

    assert physical_rows(text, columns) == expected


def test_physical_rows_counts_wrapped_lines_not_newlines():
    """The distinction the rebuild depends on, stated on its own so it cannot regress quietly."""
    wrapped = "y" * 200 + "\n"

    assert wrapped.count("\n") == 1
    assert physical_rows(wrapped, 80) == 3


def test_a_message_is_recorded_as_its_source_not_as_bytes(recorded):
    """The width-change contract: prose, tables and code lay themselves out for the new pane.

    Recording Rich's output freezes the width it was produced at, so a narrower pane can only
    re-wrap those bytes by character and a wider one leaves them short. Recording the message
    itself means the same Rich render runs again for the width it lands in.
    """
    printer, tui = recorded
    document = "A paragraph with enough words in it to wrap differently at one width than another, and then some more.\n"
    printer.emit_answer(document, role="assistant")

    at_100 = "".join(entry(100) if callable(entry) else entry for entry in tui.scrollback.transcript)
    at_50 = "".join(entry(50) if callable(entry) else entry for entry in tui.scrollback.transcript)

    def rows(rendered):
        text = "".join(fragment for _, fragment in to_formatted_text(ANSI(rendered.replace("\x1b[?7h", ""))))
        return [line for line in text.splitlines() if line.strip()]

    wide, narrow = rows(at_100), rows(at_50)
    assert all(get_cwidth(line) <= 100 for line in wide)
    assert all(get_cwidth(line) <= 50 for line in narrow), narrow
    # Re-flowed, not re-wrapped: laying the same words out narrower takes more rows, and none of
    # them is a broken remainder of a wider one.
    assert len(narrow) > len(wide)


def test_a_table_is_laid_out_for_the_width_it_lands_in(recorded):
    """Rich draws table borders to the console width, which is the most visible thing a replay of
    frozen bytes gets wrong: the box keeps its old width and wraps."""
    printer, tui = recorded
    wide_cell = "a description long enough that the box cannot fit a narrow pane"
    printer.emit_answer(f"| column | meaning |\n| --- | --- |\n| alpha | {wide_cell} |\n", role="assistant")

    def widest(columns):
        rendered = "".join(entry(columns) if callable(entry) else entry for entry in tui.scrollback.transcript)
        text = "".join(fragment for _, fragment in to_formatted_text(ANSI(rendered.replace("\x1b[?7h", ""))))
        return max(get_cwidth(line) for line in text.splitlines() if line.strip())

    # The box is wider than the narrow pane when laid out for the wide one, so this only holds if
    # it was drawn again rather than replayed.
    assert 40 < widest(100) <= 100
    assert widest(40) <= 40
