"""Real-Zellij acceptance, using the same application and assertions as tmux."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import pytest
import terminal_acceptance as acceptance

# Pytest collects the same behavioral scenarios against each backend fixture.
test_command_preview_follows_header_without_moving_input = acceptance.test_command_preview_follows_header_without_moving_input
test_bar_cascade_previews_survive_resize_and_cancel = acceptance.test_bar_cascade_previews_survive_resize_and_cancel
test_blank_rows_do_not_grow_with_resize_cycles = acceptance.test_blank_rows_do_not_grow_with_resize_cycles
test_cli_startup_banner_survives_growing_and_shrinking_pane = acceptance.test_cli_startup_banner_survives_growing_and_shrinking_pane
test_cli_agents_switch_rebuilds_only_selected_transcript = acceptance.test_cli_agents_switch_rebuilds_only_selected_transcript
test_cli_subagent_approval_config_survives_resize_under_yolo = acceptance.test_cli_subagent_approval_config_survives_resize_under_yolo
test_completed_rules_fit_each_width_without_losing_text = acceptance.test_completed_rules_fit_each_width_without_losing_text
test_exit_drains_output_accepted_before_the_last_render = acceptance.test_exit_drains_output_accepted_before_the_last_render
test_fresh_window_output_uses_available_screen_before_scrolling = acceptance.test_fresh_window_output_uses_available_screen_before_scrolling
test_long_inline_selector_keeps_context_visible = acceptance.test_long_inline_selector_keeps_context_visible
test_physical_rows_matches_terminal_wide_glyph_and_tab_wrapping = acceptance.test_physical_rows_matches_terminal_wide_glyph_and_tab_wrapping
test_transcript_survives_repeated_resize_cycles = acceptance.test_transcript_survives_repeated_resize_cycles

pytestmark = [pytest.mark.zellij, pytest.mark.skipif(shutil.which("zellij") is None, reason="requires zellij")]


@pytest.fixture
def panes(tmp_path):
    from zellij_terminal import ZellijPane

    created = []
    # Keep Unix socket paths short and independent of developer sessions and xdist workers.
    with tempfile.TemporaryDirectory(prefix="wz-") as runtime:

        def make():
            path = tmp_path / f"pane-{len(created)}"
            path.mkdir()
            socket_dir = Path(runtime) / str(len(created))
            socket_dir.mkdir(mode=0o700)
            pane = ZellijPane(path, socket_dir)
            created.append(pane)
            return pane

        try:
            yield make
        finally:
            for pane in created:
                try:
                    (pane.path / "final-screen.txt").write_text("\n".join(pane.capture()))
                    (pane.path / "geometry.json").write_text(json.dumps(pane.geometry(), indent=2))
                except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as error:
                    (pane.path / "capture-error.txt").write_text(str(error))
                finally:
                    pane.close()


@pytest.fixture
def pane(panes):
    return panes()


def test_detach_and_reattach_at_new_size_preserves_transcript_and_draft(pane):
    log = pane.path / "reattach.log"
    pane.send(f"{sys.executable} {acceptance.DRIVER} 40 0.01 {log}")
    acceptance._wait_for_markers(log, 40)
    acceptance._stop_selectors(log)
    pane.literal("kept-draft")
    pane.wait_for("> kept-draft")

    for width, height in [(acceptance.NARROW, acceptance.SHORT), (acceptance.WIDE, acceptance.TALL)]:
        pane.detach()
        pane.attach(width, height)
        pane.resize(width, height)
        pane.wait_for("> kept-draft")
        lines = acceptance._settled_capture(pane)
        text = "\n".join(lines)
        assert Counter(re.findall(r"MARKER-(\d+)", text)) == Counter(f"{i:04d}" for i in range(1, 41))
        assert text.count("> kept-draft") == 1
        assert text.count("tmux-driver") == 1
        # Editing confirms that the reattached client still drives the live application.
        pane.literal("x")
        pane.wait_for("> kept-draftx")
        pane.keys("C-u")
        pane.literal("kept-draft")
        pane.wait_for("> kept-draft")


@pytest.mark.xfail(
    reason="design/KNOWN_ISSUES.md #3 also reproduced on Zellij 0.45.1: stale live region after height-only zoom",
    raises=AssertionError,
    strict=False,
)
def test_zoom_on_a_fresh_pane_leaves_one_live_region(pane):
    acceptance.test_zoom_on_a_fresh_pane_leaves_one_live_region(pane)
