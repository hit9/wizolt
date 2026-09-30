"""Own frontend output, streaming previews, phase rules and terminal presentation state.

Both live output and transcript replay use this object. It has no command dispatcher or agent
handle; runtime code attaches the terminal and ordered writer for the duration of a frontend run.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from wizolt.base import ImageRouteNotice, LogBlock, LogEdge, LogLine, LogRole, Text, TurnBox
from wizolt.session import Session
from wizolt.ui.cli.update import UpdateStatus
from wizolt.ui.render import BashLivePreview, InputStyle, StatusBar, UiPrinter

if TYPE_CHECKING:
    from wizolt.ui.cli.runtime import ScrollbackWriter
    from wizolt.ui.tui import TuiApp


class Presentation:
    MIN_ROWS_BETWEEN_RULES: ClassVar[int] = 6
    TOOL_RUN_RULE_BATCHES: ClassVar[int] = 4

    def __init__(self, session: Session, output_fn=print):
        self.session = session
        self.update = UpdateStatus()
        self.ui = UiPrinter(output_fn)
        self.input_style = InputStyle()
        self.status_bar = StatusBar(session)
        self.live_preview = BashLivePreview()
        self.model_stream_kind = ""
        self.model_stream_text = ""
        self.model_stream_promoted_text = ""
        self.live_status_paused = False
        self.compaction_active = False
        self.script_active = False
        # Ctrl-O can show the live script before it becomes a stored record.
        self.script_running_code = ""
        # Narration resets the count; a long silent run closes with a phase rule.
        self._silent_batches = 0
        self.starting = False
        self.background_output_lock = threading.Lock()
        self.background_output_open = True
        # Runtime lends the writer admission lock to the background-output gate so closing
        # output and scheduling a write share one decision.
        self.scrollback: ScrollbackWriter | None = None
        self.tui: TuiApp | None = None

    def context_reset_notice(self, text: str) -> None:
        self.emit(LogBlock.hierarchy(LogLine(text, role=LogRole.META), []))

    def image_route_notice(self, notice: ImageRouteNotice) -> None:
        """Show the one gray routing notice for a text-only image delivery decision.

        The root line names the observation and carries the routing reason as a gray meta
        suffix; the described-by entry is its single child with a tree edge, mirroring the
        ViewImage tool's rendering. The engine emits this only after the observation
        succeeded, so a failed vision call never shows a fake described-by. Presentation
        only; never enters model context.
        """

        children = [LogLine("described by", notice.described_by, LogRole.TOOL, LogEdge.END)] if notice.described_by else []
        count = len(notice.images)
        label = "Image" if count <= 1 else "Images"
        text = notice.images[0] if count == 1 else (f"{count} attachments" if count else "")
        root = LogLine(label, text, LogRole.META, LogEdge.NONE, meta=" · " + notice.reason)
        self.tool_output(LogBlock.hierarchy(root, children))

    def automatic_compaction_status(self, active: bool, error: str = "") -> None:
        """Show automatic context compaction as a distinct phase of the running turn."""
        self.compaction_active = active
        self.set_running_phase()
        if error:
            self.tool_output(LogBlock([LogLine("compaction fallback", error, LogRole.ERROR, LogEdge.END)]))

    def model_retry_wait_status(self, active: bool) -> None:
        """Show a retry backoff wait as a distinct phase instead of claiming the agent is working."""
        self.set_running_phase(retrying=active)

    def toolscript_run_status(self, active: bool, code: str = "") -> None:
        """Show a running ToolScript body as a distinct phase of the turn.

        A script is the one stretch where the model is idle and no single tool line is pending, so
        without this the divider claims "working" from approval until the whole batch is done. The
        source is held for the same reason: a long batch is exactly when the reader wants to see
        what is running, and until it returns there is no stored record to open."""
        self.script_active = active
        self.script_running_code = code if active else ""
        self.set_running_phase()

    def set_running_phase(self, retrying: bool = False) -> None:
        """Put the running divider on the innermost phase currently active."""
        if self.tui is None:
            return
        self.tui.set_running(
            "retrying" if retrying else "compacting context" if self.compaction_active else "running script" if self.script_active else "working"
        )

    def emit(self, text: str | LogBlock = "", indent: int = 0) -> None:
        self.ui.emit(text, indent)

    def emit_turn(self, text: str = "") -> None:
        """A line that belongs to the exchange rather than to the session around it: a turn
        outcome, a command's reply, a refusal to run one. Those sit in the content column with
        the model's text and the tool lines; session chrome (the banner, the restored-session
        notice, the resume line) stays at column 0 and frames them."""
        self.emit(text, TurnBox.CONTENT_LEVEL)

    def emit_background(self, text: str) -> None:
        """Emit from a daemon worker only while this loop still owns terminal output.

        The gate and the scrollback writer's admission share one lock while the TUI is live, so a
        worker cannot pass this check and then race the writer closing behind it."""
        with self.background_output_lock:
            if self.background_output_open:
                self.emit(text)

    def close_background_output(self, final_output: Callable[[], None] | None = None) -> None:
        with self.background_output_lock:
            self.background_output_open = False
            if final_output is not None:
                final_output()

    def with_status_paused(self, action):
        # Only quiet the standalone status row used by the simple frontend. The full TUI renders
        # status and output together, so it never needs this terminal-level coordination.
        was_running = self.status_bar.is_running()
        if was_running:
            self.status_bar.stop()
        try:
            return action()
        finally:
            if was_running:
                self.status_bar.start(reset=False)

    def tool_output(self, text: str | LogBlock = "") -> None:
        def output() -> None:
            # The blank line parts each block from the one above; it is skipped when the block
            # sits directly under a rule just drawn (the turn's opening rule, or a batch rule),
            # which already provides the seam.
            #
            # It is skipped again between two calls that each fit on one line: a run of them is a
            # list of what the agent did, and a blank row between every pair doubles its height for
            # nothing. The moment a call brings output, a diff, or narration with it, the gap is
            # back -- that block needs to be parted from the one above.
            packed = self.ui.single_line_block(text) and self.ui.emitted_single_line
            if not packed and (isinstance(text, str) or (text.items and isinstance(text.items[0], LogLine))):
                self.ui.separate()
            self.emit(text)

        self.with_status_paused(output)

    def builtin_call_output(self, label: str, detail: str) -> None:
        """Log a tool the provider ran for itself, so the transcript shows it like any other call.

        A provider-side search leaves no local tool call to log, and the running status label is gone
        the moment the turn ends. Without this line the transcript would credit the model with
        knowledge it went and looked up. No edge: a standalone row, nothing above it to join (a
        branch glyph would dangle."""
        self.tool_output(LogBlock([LogLine(label, Text.clip_width(detail, 120), LogRole.TOOL, LogEdge.NONE)]))

    @staticmethod
    def unpromoted_text(text: str, promoted: str) -> str:
        """What is left to publish after an early promotion already wrote `promoted` to scrollback.

        A local tool call ends the response, so its promoted text is the whole of it. A provider-side
        tool runs inside the response and the model keeps writing afterwards, so there the promotion
        is only a prefix: re-emitting the whole text would repeat it, and skipping it would drop
        everything the model wrote after the search."""
        answer = text.strip()
        if promoted and answer.startswith(promoted):
            return answer[len(promoted) :].strip()
        return answer

    def agent_output(self, text: str = "", *, interim: bool = True) -> None:
        """A turn's interim narration or its final answer, whichever the engine is publishing:
        `output_fn` feeds interim text here, and `final_output_fn` routes the answer through the
        same promotion handling with the flag flipped. Only the flag differs; the answer takes no
        phase rule below it, because the turn-end rule already closes the turn and two rules in a
        row would read as a box."""
        # An early promotion is presentation-only: Agent still publishes the same semantic text
        # after ModelClient returns. Consume the one-shot marker instead of printing it twice.
        promoted = self.model_stream_promoted_text
        self.model_stream_promoted_text = ""
        if promoted:
            remaining = self.unpromoted_text(text, promoted)
            if not remaining:
                return
            text = remaining
        emit = self.emit_narration if interim else self.emit_final_answer
        self.with_status_paused(lambda: emit(text))

    def agent_answer_output(self, text: str = "") -> None:
        """The turn's final answer: the same markdown rendering as interim narration, but no phase
        rule below it -- the turn-end rule is already there to close the turn."""
        self.agent_output(text, interim=False)

    def model_stream_output(self, kind: str, text: str) -> None:
        """Update the dim preview or permanently promote a protocol-complete response.

        `output_done` is internal and emitted only when ModelClient has seen both completed text and
        a tool call. The scrollback write is synchronous so prompt-toolkit cannot batch it with the
        immediately following ToolRunner output and leave the `responding` preview covering it.
        """
        promote = ""
        tui = self.tui
        if kind == "output_done" and self.session.has_inflight_user_inputs():
            # A request that carried live follow-ups logs them to scrollback only once it returns,
            # so promoting here would place the response above the message it answers. Leave the
            # preview standing and let the ordinary post-request output keep the transcript ordered.
            return
        if kind == "output_done":
            promote = text.strip()
            self.model_stream_kind = self.model_stream_text = ""
            if promote and tui is not None:
                self.model_stream_promoted_text = promote
        elif not kind:
            self.model_stream_kind = self.model_stream_text = ""
        elif not text:
            self.model_stream_kind, self.model_stream_text = kind, ""
        elif text:
            if kind != self.model_stream_kind:
                self.model_stream_kind, self.model_stream_text = kind, ""
            self.model_stream_text = (self.model_stream_text + text)[-8000:]
        if tui is not None:
            tui.invalidate_frame()
            if promote:
                # Queued, not written here: this runs on the runtime loop (a stream callback), and
                # printing above the application means suspending it. The turn awaits the writer's
                # barrier before its tool batch, so the promoted answer is on screen first.
                self.write_scrollback(lambda: self.with_status_paused(lambda: self.emit_narration(promote)))

    def write_scrollback(self, callback: Callable[[], None]) -> None:
        """Publish one completed write into the runtime's ordered queue, or print it directly.

        There is no queue outside the interactive runtime -- headless output is synchronous and
        already in order -- so the callback simply runs."""

        writer = self.scrollback
        if writer is not None:
            writer.submit(callback)
            return
        callback()

    def emit_narration(self, text: str) -> None:
        """A turn's interim narration, opened by the same full-width rule the turn ends with minus
        the label: the rule lands above the text, so it announces the new phase instead of closing
        the old one, and the narration's own text is the label, so the rule carries none. A rule
        that would land within MIN_ROWS_BETWEEN_RULES of the one above it is skipped -- two rules a
        few rows apart part nothing, they just add lines to an already short stretch. The agent
        saying two things in quick succession is one phase, not two."""
        # The narration breaks a run of silent tool batches: the agent spoke, so the count starts
        # over (the batch itself is reported voiced through on_tool_batch, not by this flag). The
        # blank line above parts it from the previous block unless it sits directly under a rule
        # just drawn, which already provides the seam.
        if text.strip():
            self.ui.separate()
        self.restart_silent_batches()
        # The rule opens the text, so the distance check runs before it is drawn; the blank line
        # above already counts, the text's own rows count toward the next rule.
        if self.ui.rule_due(self.MIN_ROWS_BETWEEN_RULES):
            self.ui.emit_phase_rule()
        self.ui.emit_answer(text, rule=False, indent=TurnBox.CONTENT_LEVEL)

    def emit_final_answer(self, text: str) -> None:
        """The turn's final answer: the one block of model text the turn-end rule closes, so it
        takes no phase rule of its own."""
        if text.strip():
            self.ui.separate()
        self.ui.emit_answer(text, rule=False, indent=TurnBox.CONTENT_LEVEL)

    def user_turn_rule(self) -> None:
        """Open a live or restored turn with one separator below the user's message."""
        self.restart_silent_batches()
        self.ui.emit_phase_rule()

    def restart_silent_batches(self) -> None:
        """The agent spoke (narration, a voiced batch, a new user turn): the silent run starts over."""
        self._silent_batches = 0

    def count_silent_batch(self) -> None:
        """Count one tool batch that carried no narration, and draw the batch rule once the run
        has earned a seam.

        Both conditions, for the reason the narration rule checks the distance: the batch count
        says the silence is long enough to be worth closing, the distance says a rule this close
        to the one above would part nothing. A run of one-line calls is four rows, not a stretch.
        The count keeps running when the rule is held back, so the seam arrives on the batch that
        finally clears the distance. Held here rather than at each call site, so the live turn and
        the resumed transcript cannot drift into drawing the seam by different rules."""
        self._silent_batches += 1
        if self._silent_batches >= self.TOOL_RUN_RULE_BATCHES and self.ui.rule_due(self.MIN_ROWS_BETWEEN_RULES):
            self.ui.emit_phase_rule()
            self._silent_batches = 0

    def tool_batch_output(self, silent: bool) -> None:
        """Close a run of tool calls that has gone on long enough without the agent saying
        anything -- the model not recovering is exactly when the transcript needs the seam most,
        because nothing else is about to provide one. Fires once per batch, after the batch's
        output is out, so it can never cut a batch in half. `silent` is whether the batch carried
        no narration; a batch that spoke restarts nothing and counts nothing."""

        def output() -> None:
            if silent:
                self.count_silent_batch()

        self.with_status_paused(output)

    def worker_answer_output(self, text: str) -> None:
        """The worker's interim and final model text, rendered like an agent answer (markdown) rather than the
        plain log lines tool execution prints as."""
        self.with_status_paused(lambda: self.emit_narration(text))

    def _begin_cli_preview(self) -> None:
        """Pause the status bar if running and start the CLI Bash live-preview line."""
        self.live_status_paused = self.status_bar.is_running()
        if self.live_status_paused:
            self.status_bar.stop()
        self.live_preview.start()

    def tool_live_start(self, budget: float | None = None) -> None:
        if not self.ui.color:
            return
        if self.tui is not None:
            with self.live_preview.lock:
                self.live_preview.active = True
                self.live_preview.text = ""
                self.live_preview.started_at = time.monotonic()
                self.live_preview.deadline = (time.monotonic() + budget) if budget else None
            self.tui.invalidate()
            return
        self._begin_cli_preview()
        # The preview region is reused across calls, so a call without a budget (Bash) must clear
        # any deadline a previous Job wait left behind.
        with self.live_preview.lock:
            self.live_preview.deadline = (time.monotonic() + budget) if budget else None

    def tool_live_output(self, _stream: str, text: str) -> None:
        if not self.ui.color:
            return
        if self.tui is not None:
            with self.live_preview.lock:
                if text:
                    self.live_preview.active = True
                    self.live_preview.text = (self.live_preview.text + text)[-self.live_preview.MAX_CHARS :]
                else:
                    self.live_preview.active = False
                    self.live_preview.text = ""
            self.tui.invalidate()
            return
        if text:
            if not self.live_preview.active:
                self._begin_cli_preview()
            self.live_preview.update(text)
            return
        if self.live_preview.active:
            self.live_preview.finish()
        if self.live_status_paused:
            self.status_bar.start(reset=False)
            self.live_status_paused = False
