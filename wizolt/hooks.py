"""The one presentation seam between the engine layers and whoever renders them.

Agent, ContextManager, ModelClient, and ToolRunner never render: they report, and the CLI
answers. Every field here is that answer, optional because a headless embedding (a library
call, a piped run) has none of it. `None` is not "no-op": several fields switch the owning
layer to a different fallback, which is documented on each one.

One instance is shared by an agent's model, context, and tools, so wiring is one object, and
a delegated worker is handed a new instance built from the parent's fields in one place
(`delegate._wire_worker_agent`). Fields are grouped by the layer that reads them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from wizolt.base import ApprovalView, ImageRouteNotice

# The Ask tool's question set, as (label, ...) specs, and the answers it gets back.
QuestionFn = Callable[[list[Any]], Awaitable[list[str]]]


@dataclass
class UiHooks:
    """Live presentation callbacks; every field defaults to the layer's own behavior."""

    # -- Agent: what the turn reports about itself, once per turn or per batch.
    # One gray block per image-routing decision: a reason root line plus an optional described-by
    # child. Presentation only, never model context. None shows nothing.
    on_image_route_notice: Callable[[ImageRouteNotice], None] | None = None
    # A deterministic context trim happened. None shows nothing.
    on_context_reset: Callable[[str], None] | None = None
    # Fired once after each tool batch's output is out, with whether the batch carried text.
    # A fact, not a decision: whether that boundary is worth drawing anything is the view's to
    # judge. Never model context. None shows nothing.
    on_tool_batch: Callable[[bool], None] | None = None
    # Awaited between a promoted response and the tool batch under it, so the answer is already
    # permanent scrollback when the batch writes. None means there is no queue to be ahead of.
    output_barrier: Callable[[], Awaitable[None]] | None = None
    # Called with the queued messages when they are flushed into the turn, so the UI can move
    # them out of the live queue region. None leaves them where they are.
    on_queue_flush: Callable[[list[str]], None] | None = None

    # -- ModelClient: what a request reports while it runs.
    # Streamed (kind, delta) chunks. None disables streaming entirely -- not a silent sink.
    on_stream: Callable[[str, str], None] | None = None
    # A tool the provider ran for itself, as (label, detail). None logs nothing.
    on_builtin_call: Callable[[str, str], None] | None = None
    # True while a retry backoff wait is in flight, so the divider can say so.
    on_retry_wait: Callable[[bool], None] | None = None

    # -- ContextManager: what a projection reports.
    # (active, error) around an automatic compaction. None shows nothing.
    on_compaction: Callable[[bool, str], None] | None = None

    # -- ToolRunner and the tools it runs: the call-time seams.
    # A live one-line preview of a running command: start(budget) then output(stream, text).
    # None degrades to no preview.
    live_start: Callable[..., None] | None = None
    live_output: Callable[[str, str], None] | None = None
    # The visual rule that opens a delegated worker's block, labelled with a one-line order
    # summary. None falls back to the plain "[worker]" root line.
    worker_rule: Callable[[str], None] | None = None
    worker_answer: Callable[[str], None] | None = None
    # Ask's question set. None falls back to the runner's input_fn, which is what a headless
    # embedding wants; a presenter that can show a selector sets it.
    question_fn: QuestionFn | None = None
    # The Delegate confirm-time `c` config loop, through the shared choice selector (see
    # CommandLoop.run_worker_config). None degrades `c` to printing the config only.
    worker_config_picker: Callable[[], Awaitable[None] | None] | None = None
    # A read-only viewer for the text behind a confirmation (a Delegate order, a ToolScript
    # body) for the confirm-time `v`/`view` key (see cli.modals.approval_text_viewer). None
    # degrades `v` to printing the whole text; the viewer's close signal is the return value.
    text_viewer: Callable[[ApprovalView], Awaitable[object] | object] | None = None
    # The next approval prompt's actions as a selectable row (see TuiApp.set_approval_form).
    # None, or a False return, means the answer has to be typed -- headless runs, piped stdin.
    approval_form: Callable[[list[tuple[str, str]]], bool] | None = None
    # The post-turn code-index freshness pass. None (headless, or a runner outside CommandLoop)
    # simply means no check is scheduled.
    index_freshness: Callable[[], None] | None = None
    # A ToolScript body is the one stretch where nothing streams and no single tool line is
    # pending, so the divider would sit on "working" for the whole batch. The running source
    # rides along so Ctrl-O can offer the script while it runs. None degrades to no phase label.
    script_status: Callable[[bool, str], None] | None = None
    # Resolves a pending approval/Ask prompt with "cancelled", so a worker parked on the user can
    # be unblocked when the turn is cancelled. None (headless, piped stdin) leaves the injected
    # input function to own its own unblocking.
    cancel_input: Callable[[], None] | None = None
