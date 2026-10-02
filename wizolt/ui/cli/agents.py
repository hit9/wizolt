"""Agent selection: each agent retains its runtime, input buffer and transcript."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.styles import DynamicStyle
from prompt_toolkit.utils import get_cwidth

from wizolt.agent.engine import Agent
from wizolt.agent.subagents import AgentEntry
from wizolt.base import ApprovalView, Text, run_blocking
from wizolt.session import QueuedInput, Session, SessionSnapshotStore
from wizolt.ui.cli.modals import approval_text_viewer, choice_application, picker_height, select_choice
from wizolt.ui.cli.runtime import ScrollbackWriter, TuiRuntime
from wizolt.ui.render import ActivityPulse, InputStyle, StatusBar, Theme

if TYPE_CHECKING:
    from wizolt.ui.cli.loop import CommandLoop
    from wizolt.ui.cli.presentation import Presentation


@dataclass
class ModelSettingsEditor:
    """A draft model configuration using the caller's terminal, without its turn machinery."""

    session: Session
    presentation: Presentation
    interactive_input: bool


@dataclass(frozen=True)
class AgentPreview:
    """A compact reading aid; entering the agent opens its complete conversation.

    Wrap by terminal cells before bounding physical rows. A character limit alone lets CJK
    or long paragraphs overrun the modal; preview chrome must leave room for navigation.
    """

    task: str
    reply: str
    streaming: bool = False
    name: str = ""

    def fragments(self, width: int, height: int) -> StyleAndTextTuples:
        if height < 5 or width < 20:
            return []
        width, height = min(100, width), min(16, height)
        roomy = width >= 40 and height >= 10
        chrome = 7 if roomy else 3
        task_rows = min(3, max(1, (height - chrome) // 3))
        reply_rows = height - chrome - task_rows
        caption = Text.clip_width("─ " + self.name + " ", width - 4)
        parts: StyleAndTextTuples = [
            ("class:rule", "  ┌"),
            ("class:accent bold", caption),
            ("class:rule", "─" * (width - 4 - get_cwidth(caption)) + "┐\n"),
        ]

        def bordered(row: list[tuple[str, str]]) -> None:
            used = sum(get_cwidth(text) for _, text in row)
            parts.extend([*row, ("class:rule", " " * max(0, width - 2 - used) + " │\n")])

        if roomy:
            bordered([("class:rule", "  │")])
        for title, text, budget, tail in (
            ("Task", self.task[:1500] or "(no task yet)", task_rows, False),
            ("Live" if self.streaming else "Reply", self.reply[-3000:] or "(no reply yet)", reply_rows, True),
        ):
            if tail:
                bordered([("class:rule", "  │")])
            rows: list[list[tuple[str, str]]] = []
            if roomy:
                bordered([("class:rule", "  │ "), ("class:choice.disabled bold", title)])
            prefix = [("class:rule", "  │ "), ("", "  ")] if roomy else [("class:rule", "  │ "), ("class:choice.disabled bold", f"{title:<5} ")]
            continuation = [("class:rule", "  │ "), ("", "  " if roomy else "      ")]
            body_width = width - 4 - sum(get_cwidth(value) for _, value in prefix)
            for raw in text.splitlines():
                rows.extend(Text.wrap_styled(prefix if not rows else continuation, continuation, [("class:text", raw)], width - 2))
            if len(rows) > budget:
                # Keep the latest flowing rows and the opening task. The ellipsis stays on the
                # final/first retained row, so even a five-row pane still shows both sections.
                rows = rows[-budget:] if tail else rows[:budget]
                if tail:
                    body = "".join(value for _, value in rows[0][2:])
                    rows[0] = [*prefix, ("class:choice.disabled", "… "), ("class:text", Text.clip_width(body, body_width))]
                else:
                    body = "".join(value for _, value in rows[-1][2:])
                    rows[-1] = [*(prefix if budget == 1 else continuation), ("class:text", Text.clip_width(body, body_width + 1) + "…")]
            for row in rows:
                bordered(row)
        if roomy:
            bordered([("class:rule", "  │")])
        parts.append(("class:rule", "  └" + "─" * (width - 4) + "┘\n"))
        return parts


@dataclass(frozen=True)
class StopAgent:
    """Hand a requested confirmation out of the picker before opening the next modal."""

    uid: str


@dataclass(frozen=True)
class ArchiveAgent:
    uid: str


async def configure_subagent(loop: CommandLoop, settings: Session) -> None:
    """Reuse model commands against a call's detached settings and the caller's modal owner."""
    from wizolt.ui.cli import commands

    editor = ModelSettingsEditor(settings, loop.presentation, loop.interactive_input)
    while True:
        provider = settings.config.provider
        labels = {
            "provider": f"provider: {settings.config.active_provider}",
            "model": f"model: {provider.model or '(no model)'}",
            "effort": f"effort: {provider.reasoning}",
            "api": f"api: {provider.api}",
            "done": "done - return to the confirmation prompt",
        }
        choice = await select_choice(loop, "Subagent config", tuple(labels), labels=labels, current="done")
        if choice == "provider":
            result = await commands.provider(editor, "")
        elif choice == "model":
            result = await commands.model(editor, "")
        elif choice == "effort":
            result = await commands.reason(editor, "")
        elif choice == "api":
            result = await commands.api(editor, "")
        else:
            return
        loop.presentation.tool_output(result)


class AgentsFrontend:
    """Select a projection while each agent keeps its frontend and input destination.

    Switching changes only which application draws the terminal. Drafts, accepted submissions,
    modals, transcript writers and statistics stay attached to their original runtime. Shared
    ready/shutdown events describe the application lifetime, never one agent's turn lifetime.
    """

    def __init__(self, root: TuiRuntime):
        self.root = root
        group = root.loop.session.subagents
        assert group is not None
        self.group = group
        self.runtimes = {root.loop.session.uid: root}
        self.current = root
        self.switching = False
        root.tui.on_attention = lambda: self.notice(root.loop.agent, "waiting for input")
        root.loop.agents_frontend = self
        self.group.on_created = self.attach
        self.group.on_changed = self.changed
        self.group.on_archived = self.close_runtime
        self.group.archive_admission = self.archive_admission
        self.group.driver = self.drive

    async def attach(self, agent: Agent) -> None:
        from wizolt.ui.cli.loop import CommandLoop

        loop = CommandLoop(agent, input_fn=self.root.loop.input_fn, output_fn=self.root.loop.presentation.ui.output_fn)
        loop.interactive_input = self.root.loop.interactive_input
        loop.agents_frontend = self
        loop.presentation.input_style, loop.theme_problems = InputStyle.load(agent.session.config.ui)
        loop.theme_problems.extend(loop.presentation.status_bar.layout.load(agent.session.config.ui, Theme.bar_styles))
        runtime = TuiRuntime(loop)
        runtime.child_agent = True
        runtime.runtime_loop = self.root.runtime_loop
        runtime.accepting = True
        runtime.shutdown = self.root.shutdown
        runtime.application_ready = self.root.application_ready
        loop.presentation.tui = runtime.build_tui()
        runtime.tui.managed = True
        runtime.tui.on_attention = lambda: self.notice(agent, "waiting for input")
        # Replay can reject malformed stored presentation data. Finish it before publishing
        # this runtime or starting background consumers that would outlive a failed attach.
        if agent.session.resumed:
            loop.resume.render_resumed_session()
        loop.background.open_background()
        assert runtime.runtime_loop is not None
        writer = ScrollbackWriter(runtime.runtime_loop, runtime.tui.write_to_scrollback, loop.presentation.ui.write_direct)
        runtime.scrollback = writer
        loop.presentation.scrollback = writer
        loop.presentation.background_output_lock = writer.lock
        agent.hooks.output_barrier = writer.barrier
        runtime.submissions_task = runtime.spawn(runtime._consume_submissions(), name="agent-submissions")
        self.runtimes[agent.session.uid] = runtime

    def notice(self, agent: Agent, status: str) -> None:
        if self.current.loop.agent is not agent:
            label = f"{'agent' if agent is self.group.root else 'subagent'} [{agent.session.agent_name}]"
            if status in {"waiting for input", "completed", "failed"}:
                self.current.loop.presentation.agent_notice(label, status)
            else:
                self.current.loop.presentation.emit_turn(f"{label} {status}")
        self.current.tui.invalidate()

    def changed(self, entry: AgentEntry) -> None:
        self.notice(entry.agent, entry.status)

    async def drive(self, agent: Agent, queued: QueuedInput) -> None:
        runtime = self.runtimes[agent.session.uid]
        value = queued.user_input()
        try:
            if queued.commands:
                if await runtime.dispatch(value):
                    return
            else:
                runtime.loop.presentation.ui.emit_answer(value.display_text(), role="user", rule=False)
            await runtime.run_agent_turn(value)
        finally:
            # The inbox owns commands as well as model turns. Stopping this child from its
            # own picker cancels dispatch before it can restore the prompt; no model turn
            # was active in that case. Always settle the presentation when its driver exits.
            runtime.turn_active = False
            runtime.reset_turn()

    async def select(self, loop: CommandLoop) -> None:
        entries = tuple(self.group.entries.values())
        displayed = {entry.agent.session.uid: entry for entry in entries}
        archived = {item["uid"]: item for item in self.root.loop.session.subagent_entries if item.get("archived")}

        def elapsed(uid: str) -> str:
            seconds = int(displayed[uid].agent.session.state.elapsed)
            minutes, seconds = divmod(seconds, 60)
            return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"

        def label(uid: str) -> str:
            entry = displayed[uid]
            return (
                f"{entry.agent.session.agent_name} · {entry.status} · {elapsed(uid)} · "
                f"ctx {entry.agent.session.usage.context_percent(entry.agent.session.state.context_percent)}%"
                + (" (current)" if entry.agent is self.current.loop.agent else "")
            )

        labels = {entry.agent.session.uid: label(entry.agent.session.uid) for entry in entries}
        labels.update({uid: f"{item.get('name', uid)} · archived" for uid, item in archived.items()})

        def row(uid: str) -> StyleAndTextTuples:
            available = max(1, min(100, shutil.get_terminal_size((80, 24)).columns) - 8)
            wide = available >= 62
            status_width, marker_width = (17, 10) if wide else (11, 2)
            clock_width = 9 if available >= 72 else 0
            longest = max(
                get_cwidth(name) for name in [*(item.agent.session.agent_name for item in entries), *(item.get("name", "") for item in archived.values())]
            )
            name_width = min(max(8, longest), 28, max(4, available - status_width - marker_width - clock_width - 14))

            def cell(text: str, width: int) -> str:
                text = Text.clip_width(text, width)
                return text + " " * (width - get_cwidth(text))

            if uid in archived:
                text = "● " + cell(archived[uid].get("name", uid), name_width) + "  " + cell("archived", status_width)
                return [("class:muted", cell(text, available))]
            entry = displayed[uid]
            session = entry.agent.session
            state = entry.status if wide else entry.status.replace("waiting for input", "waiting")
            state_style = {"running": "class:accent", "waiting for input": "class:warning", "failed": "class:error"}.get(entry.status, "class:muted")
            context = session.usage.context_percent(session.state.context_percent)
            marker = (" (current)" if wide else " *") if entry.agent is self.current.loop.agent else ""
            # The menu redraws on the application's existing timer, even when main is idle.
            # Pulse the engine's running state (tools included), not just model streaming.
            activity = (
                ActivityPulse.fragments(time.monotonic())
                if entry.status == "running"
                else [("class:warning" if entry.status == "waiting for input" else "class:text", "● ")]
            )
            parts: StyleAndTextTuples = [
                *activity,
                ("class:text", cell(session.agent_name, name_width)),
                (state_style, "  " + cell(state, status_width)),
                ("class:muted", cell("  " + elapsed(uid), clock_width) if clock_width else ""),
                ("class:muted", f"  ctx {context:3d}%"),
                ("class:accent", cell(marker, marker_width)),
            ]
            parts.append(("", " " * max(0, available - sum(get_cwidth(fragment[1]) for fragment in parts))))
            return StatusBar.clip_fragments(parts, available)

        def preview(uid: str) -> StyleAndTextTuples:
            if uid in archived:
                item = archived[uid]
                return AgentPreview(item.get("instruction", ""), item.get("result", {}).get("text", ""), name=labels[uid]).fragments(
                    shutil.get_terminal_size((80, 24)).columns, picker_height() - 9
                )
            entry = displayed[uid]
            session = entry.agent.session
            opening = entry.instruction or next((str(message.get("content", "")) for message in session.messages if message.get("role") == "user"), "")
            runtime = self.runtimes.get(uid)
            active = runtime.loop.presentation.model_stream_text if runtime else ""
            height = picker_height() - 6 - min(3, len(entries))
            caption = f"{session.agent_name} · {entry.status} · {elapsed(uid)}"
            return AgentPreview(opening, active or entry.answer, bool(active), caption).fragments(shutil.get_terminal_size((80, 24)).columns, height)

        current = self.current.loop.session.uid
        while True:
            uid = await choice_application(
                loop,
                "Agents",
                tuple(labels),
                labels=labels,
                current=current,
                disabled=set(),
                preview_fn=preview,
                label_fn=row,
                preview_title=" ",
                actions={
                    "x": lambda key: StopAgent(key) if key in self.group.entries else None,
                    "X": lambda key: self.group.stop(key) if key in self.group.entries else None,
                    "d": lambda key: ArchiveAgent(key) if key in self.group.entries and key != self.root.loop.session.uid else None,
                },
                keys="↑/↓ j/k move · Enter open · x stop · X stop now · d archive · Esc back",
            )
            if isinstance(uid, ArchiveAgent):
                current = uid.uid
                name = self.group.entry(current).agent.session.agent_name
                if (
                    await select_choice(
                        loop,
                        f"Archive {name} and its children?",
                        ("archive", "back"),
                        labels={"archive": "Stop work and free slots; keep history", "back": "Back"},
                        current="back",
                    )
                    == "archive"
                ):
                    # The command itself may be running in the branch being retired. Switch
                    # first, then let a root-owned task join it before cancelling that branch.
                    owner = asyncio.current_task()
                    if self.current.loop.session.uid in self.group.branch(current):
                        self.switch_to(self.root)
                    self.root.spawn(self.archive_after_command(current, owner), name="archive-agent")
                    return
                continue
            if not isinstance(uid, StopAgent):
                break
            current = uid.uid
            name = self.group.entry(current).agent.session.agent_name
            if await select_choice(loop, f"Stop {name}?", ("stop", "back"), labels={"stop": "Stop this agent", "back": "Back"}, current="back") == "stop":
                self.group.stop(current)
        if isinstance(uid, str):
            if uid in archived:
                await self.show_archive(loop, uid)
            elif uid in self.runtimes and uid != self.current.loop.session.uid:
                self.switch_to(self.runtimes[uid])

    def switch_to(self, runtime: TuiRuntime) -> None:
        previous = self.current
        # Accepted inputs and hooks retain their owner across projection switches.
        self.current = runtime
        self.switching = True
        previous.tui.managed = True
        runtime.tui.managed = True
        previous.tui.exit()

    async def archive_after_command(self, uid: str, owner: asyncio.Task | None) -> None:
        if owner is not None:
            await asyncio.wait({owner})
        try:
            await self.group.archive(uid)
        except Exception as error:  # noqa: BLE001 - a failed archive must not shut down the application.
            self.root.loop.presentation.emit(f"Could not archive agent: {error}")

    @asynccontextmanager
    async def archive_admission(self, uids: set[str]):
        """Quiesce frontend input before group admission, for UI and model archival alike."""
        drained: list[TuiRuntime] = []
        try:
            for key in uids:
                runtime = self.runtimes[key]
                drained.append(runtime)
                await runtime._close_submissions()
            yield
        finally:
            # Failure or cancellation before disposal leaves the retained view usable.
            for runtime in drained:
                if runtime.loop.session.uid in self.runtimes:
                    runtime.accepting = True
                    runtime.submissions_task = runtime.spawn(runtime._consume_submissions(), name="agent-submissions")

    async def show_archive(self, loop: CommandLoop, uid: str) -> None:
        root = self.root.loop.session
        session = await run_blocking(lambda: SessionSnapshotStore.load(uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd))
        try:
            text = "\n\n".join(
                f"## {message.get('role', '')}\n\n"
                + (
                    message["content"]
                    if isinstance(message.get("content"), str) and not message.get("tool_calls")
                    else json.dumps(message, ensure_ascii=False, indent=2)
                )
                for message in session.transcript_messages
            )
            await approval_text_viewer(loop, ApprovalView(f"Archived · {session.agent_name}", text or "(No messages)", rows=[("mode", "read-only")]))
        finally:
            session.close()

    async def close_runtime(self, uid: str) -> None:
        runtime = self.runtimes.pop(uid)
        # Selection can change while archival drains pending submissions/snapshots.
        # Recheck at disposal, not only when the user confirmed the archive.
        if self.current is runtime:
            self.switch_to(self.root)
        await runtime._close_submissions()
        for task in tuple(runtime.tasks):
            task.cancel()
        if runtime.tasks:
            await asyncio.gather(*runtime.tasks, return_exceptions=True)
        await runtime.loop.background.close_background()
        runtime.loop.presentation.close_background_output()
        if runtime.scrollback is not None:
            await runtime.scrollback.close()
        runtime.tui.cancel_input()
        if runtime.tui.modal is not None:
            runtime.tui.close_modal(None)

    async def run_application(self, initial: asyncio.Task | None = None) -> None:
        try:
            while True:
                runtime = self.current
                assert self.root.application_ready is not None
                runtime.tui.on_ready = self.root.application_ready.set
                if self.switching:
                    runtime.tui.scrollback.reanchor()
                self.switching = False
                if initial is not None:
                    await initial
                    initial = None
                else:
                    await runtime.tui.run(style=DynamicStyle(runtime.loop.view.style))
                if not self.switching:
                    break
        finally:
            self.root.request_shutdown()

    async def close(self) -> None:
        # Drain accepted inputs before cancelling engines, then settle all child output writers.
        for runtime in tuple(self.runtimes.values()):
            if runtime is self.root:
                continue
            await runtime._close_submissions()
        try:
            await self.group.close()
        finally:
            for uid in tuple(self.runtimes):
                if uid != self.root.loop.session.uid:
                    await self.close_runtime(uid)


async def agents_command(loop: CommandLoop, args: str) -> str | None:
    group = loop.session.subagents
    assert group is not None
    if args.strip() == "stop-all":
        for uid in group.entries:
            group.stop(uid)
        return "Stopping all agents"
    if args.strip():
        return "Usage: /agents [stop-all]"
    frontend = loop.agents_frontend
    if frontend is None:
        return "\n".join(f"{entry.agent.session.agent_name}: {entry.status}" for entry in group.entries.values())
    await frontend.select(loop)
    return None
