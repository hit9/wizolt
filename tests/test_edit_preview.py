"""edit preview (split from tests/test_edit_tool.py)."""

from prompt_toolkit.utils import get_cwidth
from test_edit_tool import session, view

from wizolt.base import LogBlock, LogEdge, LogLine, LogRole, ToolCall
from wizolt.context import ContextManager
from wizolt.render import Theme, UiPrinter
from wizolt.runner import ToolRunner
from wizolt.tools import CodeIndex


async def ignore_index_update(_index, _paths):
    return ""


def test_approval_segments_highlight_inline_edit_preview():
    preview = "--- foo.py\n+++ foo.py\n@@ -1,2 +1,2 @@\n def hello():\n-    pass\n+    return 42"
    block = LogBlock.hierarchy(
        LogLine("Edit", "foo.py", LogRole.TOOL),
        [
            LogLine("preview", role=LogRole.META, edge=LogEdge.BRANCH),
            *(LogLine("", line, LogRole.DIFF, LogEdge.CONTINUE) for line in preview.splitlines()),
        ],
    )
    segments = UiPrinter().log_segments(block)
    rendered = "".join(text for _, text in segments)

    assert (Theme.fg("tool"), "Edit") in segments
    assert any(style.endswith("bg:#003b00") and style != "bg:#003b00" and "return" in text for style, text in segments)
    assert any(style == "ansigreen bg:#003b00" and text == "+" for style, text in segments)
    assert any(style == "fg:default bg:#520000" and "pass" in text for style, text in segments)
    assert "\n\n" not in rendered


def test_diff_marks_the_words_a_modified_line_changed():
    """A removed line and the added line replacing it put the heavier band under the words that
    differ, and only there; the rest of each line keeps its ordinary band."""
    segments = UiPrinter().diff_segments("@@ -1 +1 @@\n-total = price * count\n+total = price * quantity")
    added_emph, removed_emph = Theme.diff_style("diff.added.emph"), Theme.diff_style("diff.removed.emph")

    assert [text for style, text in segments if style.endswith(removed_emph)] == ["count"]
    assert [text for style, text in segments if style.endswith(added_emph)] == ["quantity"]
    assert any("price" in text and style.endswith(Theme.diff_style("diff.added.bg")) for style, text in segments)


def test_diff_marks_nothing_when_the_runs_do_not_pair_up():
    """Three removed lines replaced by one: no line is paired with whatever sits at its offset."""
    segments = UiPrinter().diff_segments("@@ -1,3 +1 @@\n-## Unreleased\n-\n-### Added\n+## 0.49.6 - 2026-09-18")

    assert not any(style.endswith((Theme.diff_style("diff.added.emph"), Theme.diff_style("diff.removed.emph"))) for style, _ in segments)


def test_diff_reads_a_changed_line_starting_with_three_dashes_as_content():
    """Removing a markdown rule (`---`) makes the diff line `----`, which is not a file header:
    it keeps the removed band, and the rows under it keep counting from it."""
    diff = "--- a/r.md\n+++ b/r.md\n@@ -1,3 +1,3 @@\n # Title\n----\n+***\n after"
    ui = UiPrinter()
    rows = ui.segment_lines(ui.diff_segments(diff))
    text = ["".join(part for _, part in row) for row in rows]
    band = Theme.diff_style("diff.removed.bg")

    assert text[4].startswith("   2      │ ----")  # the removed rule, numbered on the old side
    assert ("ansired " + band, "-") in rows[4] and any(part == "---" and band in style for style, part in rows[4])
    assert text[6].startswith("   3    3 ")  # counted, so the context row under it is still line 3


def test_diff_leaves_a_rewritten_line_unmarked():
    """A pair that shares too little is a rewrite, not an edit: no words are singled out."""
    segments = UiPrinter().diff_segments("@@ -1 +1 @@\n-import os\n+return render(frame, width)")

    assert not any(style.endswith((Theme.diff_style("diff.added.emph"), Theme.diff_style("diff.removed.emph"))) for style, _ in segments)


async def test_auto_approved_edit_keeps_preview_pre_line(tmp_path, monkeypatch):
    # Edit's "auto …" pre-line carries the approval preview; the result line is tagged [auto].
    s = session(tmp_path)
    s.settings.yolo = True
    monkeypatch.setattr(CodeIndex, "update", ignore_index_update)
    (tmp_path / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    key = view(s, "a.txt")
    out = []
    runner = ToolRunner(s, ContextManager(s), output_fn=out.append)
    await runner.run([ToolCall("e0", "Edit", ["a.txt", key, [{"op": "replace", "start": 1, "end": 1, "content": "hello\nNEW\n"}]])])
    assert len(out) == 2
    assert isinstance(out[0], LogBlock)
    root, _ = next(out[0].walk())
    assert root.role is LogRole.AUTO
    assert "+NEW" in str(out[0])
    assert str(out[1]).rstrip().endswith("[auto]")


async def test_batch_edit_no_change_reports_no_change(tmp_path, monkeypatch):
    s = session(tmp_path)
    s.settings.yolo = True
    monkeypatch.setattr(CodeIndex, "update", ignore_index_update)
    path = tmp_path / "code.txt"
    path.write_text("a\nb\n", encoding="utf-8")
    key = view(s, "code.txt")
    runner = ToolRunner(s, ContextManager(s), output_fn=lambda text: None)

    await runner.run([ToolCall("noop", "Edit", ["code.txt", key, [{"op": "replace", "start": 2, "end": 2, "content": "b\n"}]])])

    assert s.tool_errors
    assert "edit produced no changes; requested content already matches target range" in s.tool_errors[0].error
    assert path.read_text(encoding="utf-8") == "a\nb\n"


async def test_batch_edit_stale_reports_source_target_changed(tmp_path, monkeypatch):
    s = session(tmp_path)
    s.settings.yolo = True
    monkeypatch.setattr(CodeIndex, "update", ignore_index_update)
    path = tmp_path / "code.txt"
    path.write_text("a\nb\n", encoding="utf-8")
    key = view(s, "code.txt")
    runner = ToolRunner(s, ContextManager(s), output_fn=lambda text: None)

    await runner.run([ToolCall("first", "Edit", ["code.txt", key, [{"op": "replace", "start": 2, "end": 2, "content": "B\n"}]])])
    await runner.run([ToolCall("bad", "Edit", ["code.txt", key, [{"op": "replace", "start": 2, "end": 2, "content": "x\n"}]])])

    assert s.tool_errors
    assert "source target changed" in s.tool_errors[0].error
    assert path.read_text(encoding="utf-8") == "a\nB\n"


async def test_code_index_updates_after_file_mutation_tools(tmp_path, monkeypatch):
    s = session(tmp_path)
    s.settings.yolo = True
    updated = []

    async def record_update(_index, paths):
        updated.extend(paths)
        return ""

    monkeypatch.setattr(CodeIndex, "update", record_update)
    runner = ToolRunner(
        s,
        ContextManager(s),
        input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
        output_fn=lambda text: None,
    )

    await runner.run([ToolCall("empty", "Edit", ["empty.py", "", [{"op": "create", "content": ""}]])])
    await runner.run([ToolCall("create", "Edit", ["made.py", "", [{"op": "create", "content": "print(1)\n"}]])])
    key = view(s, "made.py")
    await runner.run([ToolCall("edit", "Edit", ["made.py", key, [{"op": "replace", "start": 1, "end": 1, "content": "print(2)\n"}]])])

    assert (tmp_path / "made.py").read_text(encoding="utf-8") == "print(2)\n"
    assert updated == ["empty.py", "made.py", "made.py"]


def test_diff_segments_gracefully_degrades_without_header_path(tmp_path):
    ui = UiPrinter()
    # No +++ line, so pygments cannot pick a lexer.
    diff = "@@ -1,1 +1,1 @@\n- old\n+ new\n"
    segments = ui.diff_segments(diff)

    assert any(t == "-" and s == "ansired bg:#520000" for s, t in segments)
    assert any(t == "+" and s == "ansigreen bg:#003b00" for s, t in segments)


def test_diff_segments_gracefully_degrades_without_lexer(tmp_path):
    ui = UiPrinter()
    diff = "--- foo.unknownxyz\n+++ foo.unknownxyz\n@@ -1,1 +1,1 @@\n- old\n+ new\n"
    segments = ui.diff_segments(diff)

    assert any(t == "-" and s == "ansired bg:#520000" for s, t in segments)
    assert any("old" in t and s == "fg:default " + Theme.diff_style("diff.removed.emph") for s, t in segments)
    assert any(t == "+" and s == "ansigreen bg:#003b00" for s, t in segments)


def test_diff_segments_syntax_highlights_python(tmp_path):
    ui = UiPrinter()
    diff = "--- foo.py\n+++ foo.py\n@@ -1,2 +1,2 @@\n def hello():\n-    pass\n+    return 42\n"
    segments = ui.diff_segments(diff)

    assert any(t == "+" and s == "ansigreen bg:#003b00" for s, t in segments)
    # Highlighted by the theme's Pygments style, on the added band: the color is the style's, the
    # band is the diff's own pinned green.
    assert any(t == "return" and s.endswith("bg:#003b00") and s.startswith("fg:#") for s, t in segments)

    assert any(t == "-" and s == "ansired bg:#520000" for s, t in segments)
    assert any("pass" in t and s == "fg:default bg:#520000" for s, t in segments)

    # Changed-line gutters join the background band; context stays unfilled.
    assert any("│" in text and style == "ansibrightblack bg:#003b00" for style, text in segments)
    assert any("│" in text and style == "ansibrightblack bg:#520000" for style, text in segments)
    assert any("1" in text and "│" in text and "bg:" not in style for style, text in segments)
    assert any(text == "def" and "bg:" not in style for style, text in segments)

    live = ui.segment_lines(ui.diff_segments_live(diff, row_width=40))
    changed = [line for line in live if any("bg:" in style for style, _ in line)]
    widths = [sum(get_cwidth(text.rstrip("\n")) for _, text in line) for line in changed]
    assert set(widths) == {40}
