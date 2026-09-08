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

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.utils import get_cwidth

from wizolt.render import HorizontalRule, Theme, UiPrinter
from wizolt.tui.app import TuiApp


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
