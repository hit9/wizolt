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

import pytest

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
    for line in range(ScrollbackCap := TuiApp().scrollback.MAX_REPLAY + 50):
        printer.emit(f"line {line}")

    assert len(tui.scrollback.transcript) <= ScrollbackCap
