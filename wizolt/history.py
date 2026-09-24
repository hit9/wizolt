"""Compacted-history export: the greppable copy of the conversation compaction evicted.

The append-only jsonl log stays the source of truth; these files are a derived copy written once,
at compaction time, into the assets directory beside `tr.N.txt`. `history.md` is the append-only
index, one entry per exported segment; `history.N.md` (segment `seg.N`) is that span's conversation
as plain text, so grep and Read can search it. Nothing here runs during request projection:
building request text from state that changes between turns would break prompt-cache reuse.

Two objects, one job each: a `SegmentDocument` renders one file's text from immutable inputs, and
a `HistoryArchive` owns the write lifecycle in one assets directory. Writes swallow `OSError` the
way `ContextManager._materialize_output_sync` does, so a read-only or full disk costs a file and
never a turn. Whether the checkpoint names the index is the session's call
(`Session.compacted_history_line`), made from what actually landed on disk.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from wizolt.base import HISTORY_INDEX_ASSET, SESSION_EVENT_KEY, Json
from wizolt.image import ImageInputs
from wizolt.prompts import COMPACTION_SUMMARY_TITLE
from wizolt.session import HistorySegment

# One grep hit must not dump a whole span into the context window, and one summary must not bloat
# the index it sits in. Both limits say in the file what they left out.
WRAP_CHARS = 400
SUMMARY_CHARS = 500
# The marker `ContextManager.store_history_segment` left where it cut an older segment's excerpt:
# bound without a key or a file, so it carries no attribute past `omitted_tokens`. A tool result's
# own marker inside the excerpt names its file (or, in older sessions, its recall key) after that.
_EXCERPT_CUT = re.compile(r'<bounded_output omitted="middle" max_tokens="\d+" estimated_tokens="\d+" omitted_tokens="(\d+)"/>')
_ROWS_NOTE = f"Rows: u=user, a=assistant, t=tool call label (arguments and output not included). Long rows wrap at {WRAP_CHARS} chars; every row of a message keeps its prefix."
_EXCERPT_ROWS_NOTE = f"Rows: the text as stored at compaction, without message labels. Long rows wrap at {WRAP_CHARS} chars."
_EXCERPT_CUT_NOTE = "Excerpt: middle omitted at compaction (~{tokens} tokens); original text only in the session log."


class SegmentDocument:
    """One `history.N.md`: its header, its summary, and the evicted span as rows.

    Built with the messages this compaction evicted, it labels and numbers every row. Built with
    `excerpt`, the segment is an older session's, whose only surviving text is the excerpt
    compaction stored -- a flat `role:` + content concatenation with no message structure left to
    label -- so the body is that text wrapped as-is.
    """

    def __init__(self, segment: HistorySegment, conversation: Sequence[Json] | None):
        self._segment = segment
        self._conversation = conversation  # None: an excerpt, rendered from `segment.text`

    @classmethod
    def excerpt(cls, segment: HistorySegment) -> SegmentDocument:
        return cls(segment, None)

    def render(self) -> str:
        segment = self._segment
        rows = [f"# {segment.export_name.removesuffix('.md')} · {segment.created_at} · {segment.title}"]
        if self._conversation is None:
            rows.extend(self._excerpt_notes())
            body_title, body = "## Excerpt", self._wrap_rows(segment.text)
        else:
            rows.append(_ROWS_NOTE)
            body_title, body = "## Conversation", self._conversation_rows(self._conversation)
        rows.append("")
        if segment.summary:
            # Verbatim, never wrapped: it is one block of prose, not a searchable transcript.
            rows.extend(("## Summary at compaction", segment.summary, ""))
        rows.extend((body_title, *body))
        return "\n".join(rows) + "\n"

    def _excerpt_notes(self) -> list[str]:
        """The row note, and how much the excerpt lost when compaction cut it. The segment-level cut
        omits the most, so the largest bare marker is it; a span short enough to be stored whole
        has none, and then nothing is claimed missing."""
        cuts = [int(tokens) for tokens in _EXCERPT_CUT.findall(self._segment.text)]
        return [_EXCERPT_ROWS_NOTE, *([_EXCERPT_CUT_NOTE.format(tokens=max(cuts))] if cuts else [])]

    @classmethod
    def _conversation_rows(cls, conversation: Sequence[Json]) -> list[str]:
        """Every message's rows, numbered from 1 in the order they were compacted.

        A message that renders nothing does not consume a number: the numbers exist to grep one
        message back, and a number nothing carries could not be grepped."""
        rows: list[str] = []
        number = 0
        for message in conversation:
            if labelled := cls._message_text(message):
                number += 1
                marker, text = labelled
                rows.extend(f"{marker}{number} | {row}" for row in cls._wrap_rows(text))
        return rows

    @staticmethod
    def _message_text(message: Json) -> tuple[str, str] | None:
        """(row marker, text) for one message, or None when it carries nothing to show.

        A tool result contributes its label line only -- `tool tr.N <name> <args>`, plus how the
        call ended when that is not ok: its arguments can be re-derived and its output would drown
        the search, which is the whole point of the file."""
        role = message.get("role")
        if role == "tool":
            head, _, rest = str(message.get("content") or "").partition("\n")
            if not head.strip():
                return None
            # `ToolRunner.tool_message` writes `status: <word>` on the line right after the label
            # when the call did not succeed; anything further down is the tool's own output.
            second = rest.partition("\n")[0]
            status = second.removeprefix("status: ").strip() if second.startswith("status: ") else ""
            return "t", head.strip() + (f" {status}" if status else "")
        if role not in ("user", "assistant") or message.get(SESSION_EVENT_KEY) or ImageInputs.is_tool_observation(message):
            return None
        text = ImageInputs.label_text(message)
        if not text.strip() or (role == "user" and text.startswith(COMPACTION_SUMMARY_TITLE)):
            return None
        return ("u" if role == "user" else "a"), text

    @staticmethod
    def _wrap_rows(text: str) -> list[str]:
        """Each line of `text` as rows of at most WRAP_CHARS.

        Python strings are code points, so a slice never splits a character. Nothing is dropped and
        no ellipsis is added: the shared row prefix is what tells the reader a message continues."""
        return [line[at : at + WRAP_CHARS] for line in text.splitlines() for at in range(0, len(line), WRAP_CHARS) or (0,)]


class HistoryArchive:
    """The compacted-history exports in one assets directory, and their write lifecycle.

    Every retained segment without a file yet -- an older session's, or one whose write failed --
    is backfilled as an excerpt before the new one is written, and each gains its index entry only
    after its own file has landed. Each write answers for itself: an `OSError` costs that one file.
    """

    def __init__(self, assets_dir: str):
        self._dir = assets_dir

    def export(self, segment: HistorySegment, compacted: Sequence[Json], retained: Sequence[HistorySegment]) -> None:
        for older in retained:
            if older is not segment and older.export_name and not os.path.exists(os.path.join(self._dir, older.export_name)):
                self._add(older, SegmentDocument.excerpt(older))
        self._add(segment, SegmentDocument(segment, compacted))

    def _add(self, segment: HistorySegment, document: SegmentDocument) -> None:
        if segment.export_name and self._write(segment.export_name, document.render()):
            self._write(HISTORY_INDEX_ASSET, self._index_entry(segment), append=True)

    @staticmethod
    def _index_entry(segment: HistorySegment) -> str:
        """What `history.md` gains for one segment: what it is, and what it settled."""
        name = segment.export_name
        rows = [f"{name} · {segment.created_at} · {segment.messages} msgs · {segment.title}"]
        if summary := " ".join(segment.summary.split()):
            if len(summary) > SUMMARY_CHARS:
                summary = summary[:SUMMARY_CHARS] + f"… [+{len(summary) - SUMMARY_CHARS} chars; full summary in {name}]"
            rows.append(f"{name} | {summary}")
        return "\n".join(rows) + "\n\n"

    def _write(self, name: str, text: str, *, append: bool = False) -> bool:
        try:
            os.makedirs(self._dir, exist_ok=True)
            with open(os.path.join(self._dir, name), "a" if append else "w", encoding="utf-8") as file:
                file.write(text)
        except OSError:
            return False
        return True
