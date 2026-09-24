"""Session value types: the plain records `Session` and its features read and write.

Kept separate from the `Session` object so the state shape a session persists and
projects is one import away from the behavior that mutates it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar, cast

from wizolt.base import Json, ToolArgs


@dataclass
class PlanItem:
    _PLAN_LINE_RE: ClassVar[re.Pattern] = re.compile(r"\[( |x|X|~|-)\]\s+(.+)")
    STATUSES: ClassVar[tuple[str, ...]] = ("todo", "doing", "done", "blocked")
    SYMBOLS: ClassVar[dict[str, str]] = {"todo": " ", "doing": "~", "done": "x", "blocked": "-"}
    LEGACY_MARKERS: ClassVar[dict[str, str]] = {" ": "todo", "~": "doing", "x": "done", "X": "done", "-": "blocked"}

    status: str
    text: str

    @classmethod
    def parse(cls, value: object) -> PlanItem | None:
        if isinstance(value, cls):
            status, text = value.status, value.text
        elif isinstance(value, dict):
            status = str(value.get("status") or "todo").strip().lower()
            text = str(value.get("text") or "").strip()
        else:
            raw = str(value).strip()
            match = PlanItem._PLAN_LINE_RE.fullmatch(raw)
            status = cls.LEGACY_MARKERS[match.group(1)] if match else "todo"
            text = match.group(2).strip() if match else raw
        if not text:
            return None
        return cls(status if status in cls.STATUSES else "todo", text)

    def row(self, *, status: bool = False, style: str = "text") -> str:
        prefix = f"[{self.SYMBOLS[self.status]}] " if status and style == "symbol" else f"{self.status}: " if status else ""
        return "- " + prefix + self.text


@dataclass
class AgentState:
    goal: str = ""
    plan: list[PlanItem | Json | str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    check: str = ""
    summary: str = ""
    # How this session is labelled when listed, and where that label came from. `apply` never sets
    # either: the name follows the user and the goal, not whatever a tool call happens to write.
    name: str = ""
    name_source: str = ""  # "" | user | goal | input
    context_percent: int = 0
    context_tokens: int = 0  # transient projection estimate; do not reconstruct it from a rounded percentage
    turn_step: int = 0
    turn_messages: int = 0
    round_count: int = 0
    current_model_call_started_at: float = 0.0
    manual_model_retry_requested: bool = False
    model_retry_count: int = 0
    current_model_attempt: int = 0
    model_retry_reason: str = ""
    model_retry_until: float = 0.0  # monotonic deadline of the current retry wait; 0 when idle
    compaction_count: int = 0
    # The last delegation that failed on this worker, for `Delegate status` to tell the parent
    # (which cannot see the worker) why it stopped, instead of the parent having to remember.
    # Live display state: never persisted.
    last_error: str = ""
    last_error_round: int = 0
    # The current request's output stream, for the throughput the running divider shows. Characters
    # rather than tokens because token deltas are not on the wire: providers report usage once, when
    # the request is over. Reset at the start of every attempt and cleared when it ends, so the rate
    # belongs to the response being watched and never survives it. Live display state, never persisted.
    stream_started_at: float = 0.0
    stream_chars: int = 0

    def __post_init__(self) -> None:
        self.plan = cast(list[PlanItem | Json | str], self.plan_items(self.plan))

    @classmethod
    def plan_items(cls, items: Iterable[object]) -> list[PlanItem]:
        return [item for raw in items if (item := PlanItem.parse(raw))]

    @classmethod
    def plan_rows_for(cls, items: Iterable[object], *, status: bool = False, style: str = "text") -> list[str]:
        rows = [item.row(status=status, style=style) for item in cls.plan_items(items)]
        return rows or ["- (empty)"]

    def apply_summary(self, data: Json) -> None:
        """Take the one field a compaction reply owns.

        `goal`, `plan`, `known` and `check` are `Note`'s, and a compaction used to overwrite all
        four from the same JSON. They did not need rescuing: they live here and survive eviction
        untouched, so handing them to a summarizer only let a prose re-derivation replace a
        validated structure -- and compound, since the next pass re-derived from that. `summary`
        is different: the compactor is the only party that read the evicted span, so it is the one
        thing it must produce. Anything it learned that belongs in `known` reaches the model
        through the summary, which can then call `Note` like any other writer.
        """
        if isinstance(data.get("summary"), str):
            self.summary = str(data["summary"]).strip()

    def format(self, *, include_summary: bool = False) -> str:
        known = ["- " + item for item in self.known] or ["- (empty)"]
        rows = [
            "Goal: " + (self.goal or "(empty)"),
            "Plan:",
            *self.plan_rows_for(self.plan, status=True),
            "Known:",
            *known,
            "Check: " + (self.check or "(empty)"),
        ]
        if include_summary:
            rows.extend(("Summary:", self.summary or "(empty)"))
        return "\n".join(rows)


@dataclass
class ToolResultRecord:
    key: str
    name: str
    args: ToolArgs
    output: str
    note: str = ""


@dataclass
class ToolErrorRecord:
    key: str
    name: str
    args: ToolArgs
    error: str


@dataclass
class TurnDiff:
    SNAPSHOT_CHAR_LIMIT: ClassVar[int] = 1_000_000
    TRANSCRIPT_CHAR_LIMIT: ClassVar[int] = 64 * 1024

    key: str
    turn: int
    path: str
    diff: str
    before: str = ""
    after: str = ""
    round: int = 0

    @classmethod
    def bounded_snapshots(cls, before: str, after: str) -> tuple[str, str]:
        """Cap each snapshot on its own. Snapshots are stored once per unique content, so a pair
        usually costs one new version rather than two, and summing the two would hold the ceiling at
        half the file size it can actually afford. Both are dropped together when either is too
        large: one alone would read as the file being created or deleted wholesale."""
        return ("", "") if max(len(before), len(after)) > cls.SNAPSHOT_CHAR_LIMIT else (before, after)

    @classmethod
    def bounded_transcript(cls, diff: str) -> str:
        if len(diff) <= cls.TRANSCRIPT_CHAR_LIMIT:
            return diff
        clipped = diff[: cls.TRANSCRIPT_CHAR_LIMIT].rsplit("\n", 1)[0]
        return clipped + "\n… diff preview truncated; see /diff for the retained session diff"


@dataclass
class HistorySegment:
    """One compacted span of conversation, kept for two readers. `text` is a bounded verbatim
    excerpt: `/compact log` shows it to the user, and the compaction that produced the segment
    writes it to `history.N.md` for the model, which reads that file rather than this text. The
    evicted messages are captured once at compaction time (never re-summarized), so repeated
    compaction cannot compound loss.

    The fields after `text` describe the compaction that produced the segment, for `/compact log`
    and for the export's index entry; they are what makes an eviction reviewable afterwards.
    `summary` is the checkpoint summary as it stood at this compaction -- the live checkpoint
    carries only the newest one, so without this copy every earlier summary would be unreachable
    once the next compaction replaced it."""

    key: str
    title: str
    text: str = ""
    created_at: str = ""
    scope: str = ""  # "history" (prior conversation) | "turn" (the running turn)
    trigger: str = ""  # "auto" (over budget) | "manual" (/compact)
    fallback: bool = False  # the summarizer failed and the span was trimmed deterministically
    messages: int = 0  # evicted message count
    summary: str = ""
    model: str = ""  # effective model the summary ran on; empty = fell back to trimming

    _KEY_RE: ClassVar[re.Pattern] = re.compile(r"seg\.(\d+)")

    @property
    def export_name(self) -> str:
        """`history.N.md` for `seg.N`, the file wizolt.history exports this segment to; "" for a
        key that does not parse, which has no file."""
        match = self._KEY_RE.fullmatch(self.key)
        return f"history.{match.group(1)}.md" if match else ""
