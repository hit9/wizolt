"""Real-tmux acceptance test for transcript integrity across repeated resizes.

The Terminal boundary section in `design/DESIGN.md` requires a real-tmux test: a deterministic
terminal model cannot reproduce tmux's reflow across multiple width transitions, its persistent
wrap flags, or the ordering between resize, SIGWINCH and redraw. Every earlier attempt at this
problem looked correct against a model or a single clean resize and failed on the second one.

What is asserted, and why each one is separate:

* Every marker the driver wrote is still present, exactly once. Under-erasing leaves duplicate
  fragments; over-erasing and mis-positioned writes destroy transcript. Counting distinct
  markers catches the first, comparing against the driver's own write log catches the second.
* Blank rows do not grow with the number of resize cycles. This is the artifact the shipped
  implementation accumulates: erasing clears cells but does not remove rows from the text flow.
* The prompt and status rows appear once. A stale copy left behind by a reflow shows up here.

Pre-existing shell history is deliberately *not* asserted to survive. A width change rebuilds
the terminal from the transcript and purges scrollback; see `wizolt/ui/tui/scrollback.py` for why
that trade is taken rather than guessing how many physical rows to delete.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest
import terminal_acceptance as acceptance
from terminal_acceptance import (
    TALL,
    WIDE,
    _settled_capture,
)

# Pytest collects the same behavioral scenarios against each backend fixture.
test_command_preview_follows_header_without_moving_input = acceptance.test_command_preview_follows_header_without_moving_input
test_streaming_tool_results_stay_attached_during_zoom = acceptance.test_streaming_tool_results_stay_attached_during_zoom
test_bar_cascade_previews_survive_resize_and_cancel = acceptance.test_bar_cascade_previews_survive_resize_and_cancel
test_blank_rows_do_not_grow_with_resize_cycles = acceptance.test_blank_rows_do_not_grow_with_resize_cycles
test_cli_startup_banner_survives_growing_and_shrinking_pane = acceptance.test_cli_startup_banner_survives_growing_and_shrinking_pane
test_cli_agents_switch_rebuilds_only_selected_transcript = acceptance.test_cli_agents_switch_rebuilds_only_selected_transcript
test_cli_subagent_approval_config_survives_resize_under_yolo = acceptance.test_cli_subagent_approval_config_survives_resize_under_yolo
test_cli_agents_live_preview_and_stop_keys = acceptance.test_cli_agents_live_preview_and_stop_keys
test_completed_rules_fit_each_width_without_losing_text = acceptance.test_completed_rules_fit_each_width_without_losing_text
test_exit_drains_output_accepted_before_the_last_render = acceptance.test_exit_drains_output_accepted_before_the_last_render
test_fresh_window_output_uses_available_screen_before_scrolling = acceptance.test_fresh_window_output_uses_available_screen_before_scrolling
test_long_inline_selector_keeps_context_visible = acceptance.test_long_inline_selector_keeps_context_visible
test_physical_rows_matches_terminal_wide_glyph_and_tab_wrapping = acceptance.test_physical_rows_matches_terminal_wide_glyph_and_tab_wrapping
test_transcript_survives_repeated_resize_cycles = acceptance.test_transcript_survives_repeated_resize_cycles

pytestmark = [pytest.mark.tmux, pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux")]

# Each test worker owns a server; a crash or personal tmux config must not affect other panes
# (especially the developer's own session). The last session's cleanup also stops this server.
SOCKET = f"wizolt-tests-{os.getpid()}"


def tmux(*args: str, check: bool = True) -> str:
    result = subprocess.run(["tmux", "-L", SOCKET, "-f", os.devnull, *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@pytest.fixture(params=["on", "off"], ids=["alt-screen-on", "alt-screen-off"])
def panes(tmp_path, request):
    """Hand out fresh tmux panes, each torn down at the end of the test.

    A test that needs two runs gets two panes rather than trying to reclaim one. Killing the
    driver in place meant sending Ctrl-C, which the selector swallows whenever it happens to be
    open, leaving the pane owned by an app that never exited and every later keystroke going to
    it instead of the shell.
    """
    created: list[str] = []

    def make() -> Pane:
        # One session per pane. These run under xdist, and a shared name makes two workers fight
        # over the same pane, which looks exactly like the corruption the test detects.
        session = f"wizolt-{request.node.name[:24]}-{os.getpid()}-{len(created)}"
        tmux("kill-session", "-t", session, check=False)
        tmux("new-session", "-d", "-s", session, "-x", str(WIDE), "-y", str(TALL), "-c", str(tmp_path), "sh")
        # Both halves of acceptance criterion 11. Nothing here uses the alternate screen, so the
        # projection must behave identically either way -- and a terminal with it disabled is
        # exactly where a stray 1049 would go unnoticed until it ate someone's screen.
        tmux("set-option", "-t", session, "-w", "alternate-screen", request.param)
        created.append(session)
        time.sleep(0.4)
        return Pane(session, tmp_path)

    try:
        yield make
    finally:
        for session in created:
            tmux("kill-session", "-t", session, check=False)


@pytest.fixture
def pane(panes):
    return panes()


class Pane(NamedTuple):
    session: str
    path: Path

    def send(self, keys: str) -> None:
        tmux("send-keys", "-t", self.session, keys, "Enter")

    def resize(self, width: int, height: int) -> None:
        tmux("resize-window", "-t", self.session, "-x", str(width), "-y", str(height))

    def capture(self) -> list[str]:
        return tmux("capture-pane", "-t", self.session, "-p", "-S", "-").split("\n")

    def visible(self):
        return tmux("capture-pane", "-t", self.session, "-p")

    def keys(self, *keys):
        tmux("send-keys", "-t", self.session, *keys)

    def literal(self, text):
        tmux("send-keys", "-t", self.session, "-l", text)

    def fresh_window(self):
        alternate = tmux("show-option", "-wv", "-t", self.session, "alternate-screen").strip()
        tmux("new-window", "-t", self.session, "env PS1='FRESH-READY> ' sh")
        tmux("set-option", "-w", "-t", self.session, "alternate-screen", alternate)

    def split(self, *, vertical=False):
        tmux("split-window", "-d", "-v" if vertical else "-h", "-t", self.session, "sh")

    def zoom(self):
        tmux("resize-pane", "-Z", "-t", self.session)


test_zoom_on_a_fresh_pane_leaves_one_live_region = pytest.mark.xfail(
    reason="see design/KNOWN_ISSUES.md: a pane height change cannot remove the rows it already drew", strict=False
)(acceptance.test_zoom_on_a_fresh_pane_leaves_one_live_region)


@pytest.mark.parametrize("stable", [False, True])
def test_capture_requires_a_stable_frame(tmp_path, monkeypatch, stable):
    frames = iter([["first"], ["second"], ["second" if stable else "third"]])
    pane = SimpleNamespace(path=tmp_path, capture=lambda: next(frames))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    if stable:
        assert _settled_capture(pane, attempts=2) == ["second"]
        assert not (tmp_path / "unstable-capture.txt").exists()
    else:
        with pytest.raises(AssertionError, match="pane did not settle"):
            _settled_capture(pane, attempts=2)
        assert (tmp_path / "unstable-capture.txt").read_text() == "third"
