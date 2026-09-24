"""The compacted-history export: `history.md` and `history.N.md`, written at compaction time.

These files are the model's copy of what compaction evicted, so the shapes asserted here are what
grep and Read will meet in them: the row prefixes, the wrap, the two length limits, and the words
that say what each limit left out.
"""

import json
import os

from agent_harness import session, session_with_provider
from test_session_persistence import session_with_data_dir

from wizolt import history
from wizolt.base import SESSION_EVENT_KEY, Json
from wizolt.context import ContextManager
from wizolt.history import SUMMARY_CHARS, WRAP_CHARS
from wizolt.image import IMAGE_REFS_KEY, ImageRef
from wizolt.prompts import COMPACTION_SUMMARY_TITLE
from wizolt.session import HistorySegment


def read(assets_dir: str, name: str) -> str:
    with open(os.path.join(assets_dir, name), encoding="utf-8") as file:
        return file.read()


def checkpoint(messages: list[Json]) -> str:
    return next(str(message["content"]) for message in messages if str(message.get("content") or "").startswith(COMPACTION_SUMMARY_TITLE))


def test_segment_file_lays_out_the_header_summary_and_rows(tmp_path):
    segment = HistorySegment(key="seg.3", title="Parser refactor", created_at="2026-08-13T13:12:00+08:00", messages=3)
    segment.summary = "what the span settled"
    compacted = [
        {"role": "user", "content": "first line\nsecond line"},
        {"role": "assistant", "content": "answer"},
    ]

    rows = history.SegmentDocument(segment, compacted).render().splitlines()

    assert rows[0] == "# history.3 · 2026-08-13T13:12:00+08:00 · Parser refactor"
    assert rows[1] == f"Rows: u=user, a=assistant, t=tool call label (arguments and output not included). Long rows wrap at {WRAP_CHARS} chars; every row of a message keeps its prefix."
    assert "## Summary at compaction" in rows
    assert "what the span settled" in rows  # the full summary, verbatim
    assert "## Conversation" in rows
    assert "u1 | first line" in rows
    assert "u1 | second line" in rows  # both rows of one message share its index
    assert "a2 | answer" in rows


def test_long_rows_wrap_with_the_same_prefix_and_lose_nothing():
    line = "x" * (WRAP_CHARS * 2 + 5)

    text = history.SegmentDocument(HistorySegment(key="seg.1", title="t"), [{"role": "user", "content": line}]).render()
    rows = [row for row in text.splitlines() if row.startswith("u1 | ")]

    assert [len(row) - len("u1 | ") for row in rows] == [WRAP_CHARS, WRAP_CHARS, 5]
    assert "".join(row[len("u1 | ") :] for row in rows) == line  # wrapped, never shortened


def test_tool_rows_keep_the_label_line_only():
    compacted = [{"role": "tool", "content": "tool tr.7 Bash rg -n TODO wizolt\noutput:\nlots of matches\n"}]

    rows = [row for row in history.SegmentDocument(HistorySegment(key="seg.1", title="t"), compacted).render().splitlines() if row.startswith("t1 | ")]

    assert rows == ["t1 | tool tr.7 Bash rg -n TODO wizolt"]  # no arguments, no output


def test_a_tool_row_says_how_the_call_ended():
    compacted = [
        {"role": "tool", "content": "tool tr.7 Bash bad\nstatus: failed\noutput:\nboom\n"},
        {"role": "tool", "content": "tool - Read secret.txt\nstatus: rejected\noutput:\nuser refused\n"},
    ]

    text = history.SegmentDocument(HistorySegment(key="seg.1", title="t"), compacted).render()

    assert "t1 | tool tr.7 Bash bad failed" in text.splitlines()
    assert "t2 | tool - Read secret.txt rejected" in text.splitlines()


def test_checkpoints_and_session_events_are_not_rows():
    compacted = [
        {"role": "user", "content": COMPACTION_SUMMARY_TITLE + "\nold summary"},
        {"role": "user", "content": "a protocol event", SESSION_EVENT_KEY: "compaction_request"},
        {"role": "assistant", "content": ""},  # a tool-calling message with no text of its own
        {"role": "user", "content": "the real request"},
    ]

    text = history.SegmentDocument(HistorySegment(key="seg.1", title="t"), compacted).render()
    rows = [row for row in text.splitlines() if row[:1] in ("u", "a", "t")]

    assert rows == ["u1 | the real request"]  # nothing to show consumes no index
    assert "old summary" not in text


def test_images_are_reduced_to_their_text_label(tmp_path):
    image = ImageRef(ref="a" * 64, name="shot.png", media_type="image/png", width=10, height=10, size=1).to_json()
    compacted = [{"role": "user", "content": "look here", IMAGE_REFS_KEY: [image]}]

    rows = [row for row in history.SegmentDocument(HistorySegment(key="seg.1", title="t"), compacted).render().splitlines() if row.startswith("u1 | ")]

    assert rows == ["u1 | [Image #1 · shot.png] look here"]


def test_the_index_appends_one_entry_per_compaction(tmp_path):
    s = session(tmp_path)
    context = ContextManager(s)
    assets = s.images.assets_dir()

    context.apply_compaction({"summary": "first summary"}, [], compacted=[{"role": "user", "content": "first task"}])
    first = read(assets, "history.md")
    context.apply_compaction({"summary": "second summary"}, [], compacted=[{"role": "user", "content": "second task"}])
    text = read(assets, "history.md")

    assert text.startswith(first)  # append-only: the first entry is still there, byte for byte
    assert "history.1.md | first summary" in text
    assert "history.2.md | second summary" in text
    assert "msgs · first task" in text
    assert os.path.isfile(os.path.join(assets, "history.1.md"))
    assert os.path.isfile(os.path.join(assets, "history.2.md"))


def test_the_index_collapses_whitespace_and_names_the_full_summary(tmp_path):
    s = session(tmp_path)
    long_summary = "first line\n\nsecond line   " + "x" * 600

    ContextManager(s).apply_compaction({"summary": long_summary}, [], compacted=[{"role": "user", "content": "task"}])

    entry = next(line for line in read(s.images.assets_dir(), "history.md").splitlines() if line.startswith("history.1.md |"))
    collapsed = " ".join(long_summary.split())

    assert entry == f"history.1.md | {collapsed[:SUMMARY_CHARS]}… [+{len(collapsed) - SUMMARY_CHARS} chars; full summary in history.1.md]"
    # The whole summary is in the segment file, which is what the index line points at.
    assert collapsed in " ".join(read(s.images.assets_dir(), "history.1.md").split())


def test_a_short_summary_is_stored_whole(tmp_path):
    s = session(tmp_path)

    ContextManager(s).apply_compaction({"summary": "short and complete"}, [], compacted=[{"role": "user", "content": "task"}])

    assert "history.1.md | short and complete" in read(s.images.assets_dir(), "history.md")


def test_a_resumed_segments_excerpt_is_exported_by_the_next_compaction(tmp_path):
    """A session resumed from an older version has segments whose text is already a truncated
    excerpt, with no message structure left to label. It is exported as an excerpt, at the first
    compaction after the resume -- never during the resume itself, which would inject files into a
    conversation that never asked for them."""
    s = session(tmp_path)
    old = HistorySegment(
        key="seg.1",
        title="old span",
        created_at="2026-08-01T09:00:00+08:00",
        text=(
            "user:\nold work\n\n"
            '<bounded_output omitted="middle" max_tokens="6000" estimated_tokens="9000" omitted_tokens="9192" recall="seg.1"/>'
            "\nuser:\nlate work"
        ),
        summary="what it settled",
    )
    s.history.append(old)
    assets = s.images.assets_dir()

    assert not os.path.exists(os.path.join(assets, "history.1.md"))  # a resume exports nothing

    ContextManager(s).apply_compaction({"summary": "new work"}, [], compacted=[{"role": "user", "content": "new task"}])

    rows = read(assets, "history.1.md").splitlines()
    assert rows[2] == "Excerpt: middle omitted at compaction (~9192 tokens); original text only in the session log."
    assert "## Summary at compaction" in rows and "what it settled" in rows
    assert "## Excerpt" in rows
    assert "old work" in rows and "late work" in rows
    assert not any(row.startswith(("u1 | ", "a1 | ")) for row in rows)  # no message structure to label
    assert any(row.startswith('<bounded_output omitted="middle"') for row in rows)  # the old marker survives


async def test_the_checkpoint_omits_the_path_when_the_export_cannot_be_written(tmp_path, monkeypatch):
    s = session(tmp_path)
    (tmp_path / "blocked.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(s.images, "assets_dir", lambda: str(tmp_path / "blocked.txt" / "sub"))
    context = ContextManager(s)

    context.apply_compaction({"summary": "s"}, [], compacted=[{"role": "user", "content": "task"}])

    # The span is retained either way; only the promise of a file is dropped.
    assert [segment.key for segment in s.history] == ["seg.1"]
    assert "Compacted history:" not in checkpoint(s.messages)


async def test_a_dropped_segments_export_is_collected_while_the_index_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(ContextManager, "MAX_HISTORY_SEGMENTS", 1)
    s = session_with_data_dir(tmp_path)
    context = ContextManager(s)
    context.apply_compaction({"summary": "one"}, [], compacted=[{"role": "user", "content": "first"}])
    context.apply_compaction({"summary": "two"}, [], compacted=[{"role": "user", "content": "second"}])
    assets = s.images.assets_dir()
    assert [segment.key for segment in s.history] == ["seg.2"]
    assert os.path.isfile(os.path.join(assets, "history.1.md"))

    await s.save_snapshot()

    assert not os.path.exists(os.path.join(assets, "history.1.md"))  # its segment left the window
    assert os.path.isfile(os.path.join(assets, "history.2.md"))  # this one is still referenced
    index = read(assets, "history.md")
    assert "history.1.md | one" in index and "history.2.md | two" in index  # the index is append-only


class _RecordingModel:
    """A model stub that records what each request carried, and answers with a summary."""

    last_compaction_model = ""

    def __init__(self, session):
        self.session = session
        self.sent: list[list[Json]] = []

    async def api_request(self, messages, _tools, **_kwargs):
        self.sent.append(messages)
        return "", "", json.dumps({"summary": "compacted"})

    @staticmethod
    def parse_json_object(content):
        return json.loads(content)


def over_budget(tmp_path):
    s = session_with_provider(tmp_path)
    s.settings.max_context_tokens = 1
    s.messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest request"},
    ]
    return s


async def test_the_compaction_request_is_a_prefix_of_the_live_projection(tmp_path):
    """The summary request is the turn's own request truncated, plus the instruction appended --
    message for message, byte for byte, ending before the latest user message that `keep` decides.
    That is what makes it a cache hit, and it is why every export happens after it has returned."""
    s = over_budget(tmp_path)
    context = ContextManager(s)
    turn = [{"role": "user", "content": "current request"}]
    live = context.model_messages(s.system_prompt, turn)

    model = _RecordingModel(s)
    await context.prepare_messages(model, s.system_prompt, turn)

    sent = model.sent[0]
    assert sent[:-1] == live[: len(sent) - 1]
    assert len(sent) - 1 < len(live)  # truncated, never a request the turn did not make
    assert sent[-1]["role"] == "user" and sent[-1] not in live
    # The export ran in the same call, after the request had already gone out.
    assert os.path.isfile(os.path.join(s.images.assets_dir(), "history.1.md"))


async def test_the_checkpoint_is_written_once_and_then_only_read(tmp_path):
    s = over_budget(tmp_path)
    context = ContextManager(s)
    await context.prepare_messages(_RecordingModel(s), "system", [{"role": "user", "content": "current request"}])

    settled = context.model_messages("system", [])
    later = context.model_messages("system", [{"role": "user", "content": "next request"}])

    # Projected twice, then with a later turn appended: the checkpoint bytes never move, so every
    # request after the rebuild reads that prefix from the cache.
    assert context.model_messages("system", []) == settled
    assert later[: len(settled)] == settled
    assert "Compacted history:" in checkpoint(later)
    assert os.path.isfile(os.path.join(s.images.assets_dir(), "history.1.md"))


async def test_a_truncated_marker_is_projected_unchanged(tmp_path):
    s = session(tmp_path)
    context = ContextManager(s)
    large = "head\n" + "\n".join(f"line {index}" for index in range(20000)) + "\ntail\n"
    marker = context.bound_output(large, path=await context.materialize_output("tr.1", large))
    s.messages.append({"role": "tool", "content": "tool tr.1 Bash cat big\noutput:\n" + marker, "tool_call_id": "c1"})

    def tool_message(messages):
        return next(str(message.get("content")) for message in messages if str(message.get("content") or "").startswith("tool tr.1"))

    first = tool_message(context.model_messages("system", []))
    second = tool_message(context.model_messages("system", [{"role": "user", "content": "next"}]))

    # The marker was written once, when the output was truncated; projection copies it verbatim.
    assert first == second
    assert f'file="{os.path.join(s.images.assets_dir(), "tr.1.txt")}"' in first
    assert "recall=" not in first and "hint=" not in first
