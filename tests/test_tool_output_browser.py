"""tool output browser (split from tests/test_command_ui.py)."""

import asyncio
import json
import os
import shutil
import time

import pytest
from prompt_toolkit.utils import get_cwidth
from test_command_ui import ModalHarness
from tui_harness import loop, session

import wizolt.ui.cli.modals as modals_mod
from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import load_session
from wizolt.base import ToolCall
from wizolt.session.jobs import BackgroundJob
from wizolt.tools import BashTool, JobTool, Tool, tooloutput
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.modals import job_view, tool_output_viewer
from wizolt.ui.cli.resume import ResumeRenderer
from wizolt.ui.render import Theme
from wizolt.ui.tui import TuiApp


async def test_failed_script_is_not_marked_successful_in_browser(tmp_path, monkeypatch):
    command_loop = loop(tmp_path)
    command_loop.session.settings.yolo = True
    await command_loop.agent.tools.run([ToolCall("script", "ToolScript", [{"code": "raise ValueError('boom')"}])])
    command_loop.session.store_tool_result("Bash", ["echo ok"], Tool.process_result("BashToolResult", 0, "ok", ""))
    modal = await _open_detail(command_loop, ["enter"], monkeypatch)
    assert any(("class:choice.output.fail", "✗ ") in row for row in _display_rows(modal.frames))


async def test_silent_failed_bash_is_available_for_inspection(tmp_path, monkeypatch):
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result("Bash", ["false"], Tool.process_result("BashToolResult", 1, "", ""))
    modal = await _open_detail(command_loop, ["enter"], monkeypatch)
    assert modal.frames
    assert "false" in "".join(text for _, text in modal.frames[-1])
    assert any("choice.output.fail" in style for style, _ in modal.frames[-1])


async def test_job_fallback_preserves_literal_output(tmp_path, monkeypatch):
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result("Job", [{"action": "wait", "job": "job.9"}], "<build>\n# literal heading\n    indented\n</build>")
    modal = await _open_detail(command_loop, ["enter"], monkeypatch, rows=40)
    text = "".join(text for _, text in modal.frames[-1])
    assert "<build>" in text and "</build>" in text
    assert "# literal heading" in text and "    indented" in text


async def test_detail_title_fits_a_narrow_terminal(tmp_path, monkeypatch):
    command_loop = loop(tmp_path)
    key = command_loop.session.store_tool_result("Edit", ["long.py"], "edited")
    command_loop.session.store_turn_diff(key, 1, "目录/" * 30 + "long.py", "--- long.py\n+++ long.py\n@@ -1 +1 @@\n-a\n+b\n")
    modal = await _open_detail(command_loop, ["enter"], monkeypatch, columns=30)
    text = "".join(text for _, text in modal.frames[-1])
    assert get_cwidth(text.splitlines()[0]) <= 30
    assert "read-only" in text.splitlines()[0]


@pytest.mark.parametrize("rows", [20, 26, 40])
async def test_tool_output_browser_sheet_fits_the_modal_window(tmp_path, monkeypatch, rows):
    """The sheet -- title, rule, rows, counter, legend -- fits the rows the modal window gets, so
    the counter and the key legend are never cut off at the bottom (the old cap assumed ten rows of
    list in a twenty-row terminal, where the window holds ten rows in all). Below twenty rows the
    window cannot hold the fixed rows plus one of list, and no sizing of the list changes that."""
    command_loop = loop(tmp_path)
    for index in range(30):
        command_loop.session.store_tool_result("Bash", [f"printf {index}"], Tool.process_result("BashToolResult", 0, f"out {index}", ""))
    modal = ModalHarness(["q"])
    command_loop.presentation.tui = modal

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, rows)))
        await tool_output_viewer(command_loop)

    sheet = "".join(value for _, value in modal.frames[0]).rstrip("\n").splitlines()
    assert len(sheet) <= TuiApp.modal_rows(rows)
    assert sheet[-1].strip().startswith("j/k/Tab move") and "showing 1-" in sheet[-3]


async def test_tool_output_viewer_browses_recent_calls_through_a_viewport_and_opens_full_output(tmp_path, monkeypatch):
    command_loop = loop(tmp_path)
    for index in range(12):
        stdout = "\n".join(f"line {line}" for line in range(40)) if index == 10 else f"output {index}"
        stderr = "detail stderr" if index == 10 else ""
        command_loop.session.store_tool_result("Bash", [f"printf command-{index}"], Tool.process_result("BashToolResult", 0, stdout, stderr))
    command_loop.session.store_tool_result("Bash", ["true"], Tool.process_result("BashToolResult", 0, "", ""))
    modal = ModalHarness(["j", "enter", "G"])  # second entry, then scroll the viewer to the bottom
    command_loop.presentation.tui = modal

    # ``shutil`` is a shared module object also used by pytest's terminal reporter. Restore the
    # patch before pytest reports this test result, rather than waiting for fixture teardown.
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    listing = "".join(value for _, value in modal.frames[0])
    assert listing.startswith("  Tool output · latest 12\n")  # the title stands on its own line
    assert get_cwidth(listing.splitlines()[1]) == 48  # over a rule that spans the sheet
    # A blank row flanks the rule, and the key legend closes the sheet.
    rows = listing.rstrip("\n").splitlines()
    assert rows[2] == "" and rows[-2] == "" and rows[-1].strip().startswith("j/k/Tab move")
    assert "command-11" in listing and "command-3" in listing
    # A twenty-six-row terminal draws nine of the twelve: the rest are a scroll away, not dropped, and
    # the counter is what says so. `true` printed nothing and is not an entry at all.
    assert "Bash  printf command-1\n" not in listing and "Bash  printf command-0\n" not in listing and "Bash  true" not in listing
    assert "showing 1-9 of 12" in listing
    # The second entry opens in the scrolling viewer: the command as its body, the streams below.
    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    viewer = [frame for frame in frames if "read-only" in frame]
    assert "Output · tr.11 · read-only" in viewer[0]
    assert "1  printf command-10" in viewer[0]
    assert "── command " in viewer[0] and "── result " in viewer[0]
    assert "stdout:" in viewer[0] and "line 0" in viewer[0]
    assert "stderr:" in viewer[-1] and "detail stderr" in viewer[-1]  # both streams, a scroll away
    assert modal.exclusive == [False, True]  # the list shares the screen; the viewer takes it


async def test_tool_output_browser_marks_bash_results_ok_and_fail(tmp_path, monkeypatch):
    """A Bash row's first column carries its verdict: a green ✓ for exit 0, a red ✗ for any other
    exit. The list is mostly bash, so the failures should be scannable by color."""
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result("Bash", ["printf ok"], Tool.process_result("BashToolResult", 0, "ok output", ""))
    command_loop.session.store_tool_result("Bash", ["make check"], Tool.process_result("BashToolResult", 2, "", "target failed"))
    modal = ModalHarness(["j", "q"])
    command_loop.presentation.tui = modal

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    # The selected row is reversed as a whole, which hides its mark's own color, so the two frames
    # (cursor on the failure, then on the success) each expose one verdict column in its color.
    pairs = [(style, value) for frame in modal.frames for style, value in frame]
    assert ("class:choice.output.ok", "✓ ") in pairs
    assert ("class:choice.output.fail", "✗ ") in pairs


async def test_tool_output_browser_checks_every_stored_call(tmp_path, monkeypatch):
    """A stored record exists only for a call that completed, so every row answers "did it do
    what was asked": an Edit gets the same green check a successful Bash gets, and only a
    nonzero Bash exit turns the check red. A call that failed has no record and no row -- it
    stays a red block in the transcript."""
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result("Edit", [{"path": "src/app.py"}], "Edited src/app.py (12 lines)")
    command_loop.session.store_tool_result("Bash", ["make check"], Tool.process_result("BashToolResult", 2, "", "target failed"))
    modal = ModalHarness(["j", "q"])
    command_loop.presentation.tui = modal

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    rows = _display_rows(modal.frames)
    # The selected row is reversed as a whole, which hides its mark's own color; the cursor visits
    # both rows across the frames, so each mark shows up in its own color in one of them.
    assert any(("class:choice.output.ok", "✓ ") in row and any("Edit" in value for _, value in row) for row in rows)
    assert any(("class:choice.output.fail", "✗ ") in row and any("make check" in value for _, value in row) for row in rows)


def _styled(frames) -> list[tuple[str, str]]:
    """The flat (style, text) fragments of the captured modal frames."""
    return [(style, value) for frame in frames for style, value in frame]


def _display_rows(frames) -> list[list[tuple[str, str]]]:
    """Captured frames split back into display rows: the modal builds its sheet from fragments and
    separates rows with a newline, so folding them back is what lets a test look at one row."""
    rows: list[list[tuple[str, str]]] = [[]]
    for style, value in _styled(frames):
        for index, part in enumerate(value.split("\n")):
            if index:
                rows.append([])
            if part:
                rows[-1].append((style, part))
    return rows


def _rows_with(rows: list[list[tuple[str, str]]], needle: str) -> list[list[tuple[str, str]]]:
    return [row for row in rows if any(needle in part for _, part in row)]


async def _open_detail(command_loop, keys, monkeypatch, *, columns: int = 60, rows: int = 26):
    """Drive the browser with `keys` against a fixed terminal size and return the harness."""
    modal = ModalHarness(keys, consumed=True)
    command_loop.presentation.tui = modal
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((columns, rows)))
        await tool_output_viewer(command_loop)
    return modal


async def test_tool_output_browser_opens_an_edits_whole_recorded_diff(tmp_path, monkeypatch):
    """The transcript trims a long replay's diffs, so the browser is where an edit is read whole.

    The detail scrolls to lines a replay would have dropped, and it keeps the preview's colors:
    syntax-highlighted code, banded added and removed lines, unfilled context -- the bands filled
    to the pane edge, which is how the diff viewer framed them."""
    command_loop = loop(tmp_path)
    count = ResumeRenderer.TRANSCRIPT_DIFF_LINES + 10  # longer than a replay redraws
    added = "\n".join(f"+zebra{index}" for index in range(count))
    diff = f"--- x.py\n+++ x.py\n@@ -1,1 +1,{count} @@\n-zebra_removed\n zebra_context\n{added}\n"
    key = command_loop.session.store_tool_result("Edit", ["x.py"], '<Edit path="x.py">\n')
    command_loop.session.store_turn_diff(key, 1, "x.py", diff, round=1)
    modal = await _open_detail(command_loop, ["enter", "G"], monkeypatch)  # open the edit, then scroll to its end

    assert "Edit" in "".join(value for _, value in modal.frames[0])
    top, bottom = modal.frames[2], modal.frames[-1]
    assert "Diff · x.py" in "".join(value for _, value in top)
    assert f"zebra{count - 1}" not in "".join(value for _, value in top)  # one screen, not the whole diff
    assert f"zebra{count - 1}" in "".join(value for _, value in bottom)  # and the rest is reachable
    assert "more lines" not in "".join(value for _, value in top)  # no trimmed-away tail marker

    added_band, removed_band = Theme.diff_style("diff.added.bg"), Theme.diff_style("diff.removed.bg")
    removed_row = _rows_with(_display_rows([top]), "zebra_removed")[0]
    context_row = _rows_with(_display_rows([top]), "zebra_context")[0]
    assert any(removed_band in style for style, _ in removed_row)
    assert not any(added_band in style or removed_band in style for style, _ in context_row)
    changed = [row for row in _display_rows([bottom]) if any(part == "+" for _, part in row)]
    assert changed and all(sum(get_cwidth(part) for _, part in row) == 58 for row in changed)
    assert all("".join(part for _, part in row).startswith("  │ ") and "".join(part for _, part in row).endswith(" │") for row in changed)
    assert all(any(added_band in style for style, _ in row) for row in changed)


async def test_tool_output_browser_opens_the_edit_its_row_names(tmp_path, monkeypatch):
    """Each row carries the key of its own receipt: opening the older edit shows the older diff,
    not whichever edit came last."""
    command_loop = loop(tmp_path)
    first = command_loop.session.store_tool_result("Edit", ["a.py"], '<Edit path="a.py">\n')
    command_loop.session.store_turn_diff(first, 1, "a.py", "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-zebra_first_old\n+zebra_first_new\n", round=1)
    second = command_loop.session.store_tool_result("Edit", ["b.py"], '<Edit path="b.py">\n')
    command_loop.session.store_turn_diff(second, 2, "b.py", "--- b.py\n+++ b.py\n@@ -1 +1 @@\n-zebra_second_old\n+zebra_second_new\n", round=2)
    modal = await _open_detail(command_loop, ["j", "enter"], monkeypatch)  # newest first, so row two is a.py

    text = "".join(value for _, value in _styled([modal.frames[-1]]))
    assert "Diff · a.py" in text
    assert "zebra_first_old" in text
    assert "zebra_second" not in text


async def test_tool_output_browser_still_lists_an_edit_whose_receipt_is_gone(tmp_path, monkeypatch):
    """Past the retained window a session holds no receipt for an old edit. The row stays, opening
    the output it did retain, which carries the diff inside the edit block."""
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result("Edit", ["x.py"], '<Edit path="x.py">\n--- x.py\n+++ x.py\n@@ -1 +1 @@\n-zebra_old\n+zebra_new\n</Edit>')
    modal = await _open_detail(command_loop, ["enter"], monkeypatch)

    assert "Edit" in "".join(value for _, value in modal.frames[0])
    text = "".join(value for _, value in _styled([modal.frames[-1]]))
    assert "Edit · tr.1" in text
    assert "zebra_new" in text


async def test_tool_output_browser_omits_a_section_whose_body_is_empty(tmp_path, monkeypatch):
    """An empty body is not a rule with a blank numbered row under it: the section is left out, and
    the sheet is its fields alone."""
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result("Job", [{"action": "status", "job": "job.9"}], "")
    modal = await _open_detail(command_loop, ["enter"], monkeypatch)

    text = "".join(value for _, value in _styled([modal.frames[-1]]))
    assert "Job · tr.1" in text
    assert "── job" not in text


async def test_tool_output_browser_lists_past_the_old_fifty_entry_cap(tmp_path, monkeypatch):
    """The browser's list reaches as far back as the session stores: 400 results, not a page of
    fifty. The viewport still shows one screenful with the counter saying how far there is to go."""
    command_loop = loop(tmp_path)
    for index in range(55):
        command_loop.session.store_tool_result("Bash", [f"printf {index}"], Tool.process_result("BashToolResult", 0, f"out {index}", ""))
    modal = ModalHarness(["q"])
    command_loop.presentation.tui = modal

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    listing = "".join(value for _, value in modal.frames[0])
    assert listing.startswith("  Tool output · latest 55\n")
    assert "showing 1-9 of 55" in listing
    assert "Bash  printf 54" in listing  # the newest is in view


async def test_tool_output_browser_keeps_every_stored_record_with_a_running_script(tmp_path, monkeypatch):
    """A running ToolScript's live entry does not push the oldest stored record out: with a full
    session the browser lists all 400 stored results plus the running one, and the viewport still
    bounds the screen."""
    command_loop = loop(tmp_path)
    for index in range(400):
        command_loop.session.store_tool_result("Bash", [f"printf {index}"], Tool.process_result("BashToolResult", 0, f"out {index}", ""))
    command_loop.presentation.script_running_code = "print('hi')\n"
    modal = ModalHarness(["q"])
    command_loop.presentation.tui = modal

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    listing = "".join(value for _, value in modal.frames[0])
    assert listing.startswith("  Tool output · latest 401\n")
    assert "showing 1-9 of 401" in listing
    assert "ToolScript" in listing  # the running script's live entry is listed too


async def test_tool_output_viewer_escape_returns_to_the_list_with_the_cursor_kept(tmp_path, monkeypatch):
    """Esc (or q) in a detail goes back to the list instead of closing the whole browser, and
    the reopened list still points at the entry the reader came from."""
    command_loop = loop(tmp_path)
    for index in range(5):
        command_loop.session.store_tool_result(
            "Bash",
            [f"printf command-{index}"],
            Tool.process_result("BashToolResult", 0, f"output {index}", ""),
        )
    # j moves to the second entry, enter opens it, escape returns to the list, enter opens the
    # same entry again, c-o closes the whole browser.
    modal = ModalHarness(["j", "enter", "escape", "enter", "c-o"], consumed=True)
    command_loop.presentation.tui = modal
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    listings = [frame for frame in modal.frames if "Tool output" in "".join(value for _, value in frame)]
    assert len(listings) == 5  # two list passes: three renders, then two on the reopened one
    assert modal.exclusive == [False, True, False, True]  # list, detail, list, detail

    def selected_line(listing: list) -> str:
        # The selection band is the only marker a selected row carries.
        return "".join(value for style, value, *_ in listing if style == "class:choice.selected")

    assert "command-4" in selected_line(listings[0])  # first list starts at the newest entry
    assert "command-3" in selected_line(listings[1])  # j moved to the second entry
    # The reopened list still sits on the entry the escape came back from, not the top.
    assert "command-3" in selected_line(listings[3])
    # The same detail opened twice: once before the escape, once before the c-o, each rendering
    # its title row twice (initial frame plus the frame after its closing key).
    assert sum("read-only" in frame for frame in frames) == 4


async def test_tool_output_viewer_q_in_a_detail_also_returns_to_the_list(tmp_path, monkeypatch):
    """q behaves exactly like Esc inside a detail: back to the list, not out of the browser."""
    command_loop = loop(tmp_path)
    for index in range(3):
        command_loop.session.store_tool_result(
            "Bash",
            [f"printf command-{index}"],
            Tool.process_result("BashToolResult", 0, f"output {index}", ""),
        )
    # enter opens the top entry, q returns to the list, enter opens it again, c-o closes.
    modal = ModalHarness(["enter", "q", "enter", "c-o"], consumed=True)
    command_loop.presentation.tui = modal
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    listings = [frame for frame in frames if "Tool output" in frame]
    assert modal.exclusive == [False, True, False, True]  # list, detail, list, detail
    assert len(listings) == 4  # q came back to the list: two renders per list pass
    assert sum("read-only" in frame for frame in frames) == 4  # the detail opened twice


async def test_tool_output_viewer_ctrl_c_in_a_detail_also_returns_to_the_list(tmp_path, monkeypatch):
    """Ctrl-C behaves like Esc inside a detail: back to the list, not out of the browser."""
    command_loop = loop(tmp_path)
    for index in range(3):
        command_loop.session.store_tool_result(
            "Bash",
            [f"printf command-{index}"],
            Tool.process_result("BashToolResult", 0, f"output {index}", ""),
        )
    # enter opens the top entry, c-c returns to the list, enter opens it again, c-o closes.
    modal = ModalHarness(["enter", "c-c", "enter", "c-o"], consumed=True)
    command_loop.presentation.tui = modal
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    listings = [frame for frame in frames if "Tool output" in frame]
    assert modal.exclusive == [False, True, False, True]  # list, detail, list, detail
    assert len(listings) == 4  # c-c came back to the list: two renders per list pass
    assert sum("read-only" in frame for frame in frames) == 4  # the detail opened twice


async def test_tool_output_viewer_ctrl_o_in_a_detail_closes_the_browser(tmp_path, monkeypatch):
    """Ctrl-O inside a detail still closes the whole browser: only Esc/q go back to the list."""
    command_loop = loop(tmp_path)
    for index in range(3):
        command_loop.session.store_tool_result(
            "Bash",
            [f"printf command-{index}"],
            Tool.process_result("BashToolResult", 0, f"output {index}", ""),
        )
    modal = ModalHarness(["j", "enter", "c-o"], consumed=True)
    command_loop.presentation.tui = modal
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    assert modal.exclusive == [False, True]  # list, detail -- and no reopened list
    assert sum("Tool output" in frame for frame in frames) == 3  # the list rendered its three frames once
    assert sum("read-only" in frame for frame in frames) == 2  # the detail closed the browser


async def test_tool_output_viewer_keeps_the_search_filter_across_an_escape(tmp_path, monkeypatch):
    """A `/` filter survives Esc back to the list: the reopened list is still filtered."""
    command_loop = loop(tmp_path)
    for index in range(5):
        command_loop.session.store_tool_result(
            "Bash",
            [f"printf command-{index}"],
            Tool.process_result("BashToolResult", 0, f"output {index}", ""),
        )
    # Filter to the newest entry, open it, Esc back: the reopened list still shows just that
    # entry, then the second enter opens it again and c-o closes the browser.
    modal = ModalHarness(["/", *"command-4", "enter", "enter", "escape", "enter", "c-o"], consumed=True)
    command_loop.presentation.tui = modal
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    listings = [frame for frame in frames if "Tool output" in frame]
    assert modal.exclusive == [False, True, False, True]
    assert "command-4" in listings[0] and "command-3" in listings[0]  # the full list first
    # The reopened list is still filtered to the one matching entry, not the full list again.
    assert "command-4" in listings[-1]
    assert "command-3" not in listings[-1] and "command-0" not in listings[-1]
    assert sum("read-only" in frame for frame in frames) == 4


async def test_tool_output_list_keeps_a_no_matches_row_among_the_rows(tmp_path, monkeypatch):
    """A no-match search keeps the query and an empty-result row above the closing legend."""
    command_loop = loop(tmp_path)
    for index in range(3):
        command_loop.session.store_tool_result(
            "Bash",
            [f"printf command-{index}"],
            Tool.process_result("BashToolResult", 0, f"output {index}", ""),
        )
    modal = ModalHarness(["/", "z", "q"], consumed=True)
    command_loop.presentation.tui = modal
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((50, 26)))
        await tool_output_viewer(command_loop)

    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    listing = [frame for frame in frames if "no matches" in frame][-1]
    rows = listing.rstrip("\n").splitlines()
    assert rows[-1].strip().startswith("j/k/Tab move")  # the legend still closes the sheet
    assert rows[-2] == "/z"
    assert rows.index("  no matches") < len(rows) - 1  # the row sits with the rows, not under the legend


async def test_tool_output_viewer_folds_a_multiline_command_into_one_row(tmp_path, monkeypatch):
    """A row is one row. `short_call` keeps a multi-line command whole for the transcript, and a
    `git commit -m` with a real message would otherwise spill its row over several lines, carrying
    the numbering and the selection bar with it."""
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result(
        "Bash",
        ['git commit -m "fix the parser\n\nIt dropped the last token."'],
        Tool.process_result("BashToolResult", 0, "1 file changed", ""),
    )
    modal = ModalHarness([])
    command_loop.presentation.tui = modal

    # ``shutil`` is a shared module object also used by pytest's terminal reporter. Restore the
    # patch before pytest reports this test result, rather than waiting for fixture teardown.
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((120, 26)))
        await tool_output_viewer(command_loop)

    listing = "".join(value for _, value in modal.frames[0])
    rows = [row for row in listing.splitlines() if "tr.1" in row]
    assert len(rows) == 1
    assert "fix the parser It dropped the last token." in rows[0]

    # The viewer still opens the command exactly as it was run, newlines and all.
    view = modals_mod.record_view(command_loop, command_loop.session.tool_records[0])
    assert view is not None and view.text.count("\n") == 2




async def test_tool_output_viewer_shows_the_whole_output_not_the_transcript_preview(tmp_path):
    """The transcript keeps three lines. The point of opening an entry is the other thirty-seven."""
    command_loop = loop(tmp_path)
    stdout = "\n".join(f"line {line}" for line in range(40))
    command_loop.session.store_tool_result("Bash", ["seq 40"], Tool.process_result("BashToolResult", 0, stdout, ""))
    modal = ModalHarness(["enter", "G"])
    command_loop.presentation.tui = modal

    await tool_output_viewer(command_loop)

    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    viewer = [frame for frame in frames if "read-only" in frame]
    assert "line 0" in viewer[0]
    assert "line 39" in viewer[-1]  # reachable by scrolling, not elided
    assert "lines omitted" not in "".join(viewer)


async def test_tool_output_viewer_bounds_a_huge_result_and_says_so(tmp_path):
    """Stored output has no cap and the wrapper is quadratic in one line's length, so the viewer
    bounds what it renders -- and says how much it is showing, because a reader who cannot tell an
    elided result from a complete one has to distrust every result."""
    command_loop = loop(tmp_path)
    stdout = "\n".join(f"line {line}" for line in range(tooloutput.VIEWER_LINES * 2))
    command_loop.session.store_tool_result("Bash", ["seq huge"], Tool.process_result("BashToolResult", 0, stdout, ""))
    command_loop.session.store_tool_result("Bash", ["one long line"], Tool.process_result("BashToolResult", 0, "x" * (tooloutput.VIEWER_LINE_CHARS * 3), ""))
    modal = ModalHarness(["enter"])
    command_loop.presentation.tui = modal

    await tool_output_viewer(command_loop)

    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    viewer = next(frame for frame in frames if "read-only" in frame)
    # The newest entry is the one long line; the header says the clip happened rather than
    # presenting a truncated line as the whole of it.
    assert f"long lines clipped at {tooloutput.VIEWER_LINE_CHARS}" in viewer
    rendered = [row for row in viewer.splitlines() if row.lstrip(" │").startswith("x")]
    assert rendered and all(len(row) <= tooloutput.VIEWER_LINE_CHARS + 10 for row in rendered)


async def test_tool_output_viewer_bounds_a_result_with_too_many_lines(tmp_path):
    """The line bound counts against the streams, not the stored envelope, so the note it prints
    is a fact about the output rather than about the tags wrapped around it."""
    command_loop = loop(tmp_path)
    stdout = "\n".join(f"line {line}" for line in range(tooloutput.VIEWER_LINES * 2))
    command_loop.session.store_tool_result("Bash", ["seq huge"], Tool.process_result("BashToolResult", 0, stdout, ""))
    modal = ModalHarness(["enter"])
    command_loop.presentation.tui = modal

    await tool_output_viewer(command_loop)

    viewer = next(frame for frame in ("".join(v for _, v in f) for f in modal.frames) if "read-only" in frame)
    assert f"{tooloutput.VIEWER_LINES} shown of {tooloutput.VIEWER_LINES * 2}" in viewer
    # The elision is marked in the text too, where it happens -- the header note is derived from it.
    bounded, note = tooloutput.bash_viewer_output(command_loop.session.tool_records[-1].output)
    assert "lines omitted" in bounded and note.startswith(f"{tooloutput.VIEWER_LINES} shown of ")


async def test_tool_output_viewer_is_noop_without_stored_bash_output(tmp_path):
    command_loop = loop(tmp_path)
    modal = ModalHarness([])
    command_loop.presentation.tui = modal

    await tool_output_viewer(command_loop)

    assert modal.frames == []


async def test_tool_output_viewer_offers_the_script_that_is_still_running(tmp_path):
    """A long batch is exactly when the reader wants to look; the record only arrives at the end."""
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result("Bash", ["printf done"], Tool.process_result("BashToolResult", 0, "done", ""))
    command_loop.presentation.toolscript_run_status(True, 'for key in KEYS:\n    call("server.tool", {"key": key})\n')
    modal = ModalHarness(["enter"])
    command_loop.presentation.tui = modal

    await tool_output_viewer(command_loop)

    listing = "".join(value for _, value in modal.frames[0])
    assert "ToolScript  call 2 lines" in listing  # first row, above the stored Bash entry
    assert listing.index("running") < listing.index("Bash")
    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    viewer = [frame for frame in frames if "read-only" in frame]
    assert "Script · running · read-only" in viewer[0]
    assert 'call("server.tool"' in viewer[0]

    # It leaves with the script: once the batch returns, only the stored record remains.
    command_loop.presentation.tui = None
    command_loop.presentation.toolscript_run_status(False)
    modal = ModalHarness([])
    command_loop.presentation.tui = modal
    await tool_output_viewer(command_loop)
    assert "running" not in "".join(value for _, value in modal.frames[0])


async def test_tool_output_list_rows_are_coloured_by_part(tmp_path):
    """All-grey rows read as a wall; the key, the tool name, and the arguments each get their own."""
    command_loop = loop(tmp_path)
    for index in range(2):
        command_loop.session.store_tool_result("Bash", [f"printf hi-{index}"], Tool.process_result("BashToolResult", 0, "hi", ""))
    modal = ModalHarness([])
    command_loop.presentation.tui = modal

    await tool_output_viewer(command_loop)

    row = [(style, text) for style, text in modal.frames[0] if text.strip()]
    assert ("class:choice.meta", "tr.1  ") in row
    assert ("class:choice.tool", "Bash  ") in row  # padded to the widest tool name
    assert ("", "printf hi-0") in row
    # The selected row is left as one reverse bar rather than repainted part by part.
    assert not [style for style, _ in row if "choice.selected" in style and ("meta" in style or "tool" in style)]


async def test_tool_output_viewer_reads_resumed_history(tmp_path):
    saved = session(tmp_path)
    saved.store_tool_result("Bash", ["printf persisted"], Tool.process_result("BashToolResult", 0, "persisted output", ""))
    await saved.save_snapshot()
    saved.close()  # release the writer before reloading
    restored = load_session(saved.uid, config=saved.config)
    command_loop = CommandLoop(Agent(restored, output_fn=lambda _text: None), input_fn=lambda prompt="": "", output_fn=lambda _text: None)
    modal = ModalHarness(["enter", "c-o"], consumed=True)
    command_loop.presentation.tui = modal

    await tool_output_viewer(command_loop)

    detail = next(frame for frame in ("".join(value for _, value in f) for f in modal.frames) if "read-only" in frame)
    assert "printf persisted" in detail
    assert "persisted output" in detail


def _finished_job(tmp_path, job_id: str, command: str, log_text: str) -> "BackgroundJob":
    """A done BackgroundJob whose log file is on disk, the state a real ``Job(start)`` leaves
    behind after the process exits."""
    import subprocess

    log_path = tmp_path / f"{job_id}.log"
    log_path.write_text(log_text)
    proc = subprocess.Popen(["true"])
    proc.wait()
    return BackgroundJob(id=job_id, command=command, process=proc, log_path=str(log_path), started_at=0.0, status="done", exit_code=0)


def test_tool_output_browser_shows_the_job_log_for_a_known_job(tmp_path):
    """A Job status/wait record opens the job's full log while the job still exists in the
    session, so the browser is where a backgrounded build's real output is read."""
    command_loop = loop(tmp_path)
    command_loop.session.jobs["job.3"] = _finished_job(tmp_path, "job.3", "make build", "compiling...\nbuild ok\n")
    command_loop.session.store_tool_result("Job", [{"action": "wait", "job": "job.3"}], "Job: job.3\nStatus: done\n--- output ---\nbuild ok")

    view = job_view(command_loop, command_loop.session.tool_records[-1])

    assert view is not None
    assert view.label == "job · tr.1"
    assert "compiling..." in view.text and "build ok" in view.text
    assert ("job", "job.3") in view.rows
    assert ("status", "done") in view.rows
    assert ("exit", "0") in view.rows
    assert ("command", "make build") in view.rows
    assert "Status: done" in view.result


def test_tool_output_browser_job_view_falls_back_to_the_return_value_when_the_job_is_gone(tmp_path):
    """After a resume the session's job table is gone and a kill deletes the log file; those
    records show the tool's return value instead of pretending there is a log."""
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result("Job", [{"action": "wait", "job": "job.9"}], "Job: job.9\nStatus: done")

    view = job_view(command_loop, command_loop.session.tool_records[-1])

    assert view is not None
    assert view.text == "Job: job.9\nStatus: done"
    assert view.result == ""


def test_tool_output_browser_job_start_record_links_to_the_job_it_started(tmp_path):
    """A ``Job(start)`` call names the job only in its return value; the view finds the id there
    and shows the log anyway."""
    command_loop = loop(tmp_path)
    command_loop.session.jobs["job.1"] = _finished_job(tmp_path, "job.1", "long task", "started output\n")
    command_loop.session.store_tool_result("Job", [{"action": "start", "command": "long task"}], "Started job.1: long task")

    view = job_view(command_loop, command_loop.session.tool_records[-1])

    assert view is not None
    assert "started output" in view.text


def test_job_view_falls_back_when_a_known_jobs_log_was_removed(tmp_path):
    command_loop = loop(tmp_path)
    job = _finished_job(tmp_path, "job.1", "finished task", "finished output\n")
    command_loop.session.jobs[job.id] = job
    os.unlink(job.log_path)
    command_loop.session.store_tool_result("Job", [{"action": "kill", "job": job.id}], "Killed job.1 (status=done, exit_code=0)")

    view = job_view(command_loop, command_loop.session.tool_records[-1])

    assert view.text == "Killed job.1 (status=done, exit_code=0)"
    assert view.result == ""


async def test_promoted_bash_job_view_includes_output_from_before_and_after_promotion(tmp_path):
    command_loop = loop(tmp_path)
    command_loop.session.settings.bash_wait_timeout = 0.03

    promoted = await BashTool(command_loop.session, ["printf early; sleep 0.08; printf late"]).call()
    job = command_loop.session.jobs["job.1"]
    status = await JobTool(command_loop.session, [{"action": "wait", "job": job.id, "timeout": 2}]).call()
    # The process can exit just before its drainer consumes EOF. Wait for that observable state
    # instead of relying on one scheduler-sized sleep.
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        snapshot = job.log_snapshot(BackgroundJob.BUFFER_LIMIT)
        if snapshot is not None and "late" in snapshot[0]:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("background drainer did not capture the completed output")
    command_loop.session.store_tool_result("Job", [{"action": "wait", "job": job.id}], status)

    view = job_view(command_loop, command_loop.session.tool_records[-1])

    assert "early" in promoted
    assert "early" in view.text
    assert "late" in view.text


def test_disk_job_log_snapshot_reads_both_ends_with_a_fixed_bound(tmp_path):
    job = _finished_job(tmp_path, "job.1", "large task", "0123456789")

    snapshot = job.log_snapshot(6)

    assert snapshot is not None
    text, bounded = snapshot
    assert text.startswith("012") and text.endswith("789")
    assert "middle of job log omitted" in text
    assert bounded is True


async def test_tool_output_browser_defers_job_log_read_until_the_row_opens(tmp_path, monkeypatch):
    command_loop = loop(tmp_path)
    job = _finished_job(tmp_path, "job.1", "large task", "output")
    command_loop.session.jobs[job.id] = job
    command_loop.session.store_tool_result("Job", [{"action": "status", "job": job.id}], "Job: job.1\nStatus: done")
    reads = []
    monkeypatch.setattr(job, "log_snapshot", lambda _limit: reads.append(True) or ("output", False))
    command_loop.presentation.tui = ModalHarness(["q"])

    await tool_output_viewer(command_loop)

    assert reads == []


def test_job_write_browser_retains_exact_input_even_without_the_process(tmp_path):
    command_loop = loop(tmp_path)
    chars = 'answer\n\t"quoted"\x04'
    command_loop.session.store_tool_result("Job", [{"action": "write", "job": "job.9", "chars": chars}], "Wrote input")
    view = job_view(command_loop, command_loop.session.tool_records[-1])
    assert json.loads(view.text) == chars
    assert view.lexer == "json" and ("job", "job.9") in view.rows
    assert view.result == "Wrote input"


def test_bash_browser_keeps_workdir_even_without_output(tmp_path):
    command_loop = loop(tmp_path)
    command_loop.session.store_tool_result("Bash", ["true", " sub "], Tool.process_result("BashToolResult", 0, "", ""))
    view = modals_mod.bash_view(command_loop, command_loop.session.tool_records[-1])
    assert view is not None and view.text == "true"
    assert ("workdir", '" sub "') in view.rows
