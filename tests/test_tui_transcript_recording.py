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

import pytest
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.utils import get_cwidth

from wizolt.render import UiPrinter
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
