"""Agent selection: each agent retains its runtime, input buffer and transcript."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from prompt_toolkit.styles import DynamicStyle

from wizolt.agent.engine import Agent
from wizolt.agent.subagents import AgentEntry
from wizolt.base import Text
from wizolt.image import UserInput
from wizolt.ui.cli.modals import choice_application
from wizolt.ui.cli.runtime import ScrollbackWriter, TuiRuntime
from wizolt.ui.render import InputStyle, Theme

if TYPE_CHECKING:
    from wizolt.ui.cli.loop import CommandLoop


class AgentsFrontend:
    """Keep engines running while one prompt-toolkit application owns the terminal."""

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

    async def drive(self, agent: Agent, value: UserInput) -> None:
        runtime = self.runtimes[agent.session.uid]
        try:
            if not await runtime.dispatch(value):
                await runtime.run_agent_turn(value)
        finally:
            if runtime.turn_active:
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

        def preview(uid: str) -> str:
            entry = self.group.entry(uid)
            session = entry.agent.session
            opening = entry.instruction or next((str(message.get("content", "")) for message in session.messages if message.get("role") == "user"), "")
            recent = next((str(message.get("content", "")) for message in reversed(session.messages) if message.get("role") == "assistant"), "")
            active = self.runtimes[uid].loop.presentation.model_stream_text
            return Text.clip_width(session.agent_name, 80) + "\n\nTask\n" + opening[:1500] + "\n\nLatest reply\n" + (active or recent)[-3000:]

        uid = await choice_application(
            loop,
            "Agents",
            tuple(labels),
            labels=labels,
            current=self.current.loop.session.uid,
            disabled=set(),
            preview_fn=preview,
            label_fn=lambda uid: [("", label(uid))],
        )
        if isinstance(uid, str) and uid != self.current.loop.session.uid:
            previous = self.current
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
