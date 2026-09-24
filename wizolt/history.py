"""Compacted-history export: the greppable copy of the conversation compaction evicted.

The append-only jsonl log stays the source of truth; these files are a derived copy written once,
at compaction time, into the assets directory beside `tr.N.txt`. `history.md` is the append-only
index, one entry per compaction; `history.N.md` (segment `seg.N`) is that span's conversation as
plain text, so grep and Read can search it. Nothing here runs during request projection: building
request text from state that changes between turns would break prompt-cache reuse.

Two objects, one job each: a `SegmentDocument` renders one file's text from immutable inputs, and
a `HistoryArchive` owns a session's assets directory and the write lifecycle. Writes swallow
`OSError` the way `ContextManager._materialize_output_sync` does, and report whether they landed,
so a read-only or full disk costs a file and never a turn.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from wizolt.base import SESSION_EVENT_KEY, Json
from wizolt.image import ImageInputs
from wizolt.prompts import COMPACTION_SUMMARY_TITLE
from wizolt.session import HistorySegment

HISTORY_INDEX = "history.md"
# One grep hit must not dump a whole span into the context window, and one summary must not bloat
# the index it sits in. Both limits say in the file what they left out.
WRAP_CHARS = 400
SUMMARY_CHARS = 500
_SEGMENTS = re.compile(r"seg\.(\d+)")
_OMITTED_TOKENS = re.compile(r'<bounded_output\b[^>]*\bomitted_tokens="(\d+)"')
_ROWS_NOTE = f"Rows: u=user, a=assistant, t=tool call label (arguments and output not included). Long rows wrap at {WRAP_CHARS} chars; every row of a message keeps its prefix."
_EXCERPT_NOTE = "Excerpt: middle omitted at compaction{count}; original text only in the session log."


def segment_filename(key: str) -> str:
    """`history.N.md` for a `seg.N` key, or "" for a key that does not parse."""
    match = _SEGMENTS.fullmatch(key)
    return f"history.{match.group(1)}.md" if match else ""


class SegmentDocument:
    """One `history.N.md`: its header, its summary, and one row per line of conversation.

    Built with the messages this compaction evicted, it labels and numbers every row. Built with
    `excerpt`, the segment is an older session's, whose only surviving text is the bounded excerpt
    compaction kept -- a flat `role:` + content concatenation with no message structure left to
    label -- so the body is that text wrapped as-is, the embedded markers showing where the
    middle was dropped.
    """

    def __init__(self, segment: HistorySegment, conversation: Sequence[Json]):
        self._segment = segment
        self._conversation = conversation

    @classmethod
    def excerpt(cls, segment: HistorySegment) -> SegmentDocument:
        """An older session's segment: only its bounded excerpt exists, so that is the body."""
        return cls(segment, ())

    def render(self) -> str:
        rows = [self._title_row(), _ROWS_NOTE]
        if self._is_excerpt:
            rows.append(self._excerpt_note())
        rows.extend(("", *self._summary_rows(), self._body_title()))
        rows.extend(self._excerpt_rows() if self._is_excerpt else self._conversation_rows())
        return "\n".join(rows) + "\n"

    @property
    def _is_excerpt(self) -> bool:
        return not self._conversation

    def _title_row(self) -> str:
        return f"# history.{self._number()} · {self._segment.created_at} · {self._segment.title}"

    def _body_title(self) -> str:
        return "## Excerpt" if self._is_excerpt else "## Conversation"

    def _number(self) -> str:
        match = _SEGMENTS.fullmatch(self._segment.key)
        return match.group(1) if match else self._segment.key

    def _excerpt_note(self) -> str:
        match = _OMITTED_TOKENS.search(self._segment.text)
        return _EXCERPT_NOTE.format(count=f" (~{match.group(1)} tokens)" if match else "")

    def _summary_rows(self) -> list[str]:
        """The summary verbatim, never wrapped: it is one block of prose, not a searchable transcript."""
        return ["## Summary at compaction", self._segment.summary, ""] if self._segment.summary else []

    def _excerpt_rows(self) -> list[str]:
        return [row for line in self._segment.text.splitlines() for row in self._wrap_rows(line)]

    def _conversation_rows(self) -> list[str]:
        """Every message's rows, numbered from 1 in the order they were compacted.

        A message that renders nothing does not consume a number: the numbers exist to grep one
        message back, and a number nothing carries could not be grepped."""
        rows: list[str] = []
        index = 0
        for message in self._conversation:
            rendered = self._message_rows(message, index + 1)
            if rendered:
                index += 1
                rows.extend(rendered)
        return rows

    def _message_rows(self, message: Json, index: int) -> list[str]:
        """One message's rows under its shared index, or [] when it carries nothing to show.

        A tool result contributes its label line only: its arguments can be re-derived and its
        output would drown the search, which is the whole point of the file."""
        role = message.get("role")
        if role == "tool":
            head, _, rest = str(message.get("content") or "").partition("\n")
            if not head.strip():
                return []
            status = next((line.strip().removeprefix("status: ") for line in rest.splitlines() if line.strip().startswith("status: ")), "")
            return self._rows(f"t{index} | ", head.strip() + (f" {status}" if status else ""))
        if role not in ("user", "assistant") or message.get(SESSION_EVENT_KEY) or ImageInputs.is_tool_observation(message):
            return []
        text = ImageInputs.label_text(message)
        if role == "user" and text.startswith(COMPACTION_SUMMARY_TITLE):
            return []
        marker = "u" if role == "user" else "a"
        return [row for line in text.splitlines() for row in self._rows(f"{marker}{index} | ", line)]

    def _rows(self, prefix: str, text: str) -> list[str]:
        return [prefix + row for row in self._wrap_rows(text)]

    @staticmethod
    def _wrap_rows(text: str) -> list[str]:
        """`text` as rows of at most WRAP_CHARS, never inside a character.

        Python strings are code points, so a slice cannot split one. Nothing is dropped and no
        ellipsis is added: the shared row prefix is what tells the reader that the message continues."""
        return [text[at : at + WRAP_CHARS] for at in range(0, len(text), WRAP_CHARS)] or [""]


class HistoryArchive:
    """One session's exported compaction history in its assets directory.

    The archive owns the write lifecycle so its callers do not: an older segment with no file yet
    is backfilled before the new one is written, the index entry is appended only after the file
    it names has landed, and every write answers for itself -- an `OSError` costs that one file
    and never the turn. `export` reports whether the index gained this segment's entry, which is
    exactly the promise the compaction checkpoint makes about these files.
    """

    def __init__(self, assets_dir: str):
        self._dir = assets_dir

    @property
    def index_path(self) -> str:
        """The append-only index, as the checkpoint line names it: a pure path from session identity."""
        return os.path.join(self._dir, HISTORY_INDEX)

    def export(self, segment: HistorySegment, compacted: Sequence[Json], retained: Sequence[HistorySegment]) -> bool:
        """Write this compaction's file and index entry, backfilling older segments that have none.

        Each older segment is independent -- a failure there costs that one export and nothing
        else -- and the entry is appended only after its own file landed, so the index never names
        a file that is not there."""
        for older in retained:
            if older.key != segment.key:
                self._backfill(older)
        name = segment_filename(segment.key)
        if not name or not self._write(name, SegmentDocument(segment, compacted).render()):
            return False
        return self._write(HISTORY_INDEX, self._index_entry(segment), append=True)

    def _backfill(self, segment: HistorySegment) -> None:
        name = segment_filename(segment.key)
        if name and not os.path.exists(os.path.join(self._dir, name)):
            self._write(name, SegmentDocument.excerpt(segment).render())

    def _index_entry(self, segment: HistorySegment) -> str:
        """The two lines `history.md` gains for one segment: what it is, and what it settled."""
        name = segment_filename(segment.key) or f"history.{segment.key}.md"
        summary = " ".join((segment.summary or "").split())
        if len(summary) > SUMMARY_CHARS:
            summary = summary[:SUMMARY_CHARS] + f"… [+{len(summary) - SUMMARY_CHARS} chars; full summary in {name}]"
        return f"{name} · {segment.created_at} · {segment.messages} msgs · {segment.title}\n{name} | {summary}".rstrip() + "\n\n"

    def _write(self, name: str, text: str, *, append: bool = False) -> bool:
        try:
            os.makedirs(self._dir, exist_ok=True)
            with open(os.path.join(self._dir, name), "a" if append else "w", encoding="utf-8") as file:
                file.write(text)
        except OSError:
            return False
        return True
