"""Edit success receipts: the marks that separate the changed runs from the context rows."""

from test_edit_tool import session, view

from wizolt.source import ToolOutput
from wizolt.tools import EditTool


def rendered(out, s):
    assert isinstance(out, ToolOutput)
    return out.render(s.register_source_drafts(list(out.drafts)))


def test_receipt_wraps_the_changed_run_and_leaves_context_bare(tmp_path):
    s = session(tmp_path)
    (tmp_path / "code.txt").write_text("".join(f"l{i}\n" for i in range(1, 13)), encoding="utf-8")
    key = view(s, "code.txt")

    out = EditTool(s, ["code.txt", key, [{"op": "replace", "start": 6, "end": 8, "content": "X1\nX2\nX3\n"}]]).call()
    text = rendered(out, s)

    lines = text.splitlines()
    source_at = next(i for i, line in enumerate(lines) if line.startswith("<source"))
    assert 'path="code.txt"' in lines[source_at] and 'lines="3:11"' in lines[source_at]
    open_at = lines.index('<edited lines="6:8">')
    close_at = lines.index("</edited>")
    assert lines[open_at + 1 : close_at] == [" 6 | X1", " 7 | X2", " 8 | X3"]
    # up to three context rows on either side, outside the pair, unmarked
    assert lines[open_at - 3 : open_at] == [" 3 | l3", " 4 | l4", " 5 | l5"]
    assert lines[close_at + 1 : close_at + 4] == [" 9 | l9", "10 | l10", "11 | l11"]


def test_receipt_marks_each_edit_of_one_call_separately(tmp_path):
    s = session(tmp_path)
    (tmp_path / "code.txt").write_text("".join(f"l{i}\n" for i in range(1, 13)), encoding="utf-8")
    key = view(s, "code.txt")

    out = EditTool(
        s,
        [
            "code.txt",
            key,
            [
                {"op": "replace", "start": 3, "end": 4, "content": "A\nB\n"},
                {"op": "replace", "start": 9, "end": 9, "content": "C\n"},
            ],
        ],
    ).call()
    text = rendered(out, s)

    lines = text.splitlines()
    first = lines.index('<edited lines="3:4">')
    second = lines.index('<edited lines="9:9">')
    assert first < second
    assert lines[first + 1 : first + 3] == [" 3 | A", " 4 | B"]
    assert lines[second + 1 : second + 2] == [" 9 | C"]
    # the rows between the two runs are context: bare, outside both pairs
    assert lines[first + 4 : second] == [f" {n} | l{n}" for n in range(5, 9)]


def test_receipt_names_a_deleted_hole_by_the_lines_it_removed(tmp_path):
    s = session(tmp_path)
    (tmp_path / "code.txt").write_text("".join(f"l{i}\n" for i in range(1, 11)), encoding="utf-8")
    key = view(s, "code.txt")

    out = EditTool(s, ["code.txt", key, [{"op": "delete", "start": 4, "end": 6}]]).call()
    text = rendered(out, s)

    lines = text.splitlines()
    hole_at = lines.index('<deleted lines="4:6"/>')
    assert lines[hole_at - 1] == "3 | l3"
    assert lines[hole_at + 1] == "4 | l7"
    assert "<edited" not in text


def test_receipt_keeps_both_halves_of_a_hole_two_adjacent_deletions_left(tmp_path):
    s = session(tmp_path)
    (tmp_path / "code.txt").write_text("".join(f"l{i}\n" for i in range(1, 11)), encoding="utf-8")
    key = view(s, "code.txt")

    out = EditTool(s, ["code.txt", key, [{"op": "delete", "start": 4, "end": 4}, {"op": "delete", "start": 5, "end": 6}]]).call()
    text = rendered(out, s)

    lines = text.splitlines()
    # both deletions leave one hole between l3 and l7, and it names every line they removed
    hole_at = lines.index('<deleted lines="4:6"/>')
    assert lines[hole_at - 1] == "3 | l3"
    assert lines[hole_at + 1] == "4 | l7"
    assert text.count("<deleted") == 1


def test_receipt_cuts_context_rows_but_never_changed_rows(tmp_path):
    s = session(tmp_path)
    long_context, long_change = "b" * 260, "Y" * 260
    (tmp_path / "wide.txt").write_text(f"a\n{long_context}\nc\n{'d' * 260}\ne\n", encoding="utf-8")
    key = view(s, "wide.txt")

    out = EditTool(s, ["wide.txt", key, [{"op": "replace", "start": 3, "end": 3, "content": long_change + "\n"}]]).call()
    text = rendered(out, s)

    lines = text.splitlines()
    assert f"2 | {'b' * 200}…" in lines  # context rows are orientation, cut at 200 columns
    assert f"3 | {long_change}" in lines  # changed rows are the evidence, never cut
    assert f"4 | {'d' * 200}…" in lines
    assert f"2 | {long_context}" not in lines


def test_receipt_marks_the_whole_created_file(tmp_path):
    s = session(tmp_path)

    out = EditTool(s, ["new.txt", "", [{"op": "create", "content": "one\ntwo\nthree\n"}]]).call()
    text = rendered(out, s)

    lines = text.splitlines()
    open_at = lines.index('<edited lines="1:3">')
    close_at = lines.index("</edited>")
    assert lines[open_at + 1 : close_at] == ["1 | one", "2 | two", "3 | three"]
