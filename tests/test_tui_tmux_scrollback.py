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

Pre-existing shell history is deliberately *not* asserted to survive. A pane resize rebuilds
the terminal from the transcript and purges scrollback; see `wizolt/ui/tui/scrollback.py` for why
that trade is taken rather than guessing how many physical rows to delete.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest
import terminal_acceptance as acceptance

test_builtin_plugin_can_be_enabled_resized_and_disabled = acceptance.test_builtin_plugin_can_be_enabled_resized_and_disabled
test_plugin_views_shortcuts_and_resize_restore_input = acceptance.test_plugin_views_shortcuts_and_resize_restore_input
test_plugin_appearance_reload_preserves_input_and_updates_bars = acceptance.test_plugin_appearance_reload_preserves_input_and_updates_bars
from terminal_acceptance import (
    TALL,
    WIDE,
    _settled_capture,
)

# Pytest collects the same behavioral scenarios against each backend fixture.
test_command_preview_follows_header_without_moving_input = acceptance.test_command_preview_follows_header_without_moving_input
test_detail_sheets_stay_navigable_across_resize = acceptance.test_detail_sheets_stay_navigable_across_resize
test_streaming_tool_results_stay_attached_during_zoom = acceptance.test_streaming_tool_results_stay_attached_during_zoom
test_transcript_survives_resize_roundtrip_between_frames = acceptance.test_transcript_survives_resize_roundtrip_between_frames
test_bar_cascade_previews_survive_resize_and_cancel = acceptance.test_bar_cascade_previews_survive_resize_and_cancel
test_format_draft_and_status_tabs_survive_resizes = acceptance.test_format_draft_and_status_tabs_survive_resizes
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
        tmux("pipe-pane", "-O", "-t", session, f"cat > {shlex.quote(str(tmp_path / f'pane-{len(created)}.ansi'))}")
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

    # Observe frames on both sides of the quiet interval used for pane drags.
    settle_interval = 0.45

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


test_zoom_on_a_fresh_pane_leaves_one_live_region = acceptance.test_zoom_on_a_fresh_pane_leaves_one_live_region


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


def test_long_transcript_is_available_in_copy_mode_after_resize(pane):
    log = pane.path / "long.log"
    pane.send(f"{sys.executable} {acceptance.DRIVER} 540 0.001 {log}")
    acceptance._wait_for_markers(log, 516)
    pane.resize(45, 21)
    acceptance._wait_for_markers(log, 540)
    acceptance._stop_selectors(log)
    wire = pane.path / "pane-1.ansi"
    start = wire.stat().st_size
    for width, height in ((24, 12), (97, 32), (45, 23)):
        pane.resize(width, height)
        lines = _settled_capture(pane)
        markers = re.findall(r"MARKER-(\d+)", "\n".join(lines))
        assert markers == [f"{n:04d}" for n in range(1, 541)]
    if tmux("show-option", "-wv", "-t", pane.session, "alternate-screen").strip() == "on":
        assert b"MARKER-0001" not in wire.read_bytes()[start:]
    tmux("set-option", "-w", "-t", pane.session, "mode-keys", "vi")
    tmux("copy-mode", "-t", pane.session)
    pane.keys("C-u")
    assert int(tmux("display-message", "-p", "-t", pane.session, "#{scroll_position}")) > 0
    tmux("send-keys", "-t", pane.session, "-X", "history-top")
    assert "MARKER-0001" in tmux("display-message", "-p", "-t", pane.session, "#{copy_cursor_line}")


def test_dragging_split_boundary_settles_once_without_replaying_old_rows(pane):
    pane.split()
    log = pane.path / "drag.log"
    pane.send(f"{sys.executable} {acceptance.DRIVER} 160 0.002 {log}")
    acceptance._wait_for_markers(log, 160)
    acceptance._stop_selectors(log)
    _settled_capture(pane)
    wire = pane.path / "pane-1.ansi"
    start = wire.stat().st_size
    for width in range(44, 62, 2):
        tmux("resize-pane", "-t", pane.session, "-x", str(width))
        time.sleep(0.02)
    lines = _settled_capture(pane)
    assert re.findall(r"MARKER-(\d+)", "\n".join(lines)) == [f"{i:04d}" for i in range(1, 161)]
    emitted = wire.read_bytes()[start:]
    alternate = tmux("show-option", "-wv", "-t", pane.session, "alternate-screen").strip()
    # Count actual terminal writes; settled pixels alone hide intermediate replays.
    assert b"tmux-driver" in emitted
    if alternate == "on":
        assert b"MARKER-" not in emitted
        assert b"MARKER-0001" not in emitted
        assert b"\x1b[3J" not in emitted
    else:
        assert emitted.count(b"MARKER-0160") == 1


@pytest.mark.parametrize("resize", ["wider", "taller", "remote-zoom-wrapped", "remote-zoom-taller"])
def test_resize_keeps_transcript_on_attached_client(pane, resize):
    import pty
    import select
    import termios

    remote = resize.startswith("remote-zoom")
    pane.split(vertical=resize in ("taller", "remote-zoom-taller"))
    if resize == "remote-zoom-wrapped":
        # Each 42-column marker occupies two physical rows before zooming.
        tmux("resize-pane", "-t", pane.session, "-x", "32")
    log = pane.path / "attached.log"
    command = shlex.join([sys.executable, str(acceptance.DRIVER), "160", "0.002", str(log)])
    if remote:
        tmux("set-option", "-t", pane.session, "prefix", "C-a")
        # A second PTY forwards VT replies and SIGWINCH across the shell boundary.
        command = "env -u TMUX -u TMUX_PANE -u WIZOLT_TMUX_VIEWPORT TERM=xterm " + command
        command = "script -q /dev/null " + command if sys.platform == "darwin" else "script -q -c " + shlex.quote(command) + " /dev/null"
    pane.send(command)
    acceptance._wait_for_markers(log, 160)
    acceptance._stop_selectors(log)

    # pipe-pane sees only wizolt's writes. A client also sees tmux's own
    # repaint on resize, which can display the same transcript a second time.
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (TALL + 1, WIDE))

    def read_client():
        chunks = []
        deadline = time.monotonic() + 5
        while select.select([master], [], [], 0.15)[0]:
            assert time.monotonic() < deadline, "attached client did not settle"
            chunks.append(os.read(master, 65536))
        return b"".join(chunks)

    try:
        with subprocess.Popen(
            ["tmux", "-L", SOCKET, "attach-session", "-t", pane.session],
            stdin=slave, stdout=slave, stderr=slave, env={**os.environ, "TERM": "xterm-256color"},
        ) as client:
            try:
                assert b"MARKER-0160" in read_client()
                supported = tmux("show-option", "-wv", "-t", pane.session, "alternate-screen").strip() == "on"
                for zoomed in ([True, False] if remote else [False]):
                    wire = pane.path / "pane-1.ansi"
                    offset = wire.stat().st_size
                    if remote:
                        os.write(master, b"\x01z")
                    else:
                        tmux("resize-pane", "-t", pane.session, *(("-y", "23") if resize == "taller" else ("-x", "75")))
                    lines = _settled_capture(pane)
                    received = read_client()
                    assert received
                    assert tmux("display-message", "-p", "-t", pane.session, "#{window_zoomed_flag}").strip() == str(int(zoomed))
                    assert re.findall(r"MARKER-(\d+)", "\n".join(lines)) == [f"{i:04d}" for i in range(1, 161)]
                    emitted = wire.read_bytes()[offset:]
                    if supported:
                        displayed = re.findall(rb"MARKER-\d{4}", received)
                        assert len(displayed) == len(set(displayed))
                        assert b"MARKER-" not in emitted
                        if zoomed:
                            # Only the normal pane, not capture-pane -S - (which includes
                            # scrollback). The driver's idle editor and status take six rows.
                            visible = pane.visible().splitlines()
                            transcript_rows = len(visible) - 6
                            assert [row.split(" ", 1)[0] for row in visible[:transcript_rows]] == [
                                f"MARKER-{n:04d}" for n in range(161 - transcript_rows, 161)
                            ]
                            assert visible[-1].strip() == "tmux-driver"
                    elif remote:
                        assert b"MARKER-0001" in emitted
                if remote and supported:
                    tmux("copy-mode", "-t", pane.session)
                    tmux("send-keys", "-t", pane.session, "-X", "history-top")
                    first = tmux("display-message", "-p", "-t", pane.session, "#{copy_cursor_line}")
                    assert first.startswith("MARKER-0001")
            finally:
                client.terminate()
    finally:
        os.close(master)
        os.close(slave)
