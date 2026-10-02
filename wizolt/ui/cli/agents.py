"""Agent selection: each agent retains its runtime, input buffer and transcript."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.styles import DynamicStyle
from prompt_toolkit.utils import get_cwidth

from wizolt.agent.engine import Agent
from wizolt.agent.subagents import AgentEntry
from wizolt.base import Text
from wizolt.session import QueuedInput, Session
from wizolt.ui.cli.modals import choice_application, picker_height, select_choice
from wizolt.ui.cli.runtime import ScrollbackWriter, TuiRuntime
from wizolt.ui.render import InputStyle, StatusBar, Theme

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
        root.loop.agents_frontend = self
        self.group.on_created = self.attach
        self.group.on_changed = self.changed
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
        loop.background.open_background()
        assert runtime.runtime_loop is not None
        writer = ScrollbackWriter(runtime.runtime_loop, runtime.tui.write_to_scrollback, loop.presentation.ui.write_direct)
        runtime.scrollback = writer
        loop.presentation.scrollback = writer
        loop.presentation.background_output_lock = writer.lock
        agent.hooks.output_barrier = writer.barrier
        runtime.submissions_task = runtime.spawn(runtime._consume_submissions(), name="agent-submissions")
        self.runtimes[agent.session.uid] = runtime
        if agent.session.resumed:
            loop.resume.render_resumed_session()

    def notice(self, agent: Agent, status: str) -> None:
        if self.current.loop.agent is not agent:
            self.current.loop.presentation.emit_turn(f"[{agent.session.agent_name}] {status}")
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

        def label(uid: str) -> str:
            entry = self.group.entry(uid)
            return (
                f"{entry.agent.session.agent_name} · {entry.status} · "
                f"ctx {entry.agent.session.usage.context_percent(entry.agent.session.state.context_percent)}%"
                + (" (current)" if entry.agent is self.current.loop.agent else "")
            )

        labels = {entry.agent.session.uid: label(entry.agent.session.uid) for entry in entries}

        def row(uid: str) -> StyleAndTextTuples:
            entry = self.group.entry(uid)
            session = entry.agent.session
            available = max(1, min(100, shutil.get_terminal_size((80, 24)).columns) - 8)
            wide = available >= 62
            status_width, marker_width = (17, 10) if wide else (11, 2)
            longest = max(get_cwidth(item.agent.session.agent_name) for item in entries)
            name_width = min(max(8, longest), 28, max(4, available - status_width - marker_width - 12))

            def cell(text: str, width: int) -> str:
                text = Text.clip_width(text, width)
                return text + " " * (width - get_cwidth(text))

            state = entry.status if wide else entry.status.replace("waiting for input", "waiting")
            state_style = {"running": "class:accent", "waiting for input": "class:warning", "failed": "class:error"}.get(entry.status, "class:muted")
            context = session.usage.context_percent(session.state.context_percent)
            marker = (" (current)" if wide else " *") if entry.agent is self.current.loop.agent else ""
            parts: StyleAndTextTuples = [
                ("class:text", cell(session.agent_name, name_width)),
                (state_style, "  " + cell(state, status_width)),
                ("class:muted", f"  ctx {context:3d}%"),
                ("class:accent", cell(marker, marker_width)),
            ]
            parts.append(("", " " * max(0, available - sum(get_cwidth(fragment[1]) for fragment in parts))))
            return StatusBar.clip_fragments(parts, available)

        def preview(uid: str) -> StyleAndTextTuples:
            entry = self.group.entry(uid)
            session = entry.agent.session
            opening = entry.instruction or next((str(message.get("content", "")) for message in session.messages if message.get("role") == "user"), "")
            active = self.runtimes[uid].loop.presentation.model_stream_text
            height = picker_height() - 6 - min(3, len(entries))
            return AgentPreview(opening, active or entry.answer, bool(active), session.agent_name).fragments(shutil.get_terminal_size((80, 24)).columns, height)

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
                actions={"x": StopAgent, "X": self.group.stop},
                keys="↑/↓ j/k move · Enter open · x stop · X stop now · Esc back",
            )
            if not isinstance(uid, StopAgent):
                break
            current = uid.uid
            name = self.group.entry(current).agent.session.agent_name
            if await select_choice(loop, f"Stop {name}?", ("stop", "back"), labels={"stop": "Stop this agent", "back": "Back"}, current="back") == "stop":
                self.group.stop(current)
        if isinstance(uid, str) and uid != self.current.loop.session.uid:
            previous = self.current
            # Do not rebind hooks or transfer queues: input already accepted belongs to previous.
            self.current = self.runtimes[uid]
            self.switching = True
            previous.tui.managed = True
            self.current.tui.managed = True
            previous.tui.exit()

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
            for runtime in tuple(self.runtimes.values()):
                if runtime is self.root:
                    continue
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
