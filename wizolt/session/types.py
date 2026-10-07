"""Session value types: the plain records `Session` and its features read and write.

Kept separate from the `Session` object so the state shape a session persists and
projects is one import away from the behavior that mutates it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar, NotRequired, TypedDict, cast

from wizolt.base import Json, ToolArgs
from wizolt.image import IMAGE_REFS_KEY, ImageRef, UserInput


class SubagentRecord(TypedDict):
    """Root-owned child identity; archived results preserve undelivered notifications.

    Full conversation text lives only in the child's snapshot. The bounded result envelope
    also supplies the archived picker preview, without copying a second answer into main.
    """

    uid: str
    parent: str
    instruction: str
    name: NotRequired[str]
    archived: NotRequired[bool]
    result: NotRequired[Json]


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
    awaiting_input: bool = False  # runtime only: approvals and Ask are scoped to this agent.
    last_turn_status: str = "idle"
    last_turn_error: str = ""
    # Only Agent.run owns this clock. Menus/inbox drivers are not model turns. Persist
    # elapsed time, never a monotonic timestamp from a previous process.
    turn_started_at: float = 0.0
    turn_elapsed: float = 0.0
    turn_result: Json = field(default_factory=dict)
    child_results_seen: dict[str, str] = field(default_factory=dict)
    # The current request's output stream, for the throughput the running divider shows. Characters
    # rather than tokens because token deltas are not on the wire: providers report usage once, when
    # the request is over. Reset at the start of every attempt and cleared when it ends, so the rate
    # belongs to the response being watched and never survives it. Live display state, never persisted.
    stream_started_at: float = 0.0
    stream_chars: int = 0

    def estimated_output_rate(self, now: float) -> float | None:
        """Shared live-speed estimate for native UI and plugins, absent before one second.

        Providers report token totals at completion; the live approximation uses four streamed
        characters per token. Keep the heuristic here so different projections cannot drift.
        """
        elapsed = now - self.stream_started_at if self.stream_started_at else 0
        return self.stream_chars / 4 / elapsed if self.stream_chars and elapsed >= 1 else None

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.turn_started_at) if self.turn_started_at else self.turn_elapsed

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
class OperationReceipt:
    """What an intercepted operation actually did, kept apart from what was delivered.

    Bounded and durable: inputs are clipped JSON text and the actual result is a reference to the
    existing tool record (``tr.N``), never a second copy of the payload. ``core`` is ``not_run``,
    ``started``, ``completed``, ``failed``, ``interrupted`` or ``unknown`` (it may have run before
    a crash; resume never retries it). ``origin`` is ``core`` or the plugin registration that
    produced the result; ``shaped`` names registrations that changed the delivered result.
    """

    TEXT_LIMIT: ClassVar[int] = 2000
    LIMIT: ClassVar[int] = 200  # Receipts kept per session, newest last.

    id: str
    operation: str
    target: str
    original: str = ""
    effective: str = ""
    origin: str = "core"
    core: str = "not_run"
    actual: str = ""
    shaped: tuple[str, ...] = ()
    wrapper_failure: str = ""
    retry_of: str = ""
    # Settled: the turn holds a result or settlement for it. Only a crash leaves this unset.
    delivered: bool = False

    @classmethod
    def clip(cls, value: object) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return text if len(text) <= cls.TEXT_LIMIT else text[: cls.TEXT_LIMIT] + "…"


@dataclass
class TurnDiff:
    TRANSCRIPT_CHAR_LIMIT: ClassVar[int] = 64 * 1024

    key: str
    turn: int
    path: str
    diff: str
    round: int = 0

    @classmethod
    def bounded_transcript(cls, diff: str) -> str:
        if len(diff) <= cls.TRANSCRIPT_CHAR_LIMIT:
            return diff
        clipped = diff[: cls.TRANSCRIPT_CHAR_LIMIT].rsplit("\n", 1)[0]
        return clipped + "\n… diff preview truncated"


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
    # Paths the evicted messages read without editing, as the calls named them: recorded by the
    # host rather than recalled by the summarizer, so a filename is never reworded on its way out.
    files_read: list[str] = field(default_factory=list)

    _KEY_RE: ClassVar[re.Pattern] = re.compile(r"seg\.(\d+)")

    @property
    def export_name(self) -> str:
        """`history.N.md` for `seg.N`, the file wizolt.agent.history exports this segment to; "" for a
        key that does not parse, which has no file."""
        match = self._KEY_RE.fullmatch(self.key)
        return f"history.{match.group(1)}.md" if match else ""


@dataclass(eq=False)
class QueuedInput:
    text: str
    images: tuple[ImageRef, ...] = ()
    draft: str = ""
    inflight: bool = False
    # Input the user held back with Tab: `claim_user_inputs` skips it, so the engine never sees it
    # mid-turn and the runtime starts it as a fresh turn instead. Persisted so a resumed queue keeps
    # one held input per turn instead of merging them into the first one.
    next_turn: bool = False
    # Only frontend submissions may be interpreted as commands. Model-authored tasks are
    # literal turn input, even when they say "exit" or start with a slash. Preserve on resume.
    commands: bool = False
    # The submitted form this entry came from, while it is still live in memory: the queue's
    # rows, the echo, and recall read it, so a folded paste stays a chip and an attached image
    # stays a label everywhere the text is still the user's own. Never serialized -- a snapshot
    # cannot carry the references, and the flattened `draft` is what a resumed entry reads.
    source: UserInput | None = None
    # The admission receipt once claimed: the effective model message and expansion records, or
    # a refusal. Reused across request rebuilds, release/reclaim and resume, so plugins, hooks
    # and mention discovery run once per submitted item.
    admission: Json | None = None
    # Who authored it: "user" (a frontend submission) or "child" (a parent model's message to
    # a subagent). Interceptors see it as the prompt origin; it never makes input a command.
    origin: str = "user"

    def to_json(self) -> str | Json:
        if not self.images and not self.next_turn and not self.commands and self.admission is None and self.origin == "user":
            return self.text
        data: Json = {"text": self.text, "draft": self.draft}
        if self.images:
            data[IMAGE_REFS_KEY] = [image.to_json() for image in self.images]
        if self.next_turn:
            data["next_turn"] = True
        if self.commands:
            data["commands"] = True
        if self.admission is not None:
            data["admission"] = self.admission
        if self.origin != "user":
            data["origin"] = self.origin
        return data

    @classmethod
    def from_json(cls, value: object) -> QueuedInput | None:
        if isinstance(value, str):
            return cls(value) if value.strip() else None
        if not isinstance(value, dict):
            return None
        text = str(value.get("text") or "")
        raw_images = value.get(IMAGE_REFS_KEY)
        images = tuple(image for raw in raw_images if (image := ImageRef.from_json(raw)) is not None) if isinstance(raw_images, list) else ()
        draft = str(value.get("draft") or text)
        next_turn = value.get("next_turn") is True  # absent in snapshots written before the flag
        commands = value.get("commands") is True
        admission = value.get("admission") if isinstance(value.get("admission"), dict) else None
        origin = "child" if value.get("origin") == "child" else "user"
        if not text.strip():
            return None
        if draft.count("\ufffc") != len(images):
            return cls(text, next_turn=next_turn, commands=commands, admission=admission, origin=origin)
        return cls(text, images, draft, next_turn=next_turn, commands=commands, admission=admission, origin=origin)

    def user_input(self) -> UserInput:
        value = self.source if self.source is not None else UserInput(self.draft or self.text, self.images)
        value.origin = self.origin
        return value

    def message(self, prefix: str = "") -> Json:
        message: Json = {"role": "user", "content": prefix + self.text}
        if self.images:
            message[IMAGE_REFS_KEY] = [image.to_json() for image in self.images]
        return message
