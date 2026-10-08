"""Resume rendering: replaying a resumed session's transcript into scrollback, and the
paste-ready resume line a freshly persisted session prints.

Owned by `CommandLoop` as `resume`. The replay rules here (call/result pairing and
ordering, silent-batch rules during replay, diff previews) are the resume-side counterpart
of the live turn's rendering and change for their own reasons; they live in one place so
the live path and the replay path can be kept in agreement deliberately, not by distance.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from itertools import pairwise
from typing import ClassVar, Protocol

from wizolt.agent.prompts import LIVE_FOLLOWUP_PREFIX, split_reaction
from wizolt.base import (
    Json,
    LogBlock,
    LogEdge,
    LogLine,
    LogRole,
    ToolCall,
    ToolError,
    TurnBox,
    oneline,
)
from wizolt.image import ImageInputs
from wizolt.session import Session, SessionSnapshotCodec, ToolResultRecord
from wizolt.session.types import OperationReceipt
from wizolt.tools import TOOL_REGISTRY, tool_payload, toolblocks, tooloutput
from wizolt.tools.toolblocks import ToolDisplay
from wizolt.ui.render import UiPrinter


class TranscriptOutput(Protocol):
    """The output operations shared by live turns and restored transcripts."""

    ui: UiPrinter
    MIN_ROWS_BETWEEN_RULES: ClassVar[int]

    def emit(self, text: str | LogBlock = "", indent: int = 0) -> None: ...
    def tool_output(self, text: str | LogBlock = "") -> None: ...
    def restart_silent_batches(self) -> None: ...
    def count_silent_batch(self) -> None: ...
    def user_turn_rule(self) -> None: ...


class ResumeRenderer:
    """The transcript replay and resume-line half of the session restore story.

    Reads semantic session records and writes through TranscriptOutput, sharing rules and
    batching with live rendering. Context accounting is refreshed through an explicit callback;
    replay has no command-loop or agent handle.
    """

    TRANSCRIPT_DIFF_LINES: ClassVar[int] = 40
    # Resume redraws at most this many recent turns. A long session would otherwise flood the
    # terminal with the whole transcript and push the prompt out of reach; the earlier turns stay
    # in the session, so the next request still sees them.
    MAX_REDRAWN_TURNS: ClassVar[int] = 20

    def __init__(self, session: Session, presentation: TranscriptOutput, refresh_context: Callable[[], object]) -> None:
        self.session = session
        self.presentation = presentation
        self.refresh_context = refresh_context
        self.reactions: dict[int, str] = {}  # id(user message) -> the reaction its reply opened with
        self.receipts_by_key: dict[str, OperationReceipt] = {}
        self.receipts_by_id: dict[str, OperationReceipt] = {}

    def render_resumed_session(self) -> None:
        # Transcript reconstruction owns historical call/result matching and ordering invariants.
        if not self.session.resumed:
            return
        self.session.resumed = False
        # The percent is derived, not persisted; recompute it or the status bar reads 0% until
        # the first turn.
        self.refresh_context()
        transcript = self.session.transcript_messages or self.session.messages
        tool_results = {
            str(message.get("tool_call_id") or ""): message for message in transcript if message.get("role") == "tool" and message.get("tool_call_id")
        }
        # Older snapshots recorded a rejected Read as failed. The retained model result, when
        # present, still distinguishes the known missing/unreadable-file errors from failures.
        for message in self.session.messages:
            result = tool_results.get(str(message.get("tool_call_id") or "")) if message.get("role") == "tool" else None
            if result is None or result.get("status") != "failed":
                continue
            content = str(message.get("content") or "")
            if content.startswith("tool - Read "):
                reason = content.partition("\noutput:\n")[2].removeprefix("ToolError:").strip()
                if reason.startswith(("no such file:", "cannot read ")):
                    tool_results[str(message["tool_call_id"])] = {**result, "status": "rejected", "reason": reason}
        semantic_tool_results = any("status" in message for message in tool_results.values())
        messages = [message for message in transcript if not SessionSnapshotCodec.is_internal_message(message) and message.get("role") != "tool"]
        # Providers may reuse call IDs across turns, so a receipt is tied to its call by the stored
        # result key it names, or by an ID that occurs once; an ambiguous one shows no note at all.
        calls = Counter(raw.get("id") for message in messages for raw in message.get("tool_calls") or [] if isinstance(raw, dict))
        receipts = [receipt for receipt in self.session.operation_receipts if receipt.operation == "tool.call"]
        self.receipts_by_key = {receipt.actual: receipt for receipt in receipts if receipt.actual}
        self.receipts_by_id = {receipt.target: receipt for receipt in receipts if not receipt.actual and calls[receipt.target] == 1}
        # The replay is a burst of independent emits; batch them into a single print_formatted_text
        # call so the whole session restores in one flush (and one TUI coordination) instead of one
        # per line.
        with self.presentation.ui.batched():
            self.presentation.emit(f"Restored session: {self.session.uid}")
            if self.session.transcript_incomplete:
                self.presentation.emit("Warning: this transcript may omit turns written by an older wizolt version.")
            if not messages:
                return
            transcript_diffs = self.session.transcript_turn_diffs or self.session.turn_diffs
            diffs = {diff.key: diff.diff for diff in transcript_diffs if diff.key and diff.diff}
            tool_record_index = 0
            # A reply that opened with a reaction marker reacted to the message that opened its
            # turn; a live follow-up is never held for one, so it gets none here either.
            self.reactions = {
                id(message): split_reaction(ImageInputs.label_text(reply))[0]
                for message, reply in pairwise(messages)
                if message.get("role") == "user"
                and reply.get("role") == "assistant"
                and not str(message.get("content") or "").startswith(LIVE_FOLLOWUP_PREFIX.strip())
            }
            turns = TurnBox.group(messages)
            hidden = len(turns) - self.MAX_REDRAWN_TURNS
            if hidden > 0:
                # The earliest turns are not redrawn: on a long session they would flood the terminal
                # and the prompt would scroll out of reach. They stay in the session, so the next
                # request still sees them; only the redraw is skipped. Tool records still advance
                # through them so the visible turns pair with their own results.
                for turn in turns[:hidden]:
                    for message in turn.messages:
                        tool_record_index = self.render_transcript_message(message, tool_record_index, diffs, tool_results, dry_run=True)
                self.presentation.emit(f"… {hidden} earlier turn{'s' if hidden > 1 else ''} not redrawn (still in context)")
                self.presentation.emit("")
                turns = turns[hidden:]
            for i, turn in enumerate(turns):
                if i:
                    self.presentation.emit("")
                for message in turn.messages:
                    tool_record_index = self.render_transcript_message(message, tool_record_index, diffs, tool_results)
            if not semantic_tool_results:
                self.render_remaining_tool_records(tool_record_index, diffs)

    def render_transcript_message(
        self,
        message: Json,
        tool_record_index: int = 0,
        diffs: dict[str, str] | None = None,
        tool_results: dict[str, Json] | None = None,
        *,
        dry_run: bool = False,
    ) -> int:
        presentation = self.presentation
        role = str(message.get("role") or "")
        content = ImageInputs.label_text(message).strip()
        if role == "assistant":
            content = split_reaction(content)[1].strip()  # drawn beside the user's message instead
        if role == "assistant" and content and not dry_run:
            # Every assistant message sits in the content column, final answer included, so a
            # resumed session reads exactly like the live one. The turn's own text all shares that
            # column with the user's message, whose `• ` bullet hangs in the same two-space margin.
            presentation.ui.separate()  # the same gap the live narration and answer open with
            # An assistant message that carries tool calls is interim narration, not the answer:
            # the resumed session draws the same phase rule above it the live turn did (the rule
            # opens the text), skipping it only when it would land too close to the rule above.
            if message.get("tool_calls") and presentation.ui.rule_due(presentation.MIN_ROWS_BETWEEN_RULES):
                presentation.ui.emit_phase_rule()
            presentation.ui.emit_answer(content, role=role, rule=False, indent=TurnBox.CONTENT_LEVEL)
        if role == "assistant":
            tool_record_index = self.render_transcript_tool_calls(message, tool_record_index, diffs or {}, tool_results or {}, dry_run=dry_run)
            if not dry_run and message.get("tool_calls"):
                # Each tool-bearing assistant message is one batch in the replay: a voiced one
                # (it carried narration) restarts the silent count, a silent run of four closes
                # with the same batch rule the live turn drew.
                if content:
                    presentation.restart_silent_batches()
                else:
                    presentation.count_silent_batch()
            return tool_record_index
        if role == "user" and content and not ImageInputs.is_tool_observation(message) and not dry_run:
            # The follow-up marker is model-facing context, part of history because it was sent.
            # The scrollback shows what the user typed, exactly as it looked when they typed it.
            presentation.ui.emit_answer(
                content.removeprefix(LIVE_FOLLOWUP_PREFIX.strip()).lstrip(), role=role, rule=False, reaction=self.reactions.get(id(message), "")
            )
            # Replay the same spacing used by a live turn.
            presentation.user_turn_rule()
        return tool_record_index

    def render_transcript_tool_calls(
        self,
        message: Json,
        tool_record_index: int,
        diffs: dict[str, str],
        tool_results: dict[str, Json] | None = None,
        *,
        dry_run: bool = False,
    ) -> int:
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            return tool_record_index
        for raw in raw_calls:
            call = self.transcript_tool_call(raw)
            if call is None:
                continue
            result = (tool_results or {}).get(call.id)
            if result is not None and "status" in result:
                if not dry_run:
                    self.emit_transcript_tool(
                        call,
                        str(result.get("result_key") or ""),
                        diffs,
                        status=str(result.get("status") or "failed"),
                        reason=str(result.get("reason") or ""),
                    )
                    self.emit_receipt(self.receipt_for(call, str(result.get("result_key") or "")))
                continue
            record, tool_record_index = self.transcript_tool_record(call, tool_record_index)
            if not dry_run:
                self.emit_transcript_tool(call, record.key if record else "", diffs)
                self.emit_receipt(self.receipt_for(call, record.key if record else ""))
        return tool_record_index

    def receipt_for(self, call: ToolCall, key: str) -> OperationReceipt | None:
        return self.receipts_by_key.get(key) if key and key in self.receipts_by_key else self.receipts_by_id.get(call.id)

    def emit_receipt(self, receipt: OperationReceipt | None) -> None:
        """Replay what a plugin did to this call from its recorded receipt; no plugin runs."""
        if receipt is None:
            return
        notes: list[tuple[str, LogRole]] = []
        if receipt.origin != "core":
            notes.append((f"answered by plugin {receipt.origin.partition('/')[0]}; the tool did not run", LogRole.META))
        if receipt.core == "unknown":
            notes.append(("wizolt stopped while it ran; outcome unknown", LogRole.WARNING))
        if receipt.shaped:
            notes.append((f"result changed by {', '.join(name.partition('/')[0] for name in receipt.shaped)}", LogRole.META))
        if receipt.wrapper_failure:
            notes.append((oneline(receipt.wrapper_failure, 200), LogRole.ERROR))
        if notes:
            self.presentation.tool_output(LogBlock.hierarchy(None, [LogLine("plugin", text, role, LogEdge.END) for text, role in notes]))

    def render_remaining_tool_records(self, tool_record_index: int, diffs: dict[str, str]) -> None:
        records = self.session.transcript_tool_records or self.session.tool_records
        for record in records[tool_record_index:]:
            call = ToolCall(id="", name=record.name, args=record.args)
            self.emit_transcript_tool(call, record.key, diffs)

    def emit_transcript_tool(self, call: ToolCall, key: str, diffs: dict[str, str], *, status: str = "ok", reason: str = "") -> None:
        """An Edit shows the diff it made, the way it did when the edit ran live. Live, that preview
        comes from the approval block; here the stored diff text is the same string, so replaying it
        needs no reconstruction."""
        tool_class = TOOL_REGISTRY.get(call.name)
        # Live, a silent tool logs nothing unless it failed or was rejected; replay shows no more.
        if tool_class is not None and tool_class.SILENT and status == "ok":
            return
        if status == "rejected":
            self.presentation.tool_output(toolblocks.reject_display(self.session, call, reason or "rejected in saved session", d=ToolDisplay()))
            return
        preview = diffs.get(key, "") if call.name == "Edit" else ""
        # Through `tool_output`, like the live call: a replayed call opens its own group with a
        # blank row above it, and its result stays attached underneath. Emitted directly, every
        # call in a turn ran into the one above it and into the narration that introduced them.
        if not preview:
            # An Ask's stored result is the user's answer, which its finish block shows live; every
            # other call replays as its `tr.N` marker alone.
            output = "failed in saved session" if status != "ok" else self.session.tool_results.get(key, "") if call.name == "Ask" else ""
            self.presentation.tool_output(toolblocks.finish_display(self.session, call, key, output, failed=status != "ok"))
            return
        # The preview block carries the call line, so the result collapses to its trailing marker
        # underneath it — the same nesting the live approval block produces.
        self.presentation.tool_output(self.transcript_edit_preview(call, preview))
        self.presentation.tool_output(toolblocks.finish_display(self.session, call, key, "", failed=False, d=ToolDisplay(nested_display=True)))

    def transcript_edit_preview(self, call: ToolCall, preview: str) -> LogBlock:
        lines = preview.rstrip().splitlines()
        # A long replay would bury the prompt under diffs, so each one is trimmed to a readable
        # window; the stored result holds the full text, and Ctrl-O opens it.
        hidden = max(0, len(lines) - self.TRANSCRIPT_DIFF_LINES)
        if hidden:
            lines = lines[: self.TRANSCRIPT_DIFF_LINES]
        children = [LogLine("", line, LogRole.DIFF, LogEdge.CONTINUE) for line in lines]
        if hidden:
            children.append(LogLine("", f"… {hidden} more lines, Ctrl-O for the full diff", LogRole.META, LogEdge.CONTINUE))
        return LogBlock.hierarchy(toolblocks.log_root(tooloutput.short_call(self.session, call), LogRole.AUTO, "", call), children)

    @staticmethod
    def transcript_tool_call(raw: object) -> ToolCall | None:
        if not isinstance(raw, dict):
            return None
        raw_function = raw.get("function")
        function = raw_function if isinstance(raw_function, dict) else {}
        name = str(function.get("name") or "")
        if not name:
            return None
        arguments = function.get("arguments")
        try:
            # strict=False tolerates literal newlines in argument strings (e.g. multi-line
            # git commit messages) that would otherwise be rejected as invalid JSON.
            payload = json.loads(arguments, strict=False) if isinstance(arguments, str) else (arguments or {})
        except json.JSONDecodeError:
            payload = {}
        try:
            args = tool_payload(name, payload)
        except ToolError:
            # A malformed historical call (e.g. tool args that fail validation) must not crash
            # the resume; render it without parsed args.
            args = [payload] if payload else []
        return ToolCall(id=str(raw.get("id") or ""), name=name, args=args)

    def transcript_tool_record(self, call: ToolCall, tool_record_index: int) -> tuple[ToolResultRecord | None, int]:
        tool_class = TOOL_REGISTRY.get(call.name)
        if tool_class is not None and not tool_class.STORES_RESULT:
            return None, tool_record_index
        records = self.session.transcript_tool_records or self.session.tool_records
        while tool_record_index < len(records):
            record = records[tool_record_index]
            tool_record_index += 1
            if record.name == call.name:
                return record, tool_record_index
        return None, tool_record_index

    async def save_and_emit_resume(self) -> None:
        uid = await self.session.save_snapshot()
        group = self.session.subagents
        target = group.root.session if group is not None else self.session
        if target is not self.session:
            uid = await target.save_snapshot()
        self.emit_resume_line(uid)

    def emit_resume_line(self, uid: str) -> None:
        """The paste-ready resume line for a session that has just been persisted."""
        if uid:
            # The name goes in the sentence, never in the command: the line below is meant to be
            # pasted, and only the uid is guaranteed to still mean this session tomorrow.
            group = self.session.subagents
            name = group.root.session.name if group is not None and uid == group.root.session.uid else self.session.name
            self.presentation.ui.separate()
            self.presentation.emit(f"Resume {name!r} with:\nwizolt --resume {uid}" if name else f"Resume with:\nwizolt --resume {uid}")
