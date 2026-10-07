"""Detail documents keep their content and geometry across resizing and navigation."""

import os

import pytest
from prompt_toolkit.utils import get_cwidth

from wizolt.base import ApprovalView
from wizolt.ui.render import UiPrinter
from wizolt.ui.tui.details import DETAIL_BACK, DetailSheet


@pytest.mark.parametrize("lexer", ["bash", "text", "diff", ""])
def test_detail_sheet_wraps_every_section_and_reaches_the_end(monkeypatch, lexer):
    size = [80, 24]
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback: os.terminal_size(size))
    body = "\n".join(f"line {i} 中文 words" for i in range(40))
    if lexer == "diff":
        body = "--- x.py\n+++ x.py\n@@ -1 +1,40 @@\n-old\n" + "\n".join("+" + line for line in body.splitlines())
    sheet = DetailSheet(
        UiPrinter(),
        ApprovalView("document · " + "长路径/" * 30, body, lexer, [("key", "tr.1"), ("workdir", "/" + "目录/" * 30)], "<literal>\nTAIL-MARKER"),
        back_on_escape=True,
    )
    for width in (80, 30, 20, 120, 40):
        size[0] = width
        for key in ("g", "c-d", "G"):
            sheet.handle_key(key, "")
            text = "".join(fragment[1] for fragment in sheet.fragments())
            assert all(get_cwidth(line) <= width for line in text.splitlines()), (width, text)
        assert "TAIL-MARKER" in text
        assert "<literal>" in text
    assert sheet.handle_key("escape", "") is DETAIL_BACK
    assert sheet.handle_key("c-o", "") is None


def test_detail_sheet_parts_follow_the_text_under_gray_labelled_rules(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback: os.terminal_size((40, 40)))
    sheet = DetailSheet(UiPrinter(), ApprovalView("prompt", "BASE", "text", parts=(("added by runtime.reactions", "REACTIONS block"),)))

    rows = "".join(fragment[1] for fragment in sheet.fragments()).splitlines()
    rule = next(fragment for fragment in sheet.fragments() if "added by runtime.reactions" in fragment[1])

    # Inside the frame, after the text and before the part, drawn in the frame's own gray.
    assert "class:detail.border" in rule[0] and rule[1].startswith("── added by runtime.reactions ─")
    assert [index for index, row in enumerate(rows) if "BASE" in row] < [index for index, row in enumerate(rows) if "runtime.reactions" in row]
    at = rows.index(next(row for row in rows if "runtime.reactions" in row))
    # A blank frame row on each side parts the rule from the text above and the part below.
    assert rows[at - 1].strip(" │") == "" and rows[at + 1].strip(" │") == "" and "REACTIONS block" in rows[at + 2]
    assert all(get_cwidth(row) <= 40 for row in rows)


def test_detail_sheet_code_surface_does_not_color_plain_output(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback: os.terminal_size((80, 40)))
    sheet = DetailSheet(UiPrinter(), ApprovalView("command", "echo hello", "bash", result="OUTPUT"))
    fragments = sheet.fragments()
    assert any("detail.source" in style and "echo" in text for style, text in fragments)
    assert all("detail.source" not in style for style, text in fragments if "OUTPUT" in text)
    assert sum("╭" in text for _, text in fragments) == 2


def test_detail_sheet_end_anchor_survives_resize_and_can_be_left(monkeypatch):
    size = [100, 30]
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback: os.terminal_size(size))
    body = "\n".join(f"BODY-{i:03}" for i in range(80))
    sheet = DetailSheet(UiPrinter(), ApprovalView("output", body, "text", result="TAIL"))
    sheet.handle_key("G", "")
    for dimensions in ([100, 30], [62, 18], [80, 24], [100, 30]):
        size[:] = dimensions
        assert "TAIL" in "".join(text for _, text in sheet.fragments())
    sheet.handle_key("c-u", "")
    assert "TAIL" not in "".join(text for _, text in sheet.fragments())
    sheet.handle_key("g", "")
    assert "BODY-000" in "".join(text for _, text in sheet.fragments())


@pytest.mark.parametrize("back", [True, False])
def test_detail_sheet_legend_describes_its_navigation(monkeypatch, back):
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback: os.terminal_size((120, 30)))
    sheet = DetailSheet(UiPrinter(), ApprovalView("output", "text", "text"), back_on_escape=back)
    footer = "".join(fragment[1] for fragment in sheet.fragments()).splitlines()[-1]
    assert "Ctrl-D/U half-page" in footer
    assert ("Esc/q back · Ctrl-O close" if back else "Esc/q close") in footer
