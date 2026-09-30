"""Input, slash-command dispatch and turn coordination for both frontends."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import BackgroundServices, close_agent_resources
from wizolt.base import (
    LogBlock,
    MalformedToolCallError,
    TurnBox,
    WizoltError,
    __version__,
    run_blocking,
)
from wizolt.image import UserInput
from wizolt.session import QueuedInput, SessionLease, SessionSnapshotStore
from wizolt.tools.delegate import worker_provider_config
from wizolt.ui.cli import worker
from wizolt.ui.cli.commands import COMMAND_LOOKUP, COMMAND_NAMES, QUEUED_SUBCOMMANDS
from wizolt.ui.cli.modals import approval_text_viewer, question_interaction
from wizolt.ui.cli.presentation import Presentation
from wizolt.ui.cli.resume import ResumeRenderer
from wizolt.ui.cli.runtime import TuiRuntime
from wizolt.ui.cli.update import UpdateChecker
from wizolt.ui.cli.view import CommandCompleter, View
from wizolt.ui.render import InputStyle, Theme, UiPrinter, search_sources_footer


class CommandLoop:
    """Own session behavior: read input, dispatch commands, drive turns, and route output.

    Slash commands are handled here and never reach the model. The agent and prompt-toolkit share
    the runtime loop; completed user, assistant, and tool output goes to native scrollback, while
    drafts, previews, queue state, and selectors belong to the TUI. Anything transient the terminal
    leaves in scrollback is an artifact, not history — the transcript is always rebuilt from
    semantic records.

    Input entered mid-turn is queued, and only an allowlist of read-only commands may run against a
    busy session; anything that mutates configuration would change the meaning of a turn already in
    flight.

    The same object serves the non-interactive path, where there is no TUI and input and output are
    plain callables — which is also how the tests drive it.
    """

    HUNK_HEADER_RE: ClassVar[re.Pattern] = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
    EDITOR_CONTEXT_MAX_LINES: ClassVar[int] = 200
    EDITOR_CONTEXT_ELLIPSIS: ClassVar[str] = "# [... earlier lines of this reply omitted ...]"
    EDITOR_CONTEXT_SEPARATOR: ClassVar[str] = "# --- (earlier reply) ---"
    INPUT_HISTORY_BYTES: ClassVar[int] = 512 * 1024
    DIFF_MAX_BYTES: ClassVar[int] = 50_000
    DIFF_MAX_LINES: ClassVar[int] = 1_200

    @classmethod
    def bounded_diff(cls, text: str) -> tuple[str, bool]:
        if len(text.encode("utf-8")) <= cls.DIFF_MAX_BYTES and text.count("\n") <= cls.DIFF_MAX_LINES:
            return text, False
        clipped: list[str] = []
        length = 0
        for line in text.splitlines():
            line_bytes = len(line.encode("utf-8")) + 1
            if length + line_bytes > cls.DIFF_MAX_BYTES or len(clipped) >= cls.DIFF_MAX_LINES:
                break
            clipped.append(line)
            length += line_bytes
        return "\n".join(clipped), True

    @staticmethod
    def diff_counts(text: str) -> tuple[int, int]:
        added = removed = 0
        old_remaining = new_remaining = 0
        for line in text.splitlines():
            if match := CommandLoop.HUNK_HEADER_RE.match(line):
                old_remaining = int(match.group(1) or 1)
                new_remaining = int(match.group(2) or 1)
            elif line.startswith("+") and new_remaining:
                added += 1
                new_remaining -= 1
            elif line.startswith("-") and old_remaining:
                removed += 1
                old_remaining -= 1
            elif line.startswith(" "):
                old_remaining = max(0, old_remaining - 1)
                new_remaining = max(0, new_remaining - 1)
        return added, removed

    def __init__(self, agent: Agent, input_fn=input, output_fn=print):
        self.agent = agent
        self.session = agent.session
        self.presentation = Presentation(self.session, output_fn)
        self.background = BackgroundServices(self.session, self.presentation.emit_background)
        self.view = View(self.session, self.presentation)
        self.resume = ResumeRenderer(self.session, self.presentation, lambda: self.agent.context.update_current_tokens(self.session.system_prompt))
        self.input_fn = input_fn
        self.preprinted_output = ""
        # What `configure_theme` could not use, reported by `start_session` after the banner.
        self.theme_problems: list[str] = []
        # The CLI's import thread; TuiRuntime starts it after the first frame and keeps `starting`
        # set until it and the first mention scan finish: both compete with typing for the GIL.
        self.startup_warmup: threading.Thread | None = None
        # Set to the uid this run should hand over to. `main` reads it after run() returns and
        # builds the next CommandLoop around that session. The reserved lease travels with it, so
        # the target is already owned when the next run opens it -- never release-and-reacquire.
        self.resume_request = ""
        self.resume_lease: SessionLease | None = None
        self.interactive_input = input_fn is input and sys.stdin.isatty()
        # Bytes already read from the default non-TTY stdin after the first newline. Keeping the
        # remainder here lets the loop use non-blocking os.read() without losing a following line.
        self._stdin_buffer = bytearray()
        if self.interactive_input:
            history_path = self.session.data_path("history.txt")
            os.makedirs(os.path.dirname(history_path), exist_ok=True)
            self.trim_input_history(history_path)
            self.input_history = FileHistory(history_path)
        else:
            self.input_history = None
        self.input_completer = CommandCompleter(
            providers=lambda: tuple(sorted(self.session.config.providers)),
            models=lambda: self.session.config.provider.available_models,
            reasoning_choices=lambda: self.session.policy.reasoning_choices(self.session.config.provider),
            worker_reasoning_choices=lambda: self.session.policy.reasoning_choices(
                worker_provider_config(
                    self.session.config,
                    self.session.config.worker_provider or self.session.config.active_provider,
                )
            ),
            worker_models=lambda: tuple(
                dict.fromkeys(
                    (*self.session.config.providers[self.session.config.worker_provider or self.session.config.active_provider].available_models, "default")
                )
            ),
            mcp_servers=lambda: tuple(config.name for config in self.session.mcp.parse_configs()) if self.session.mcp else (),
            mcp_connected_servers=lambda: (
                tuple(config.name for config in self.session.mcp.parse_configs() if self.session.mcp.connected(config.name)) if self.session.mcp else ()
            ),
            mcp_tools=lambda server: tuple(tool.name for tool in self.session.mcp.tools.get(server, [])) if self.session.mcp else (),
            skills=lambda: tuple(skill.name for skill in self.session.skills.all()) if self.session.skills else (),
            skill_source=lambda name: skill.source if self.session.skills and (skill := self.session.skills.get(name)) else "",
            skill_commands=lambda: tuple((skill.name, skill.argument_hint) for skill in self.session.skills.commands()) if self.session.skills else (),
            file_matches=self.session.mentions.cached_matches if self.session.mentions else None,
            agents_rows=lambda: self.session.agents.menu_rows() if self.session.agents else [],
        )
        self.agent.output_fn = self.presentation.agent_output
        self.agent.final_output_fn = self.presentation.agent_answer_output
        self.agent.tools.output_fn = self.presentation.tool_output
        self.agent.tools.input_fn = self.tool_input
        # Everything the loop shows for a turn it did not print itself goes through one object, so
        # a delegated worker can be handed the same seam (see wizolt.agent.hooks and delegate.py).
        hooks = self.agent.hooks
        hooks.on_stream = self.presentation.model_stream_output
        hooks.on_builtin_call = self.presentation.builtin_call_output
        hooks.on_queue_flush = self.flush_queued_to_log
        hooks.on_compaction = self.presentation.automatic_compaction_status
        hooks.on_retry_wait = self.presentation.model_retry_wait_status
        hooks.on_image_route_notice = self.presentation.image_route_notice
        hooks.on_context_reset = self.presentation.context_reset_notice
        hooks.on_tool_batch = self.presentation.tool_batch_output
        hooks.live_start = self.presentation.tool_live_start
        hooks.live_output = self.presentation.tool_live_output
        hooks.question_fn = lambda specs: question_interaction(self, specs)
        hooks.worker_rule = self.presentation.ui.emit_worker_rule
        hooks.worker_answer = self.presentation.worker_answer_output
        hooks.worker_config_picker = worker.WorkerFlow(self).run_worker_config
        hooks.text_viewer = lambda view: approval_text_viewer(self, view)
        hooks.approval_form = self.set_approval_form
        hooks.cancel_input = self.cancel_tool_input
        hooks.script_status = self.presentation.toolscript_run_status

    @classmethod
    def trim_input_history(cls, path: str) -> None:
        """Bound the input history file, which prompt_toolkit only ever appends to.

        Keeps the newest entries that fit in `INPUT_HISTORY_BYTES` and drops the rest. The cut is
        made at an entry header rather than at a byte offset, so what survives is always loadable:
        a header is written as "\n# <timestamp>\n" and content lines are "+"-prefixed, which is why
        a user line beginning with "#" cannot be mistaken for one. The replacement is atomic, so an
        interrupted trim cannot leave a truncated history behind, and every failure is ignored —
        recall is a convenience and must never keep the session from starting.
        """
        try:
            if os.path.getsize(path) <= cls.INPUT_HISTORY_BYTES:
                return
            with open(path, "rb") as file:
                file.seek(-cls.INPUT_HISTORY_BYTES, os.SEEK_END)
                tail = file.read()
            start = tail.find(b"\n# ")
            if start < 0:
                return  # a single entry larger than the budget; keep it rather than cut inside it
            temp = path + ".tmp"
            with open(temp, "wb") as file:
                file.write(tail[start + 1 :])
            os.replace(temp, path)
        except OSError:
            return

    def flush_queued_to_log(self, texts: list[str]) -> None:
        # Move flushed queued messages from the live activity region into terminal scrollback.
        texts = [text for text in texts if text.strip()]
        if not texts:
            return
        fragments: list[tuple[str, str]] = [("", "\n")]
        for i, text in enumerate(texts):
            if i:
                fragments.append(("", "\n"))
            fragments.extend([("class:prompt", UiPrinter.USER_LOG_PREFIX), (UiPrinter.user_log_style(), text), ("", "\n")])
        fragments.append(("", "\n"))
        print_formatted_text(FormattedText(fragments), style=self.view.style(), end="", flush=True)

    def editor_context(self) -> str:
        """The agent's recent replies, newest first, restated as read-only reference for the
        external editor (Ctrl-X Ctrl-E / Ctrl-G), accumulated under a line budget so the
        editor's temp file stays small."""
        parts: list[str] = []
        for message in reversed(self.session.messages):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            lines = content.strip().splitlines()
            if len(lines) > self.EDITOR_CONTEXT_MAX_LINES:
                # Keep the newest lines and say so: a headless reply that reads as complete is
                # worse reference than a shorter one that admits where it was cut.
                drop = len(lines) - self.EDITOR_CONTEXT_MAX_LINES + 1
                lines = [self.EDITOR_CONTEXT_ELLIPSIS] + lines[drop:]
            if parts and len(parts) + 1 + len(lines) > self.EDITOR_CONTEXT_MAX_LINES:
                break  # an earlier reply would push the line budget; it adds little recent context
            if parts:
                parts.append(self.EDITOR_CONTEXT_SEPARATOR)
            parts.extend(lines)
        if not parts:
            return ""
        return "\n".join(parts)

    def skill_command(self, text: str) -> bool:
        """True when `text` starts a skill with `/name` rather than naming a built-in command:
        built-ins win a name clash, and the skill stays reachable as `$name`."""
        return text.partition(" ")[0].partition("\n")[0] not in COMMAND_NAMES and bool(self.session.skills and self.session.skills.command(text))

    async def run_queued_command(self, text: str) -> None:
        """Dispatch a read-only slash command while an agent turn is running."""
        name = text.partition(" ")[0]
        entry = COMMAND_LOOKUP.get(name)
        if entry is None or not entry.queue_safe:
            self.presentation.emit_turn(f"{name} is unavailable while the agent is working; press Ctrl-C to run it.")
            return
        allowed, refusal = QUEUED_SUBCOMMANDS.get(name, (frozenset(), ""))
        sub = text.partition(" ")[2].split()
        if allowed and sub and sub[0] not in allowed:
            self.presentation.emit_turn(refusal)
            return
        await self.command(text)

    def take_pending_inputs(self) -> list[UserInput]:
        """Remove and return the queued inputs that open the next turn.

        A held-back input (`QueuedInput.next_turn`) starts a turn of its own, so it is taken one at a
        time: later held inputs stay queued for the following boundaries. Plain follow-ups still
        leave together."""
        taken: list[QueuedInput] = []
        for item in self.session.pending_user_inputs:
            if item.inflight:
                continue
            if taken and item.next_turn:
                break  # hold it for the following boundary
            taken.append(item)
            if item.next_turn:
                break
        self.session.pending_user_inputs = [item for item in self.session.pending_user_inputs if item not in taken]
        return [item.user_input() for item in taken]

    def recall_pending_input(self, on_inflight: Callable[[], None]) -> str | UserInput:
        """Move the newest queued input back to the editor, retrying if it was already claimed.

        The mutation only; persisting it is the caller's, because this runs inside a prompt-toolkit
        key handler that has to answer with the recalled text and cannot await a file write."""

        item = next(reversed(self.session.pending_user_inputs), None)
        if item is None:
            return ""
        self.session.pending_user_inputs.remove(item)
        was_inflight = item.inflight
        if was_inflight:
            for pending_item in self.session.pending_user_inputs:
                pending_item.inflight = False
        if was_inflight:
            on_inflight()
        self.session.images.retain(item.images)
        return item.user_input()

    def run(self, *, show_banner: bool = True) -> int:
        """Synchronous entry point for the CLI. Both frontends run on one loop from here."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("CommandLoop.run() cannot be called from a running event loop; await the frontend coroutine")
        return asyncio.run(self._run_frontend(show_banner=show_banner))

    async def _run_frontend(self, *, show_banner: bool = True) -> int:
        """Select the frontend inside the CLI's single event-loop entry."""
        # Embedded frontends can execute slash commands before Agent.run acquires ownership.
        self.session.ensure_ownership()
        self.configure_theme()
        if self.interactive_input:
            return await TuiRuntime(self).run(show_banner=show_banner)
        return await self.run_simple(show_banner=show_banner)

    async def run_simple(self, *, show_banner: bool = True) -> int:
        """The non-TTY frontend on the CLI-owned loop.

        The same loop as the TUI frontend gives the turn: one owner for startup discovery, the
        model client, and MCP, so everything this session opened is closed before it closes."""

        self.session.next_hints_available = False  # the simple REPL has no chip UI; don't offer an invisible terminal tool
        self.background.open_background()
        self.start_session(show_banner=show_banner)
        discovery = asyncio.ensure_future(self.discover_mcp())
        try:
            # This frontend has no TUI settle task. Scan before even a local command can
            # freeze the model's listing (for example through /status).
            if self.session.skills is not None:
                await run_blocking(self.session.skills.reload)
            await self.agent.start_session()
            return await self._simple_loop()
        finally:
            discovery.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await discovery
            await self.background.close_background()
            await close_agent_resources(self.agent, reason="resume" if self.resume_request else "prompt_input_exit")

    async def _simple_loop(self) -> int:
        while True:
            try:
                entered = self.take_pending_inputs()
                initial_input = UserInput(
                    "\n".join(str(item) for item in entered),
                    tuple(image for item in entered for image in item.images),
                )
                user_input = await self.read_input(initial_text=initial_input)
            except EOFError:
                self.presentation.emit(TurnBox.SEPARATOR)
                await self.resume.save_and_emit_resume()
                return 0
            except KeyboardInterrupt:
                continue
            if not user_input.strip():
                continue
            handled, exit_now = await self.command(user_input.strip())
            if exit_now:
                return 0
            if handled:
                continue
            self.presentation.user_turn_rule()
            started = time.monotonic()
            malformed_tool_call = False
            answered = False
            try:
                self.presentation.status_bar.start()
                try:
                    await self.agent.run(user_input)
                    answered = True
                except (asyncio.CancelledError, KeyboardInterrupt):
                    self.presentation.emit_turn("Cancelled")
                    continue
                except MalformedToolCallError as error:
                    answer = str(error)
                    malformed_tool_call = True
                except WizoltError as error:
                    answer = f"Error: {error}"
            finally:
                self.presentation.status_bar.stop()
            # Same rule as TuiRuntime.run_agent_turn: the engine publishes its own final answer
            # through output_fn, so only an error it raised before publishing prints here.
            if not answered:
                if answer.strip():
                    self.presentation.ui.separate()
                self.presentation.ui.emit_answer(answer, rule=False, indent=TurnBox.CONTENT_LEVEL)
            if footer := search_sources_footer(self.agent.turn_sources):
                self.presentation.ui.emit_answer(footer, rule=False, indent=TurnBox.CONTENT_LEVEL)
            if not malformed_tool_call:
                self.presentation.ui.emit_turn_end(started)
            await self.session.save_snapshot()

    async def discover_mcp(self) -> None:
        """Connect the auto_connect servers, as a task the caller's runtime owns.

        A task, not a wait: an unreachable server must not hold the prompt, and the tools index
        picks servers up as they connect. It is owned rather than detached so shutdown can cancel
        it -- a discovery still opening clients when the loop closes is exactly the thing the
        private MCP thread used to hide."""

        mcp = self.session.mcp
        if mcp is not None:
            await mcp.discover_auto()

    def emit_banner(self) -> None:
        """Write the one static line that can safely precede interactive terminal setup."""
        self.presentation.emit(f"wizolt {__version__}. Type / for commands.")

    def configure_theme(self) -> None:
        """Activate the configured theme before either frontend draws anything.

        The TUI prints its banner and paints its first frame before `start_session` runs, so a
        theme set there showed the default palette first. What could not be used is kept for
        `start_session` to report after the banner.
        """
        self.theme_problems = Theme.configure(self.session.settings.theme, self.session.data_path("themes"), self.session.config.ui.get("themes"))
        self.theme_problems.extend(Theme.configure_diff_style(self.session.config.ui))
        if warning := Theme.true_color_warning():
            self.theme_problems.append(warning)
        self.theme_problems.extend(self.presentation.status_bar.layout.load(self.session.config.ui, Theme.bar_styles))
        self.presentation.input_style, problems = InputStyle.load(self.session.config.ui)
        self.theme_problems.extend(problems)

    def start_session(self, *, show_banner: bool = True) -> None:
        """Initialize output and background services shared by both command-loop frontends."""
        if show_banner:
            self.emit_banner()
        for problem in self.theme_problems:
            self.presentation.emit(problem)
        self.theme_problems = []
        # Cached state is read synchronously -- it is small, local, and the first status display
        # needs it -- and only the remote half is scheduled. Nothing here may hold the prompt: a
        # slow index, a slow filesystem, or an unreachable PyPI is not a reason to wait to type.
        checker = UpdateChecker(self.session.data_path(), self.presentation.update)
        update_due = checker.load_cached()
        if self.presentation.update.newer_than(__version__):
            self.presentation.emit(
                f"update available: {__version__} -> {self.presentation.update.latest}. upgrade with `{' '.join(UpdateChecker.upgrade_command())}`."
            )
        self.resume.render_resumed_session()
        if update_due:
            self.background.spawn_background(checker.check(), name="update-check")
        self.background.spawn_background(self.clean_expired_sessions(), name="session-cleanup")
        # The provider catalog refresh runs off the startup path after the first screen, gated to
        # once per 72h (see sync.CatalogRuntime); a failure only shows through /catalog.
        catalog = self.session.catalog
        if catalog is not None and catalog.refresh_due():
            self.background.spawn_background(catalog.refresh(), name="catalog-refresh")

    async def clean_expired_sessions(self) -> None:
        """Run the retention sweep off the startup path: on a network filesystem it can cost
        seconds before the prompt accepts a keystroke, and nothing depends on it having run first.

        The traversal and the deletions are one blocking pass. Cancelling this waits for that pass
        to finish rather than abandoning it half-deleted -- retention removes unrecoverable work,
        so the one thing it may not do is stop in the middle. The notice is emitted here, on the
        loop, once the pass has returned its count."""

        data_dir = self.session.config.data_dir
        current_uid = self.session.uid
        days = self.session.settings.session_retention_days
        with contextlib.suppress(Exception):
            removed = await run_blocking(lambda: SessionSnapshotStore.clean_expired(data_dir, current_uid, days))
            if removed:
                self.presentation.emit_background(self.expired_sessions_notice(removed))

    def expired_sessions_notice(self, removed: int) -> str:
        """Word the retention notice: retention removes unrecoverable work, so report it rather
        than deleting silently, and name the setting that controls it."""
        days = self.session.settings.session_retention_days
        sessions = "session" if removed == 1 else "sessions"
        return f"removed {removed} saved {sessions} inactive for over {days} {'day' if days == 1 else 'days'} (runtime.session_retention_days)"

    def read_input_sync(self, prompt_text: str | None = None) -> str:
        """Read from the injected/non-TTY input path; interactive terminals use TuiApp."""
        return self.input_fn(self.presentation.input_style.prefix() if prompt_text is None else prompt_text)

    async def invoke_input(self, action: Callable[[], Any]) -> Any:
        """Run an injected synchronous input callback without owning its blocking lifetime.

        Python cannot cancel an arbitrary callback. A daemon adapter lets cancellation release the
        CLI runtime immediately; the embedding still owns unblocking its callback if it wants the
        thread itself to finish. The default executor cannot be used here because `asyncio.run()`
        waits for that executor during shutdown.
        """

        loop = asyncio.get_running_loop()
        result: asyncio.Future[Any] = loop.create_future()

        def publish(value: Any = None, error: BaseException | None = None) -> None:
            if result.done():
                return
            if error is None:
                result.set_result(value)
            else:
                result.set_exception(error)

        def invoke() -> None:
            try:
                value, error = action(), None
            except BaseException as caught:  # noqa: BLE001 - reproduce the callback's outcome on its caller.
                value, error = None, caught
            with contextlib.suppress(RuntimeError):  # the cancelled runtime may already be closed.
                loop.call_soon_threadsafe(publish, value, error)

        threading.Thread(target=invoke, name="input-callback", daemon=True).start()
        return await result

    async def read_input(
        self,
        prompt_text: str | None = None,
        *,
        initial_text: str | UserInput = "",
    ) -> str | UserInput:
        """Read one non-TTY line without parking the default executor on POSIX stdin.

        An injected synchronous reader remains an embedding boundary and runs through a daemon
        adapter; its owner is responsible for unblocking it. The process stdin path is driven
        directly by fd readiness.
        """

        if initial_text:
            return initial_text
        if prompt_text is None:
            prompt_text = self.presentation.input_style.prefix()
        if self.input_fn is not input:
            return await self.invoke_input(lambda: self.read_input_sync(prompt_text))

        if prompt_text:
            sys.stdout.write(prompt_text)
            sys.stdout.flush()

        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        result: asyncio.Future[bytes] = loop.create_future()
        was_blocking = os.get_blocking(fd)

        def finish_line() -> bool:
            newline = self._stdin_buffer.find(b"\n")
            if newline < 0:
                return False
            line = bytes(self._stdin_buffer[:newline])
            del self._stdin_buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not result.done():
                result.set_result(line)
            return True

        def readable() -> None:
            if finish_line():
                return
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                return
            except OSError as error:
                if not result.done():
                    result.set_exception(error)
                return
            if chunk:
                self._stdin_buffer.extend(chunk)
                finish_line()
                return
            if self._stdin_buffer:
                line = bytes(self._stdin_buffer)
                self._stdin_buffer.clear()
                if not result.done():
                    result.set_result(line)
            elif not result.done():
                result.set_exception(EOFError())

        reader_added = False
        os.set_blocking(fd, False)
        try:
            loop.add_reader(fd, readable)
            reader_added = True
            readable()
            raw = await result
        finally:
            try:
                if reader_added:
                    loop.remove_reader(fd)
            finally:
                os.set_blocking(fd, was_blocking)
        return raw.decode(sys.stdin.encoding or "utf-8", errors=sys.stdin.errors or "strict")

    def set_approval_form(self, actions: list[tuple[str, str]]) -> bool:
        # The selectable action row exists only in the TUI. Headless and piped runs report False so
        # the approval brief keeps advertising the typed protocol they do have.
        return self.presentation.tui is not None and self.presentation.tui.set_approval_form(actions)

    def cancel_tool_input(self) -> None:
        """Resolve a pending approval or Ask prompt as cancelled, so whoever is parked on the user
        returns. Headless runs have no prompt to resolve; their injected input owns its own end."""
        if self.presentation.tui is not None:
            self.presentation.tui.cancel_input()

    async def tool_input(self, prompt: str = "") -> str | None:
        """Await one line of user input for a tool: an approval, an Ask free-text page.

        Under the TUI this is the application's own input row -- prompt-toolkit does not nest, so a
        second application is not an option -- awaited on the loop that runs it. None propagates the
        TUI's cancel signal.

        Without a TUI the injected `input_fn` blocks, so it runs through the same daemon adapter as
        non-TTY input. That contract belongs to whoever injected it: it should still be unblocked,
        but cancellation never holds the CLI runtime open around it."""

        if self.presentation.tui is not None:
            return await self.presentation.tui.request_input(prompt)
        if self.input_fn is input:
            return await self.read_input(prompt)
        return await self.invoke_input(lambda: self.presentation.with_status_paused(lambda: self.input_fn(prompt)))

    # How close a phase rule may come to the one above it, in rendered rows. Under this the rule
    # is skipped: two rules a few rows apart part nothing, they just add lines to what is already
    # a short stretch. The agent saying two things in quick succession is one phase, not two.
    # How many tool batches in a row the agent can work in silence before a phase rule closes the
    # stretch. Rendered rows would punish one big output and reward many small ones; what matters
    # is that the model keeps calling tools without ever saying anything back, so the count is of
    # batches, not lines. Fired after the batch's output is out, so a batch is never cut in half.

    async def command(self, text: str) -> tuple[bool, bool]:
        """Dispatch one slash command, on the loop that owns this session.

        A handler that needs the network -- `/compact` is the one -- is a coroutine and is awaited
        here, so its request lives on the same loop as everything else the session opened. Every
        other handler is local and bounded, and runs directly."""

        if text in {"/exit", "/quit", "exit", "quit"}:
            await self.resume.save_and_emit_resume()
            return True, True
        if not text.startswith("/"):
            return False, False
        name, _, args = text.partition(" ")
        entry = COMMAND_LOOKUP.get(name)
        if self.skill_command(text):
            return False, False  # a turn, which loads the skill (see Agent.skill_command)
        output = entry.handler(self, args.strip()) if entry else f"Unknown command: {name}"
        if inspect.isawaitable(output):
            output = await output
        # None means the handler already rendered its own UI (e.g. /diff's viewer).
        if output is not None:
            if isinstance(output, LogBlock):
                self.presentation.emit(output)
            elif entry is not None and entry.render == "compact":
                self.presentation.ui.emit_answer(output, rule=False, compact=True, indent=TurnBox.CONTENT_LEVEL)
            elif entry is not None and entry.render == "answer":
                self.presentation.ui.emit_answer(output, indent=TurnBox.CONTENT_LEVEL)
            else:
                self.presentation.emit_turn(output)
        # A session switch ends this run the way /exit does; `main` starts the next one.
        return True, bool(self.resume_request)
