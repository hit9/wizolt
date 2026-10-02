"""Parallel engines with independent state in one shared working directory.

The group owns admission and child tasks, not a combined conversation. A child receives only
its task and detached configuration; parent notes, messages and usage must never be projected
into it. Shared capability services have one root owner. See design/STATE_OWNERSHIP.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from wizolt.base import ToolError, WizoltError, oneline, run_blocking
from wizolt.image import UserInput
from wizolt.session import QueuedInput, Session, SessionSnapshotStore
from wizolt.session.ownership import subagent_root_uid

if TYPE_CHECKING:
    from wizolt.agent.engine import Agent

SHARED_WORKSPACE = """You share the working directory and filesystem with other agents. File changes
are immediately visible to all agents; you do not have a separate worktree. Your conversation and
statistics are independent. Follow the assigned task and file boundaries. Do not overwrite or
revert other agents' changes. Coordinate overlapping edits with the parent before proceeding.
Report the files changed and verification performed. Do not create specialized agent roles.
"""


def unavailable_input(_prompt: str) -> str:
    """A background engine never owns process stdin; its frontend must provide approvals."""
    raise ToolError("Subagent approval requires an interactive frontend; no input was read")


@dataclass
class AgentEntry:
    agent: Agent
    parent: str = ""
    instruction: str = ""
    task: asyncio.Task | None = None

    @property
    def error(self) -> str:
        return self.agent.session.state.last_turn_error

    @property
    def answer(self) -> str:
        """Latest textual answer; a tool-call-only assistant message may have null content.

        Interruption can leave such a message as the history tail. It is valid conversation
        state, not a damaged agent, and must not break list/stop or turn into literal 'None'.
        """
        return next(
            (
                text
                for message in reversed(self.agent.session.messages)
                if message.get("role") == "assistant" and isinstance(text := message.get("content"), str) and text
            ),
            "",
        )

    @property
    def status(self) -> str:
        if self.agent.session.state.awaiting_input:
            return "waiting for input"
        if self.agent._active_task is not None:
            return "running"
        state = self.agent.session.state
        return state.last_turn_status if state.last_turn_status != "idle" else "completed" if state.round_count else "idle"


@dataclass(frozen=True)
class AgentCounts:
    """A read-only group projection, including main; never persisted or used as agent usage."""

    total: int
    running: int
    waiting: int


class Subagents:
    """Own one family's admission, child engines and serial inboxes.

    This is the sole group-wide mutable coordinator. Nested children join the same registry and
    limit. Each engine still writes only its own Session. UI callbacks project that state and
    must not become storage or a second engine driver.
    """

    def __init__(self, root: Agent):
        self.root = root
        self.entries = {root.session.uid: AgentEntry(root)}
        self.driver: Callable[[Agent, QueuedInput], Awaitable[object]] | None = None
        self.on_created: Callable[[Agent], Awaitable[None]] | None = None
        self.on_changed: Callable[[AgentEntry], None] | None = None
        self.closed = False
        self._admission_lock = asyncio.Lock()

    @property
    def limit(self) -> int:
        return self.root.session.settings.max_subagents

    @property
    def counts(self) -> AgentCounts:
        """Derive one consistent view of retained engines, not an independently updated counter.

        Waiting for user input takes precedence over running, as it does in the agent picker.
        Completed/interrupted/failed children remain retained until the family closes.
        """
        states = [entry.status for entry in self.entries.values()]
        return AgentCounts(len(states), states.count("running"), states.count("waiting for input"))

    def entry(self, uid: str) -> AgentEntry:
        try:
            return self.entries[uid]
        except KeyError:
            raise ToolError(f"Unknown agent: {uid}") from None

    @property
    def quiescent(self) -> bool:
        return all(
            (entry.task is None or entry.task.done())
            and not entry.agent.session._active_runs
            and (entry.agent.session._snapshot_gate is None or not entry.agent.session._snapshot_gate.locked())
            for entry in self.entries.values()
            if entry.agent is not self.root
        )

    async def restore(self) -> list[str]:
        """Rebuild handles from child snapshots without waking persisted inboxes.

        The root configuration supplies current credentials; frozen child provider choices
        override it. Restoring is never implicit authorization to restart interrupted work.
        """
        problems = []
        async with self._admission_lock:
            if self.closed:
                raise ToolError("Agent group is closed")
            for item in self.root.session.subagent_entries:
                if not isinstance(item, dict):
                    continue
                uid = item.get("uid", "")
                if not isinstance(uid, str) or subagent_root_uid(uid) != self.root.session.uid or uid == self.root.session.uid:
                    continue
                if uid in self.entries:
                    continue
                try:
                    session = await run_blocking(
                        lambda uid=uid: SessionSnapshotStore.load(
                            uid, config=deepcopy(self.root.session.config), settings=replace(self.root.session.settings), cwd=self.root.session.cwd
                        )
                    )
                except (WizoltError, OSError, ValueError, TypeError, KeyError) as error:
                    # A damaged child's log cannot make healthy family conversations unusable.
                    # Keep its manifest reference and files for recovery; never overwrite them.
                    problems.append(f"Could not restore subagent {uid}: {error}")
                    continue
                await self._attach(session, str(item.get("parent", self.root.session.uid)), str(item.get("instruction", "")))
        return problems

    async def _attach(self, session: Session, parent: str, instruction: str = "") -> AgentEntry:
        from wizolt.agent.engine import Agent
        from wizolt.agent.lifecycle import bootstrap_features

        root = self.root.session
        session.listed = False
        session.subagents = self
        session.skills, session.mcp, session.catalog = root.skills, root.mcp, root.catalog
        session.system_info = root.system_info
        # Attachment adds the guard once. Child snapshots persist semantic messages, not this
        # runtime prompt; nested creation starts from root.system_prompt to avoid repeated guards.
        session.system_prompt += "\n" + SHARED_WORKSPACE
        # A healthy descendant can outlive an unreadable parent snapshot. Its own identity and
        # history survive; only runtime hook inheritance falls back to the family root.
        parent_session = self.entries.get(parent, self.entries[root.uid]).agent.session
        session.shell_hooks = parent_session.shell_hooks.detached(parent_session) if parent_session.shell_hooks else None
        # Discovery/transport are group services; resolvers and their frontend callbacks are local.
        bootstrap_features(session)
        session.borrow_ownership(root)
        agent = Agent(session, input_fn=unavailable_input, output_fn=lambda _: None)
        entry = AgentEntry(agent, parent, instruction)
        self.entries[session.uid] = entry
        if self.on_created is not None:
            await self.on_created(agent)
        return entry

    async def spawn(self, parent: Session, name: str, message: str, *, model_settings: Session | None = None) -> AgentEntry:
        async with self._admission_lock:
            if self.closed:
                raise ToolError("Agent group is closed")
            if len(self.entries) - 1 >= self.limit:
                raise ToolError(f"Subagent limit reached ({self.limit}, excluding main); reuse an existing agent with send")
            if not isinstance(name, str) or not isinstance(message, str) or not name.strip() or not message.strip():
                raise ToolError("spawn requires name and message")
            name = oneline("".join(char if char.isprintable() else " " for char in name), 40)
            if not name:
                raise ToolError("spawn requires name and message")
            if any(entry.agent.session.agent_name.casefold() == name.casefold() for entry in self.entries.values()):
                raise ToolError(f"Agent name already in use: {name}; choose a unique task-based name")
            model_settings = model_settings or parent
            # Inherit values, never semantic state or request clients. A full provider-choice
            # snapshot also preserves an inherited model if the parent changes it before resume.
            session = Session(
                uid=self.root.session.uid + ".a" + uuid4().hex[:12],
                cwd=parent.cwd,
                config=deepcopy(model_settings.config),
                provider_overrides=model_settings.frozen_provider_overrides(),
                settings=replace(parent.settings),
                created_at=parent.created_at,
                system_prompt=self.root.session.system_prompt,
                system_info=parent.system_info,
                agent_name=name,
                agent_parent=parent.uid,
            )
            entry = await self._attach(session, parent.uid, message)
            # Save the child before publishing its reference, so resume never points at a missing log.
            session.enqueue_user_input(message)
            await session.save_snapshot()
            self.root.session.subagent_entries.append({"uid": session.uid, "parent": parent.uid, "instruction": message})
            await self.root.session.save_snapshot()
            self._start(entry)
            return entry

    async def send(self, uid: str, message: str | UserInput, *, start: bool = True, commands: bool = False) -> None:
        async with self._admission_lock:
            entry = self.entry(uid)
            if entry.agent is self.root:
                raise ToolError("Send steering directly through the main agent's input")
            if self.closed:
                raise ToolError("Agent group is closed")
            if not str(message).strip():
                raise ToolError("send requires message")
            entry.agent.session.enqueue_user_input(message, commands=commands)
            await entry.agent.session.save_snapshot()
            if start:
                self._start(entry)

    def _start(self, entry: AgentEntry) -> None:
        if entry.task is None or entry.task.done():
            entry.agent.session.state.last_turn_error = ""
            entry.task = asyncio.create_task(self._run(entry), name=f"agent:{entry.agent.session.uid}")
            entry.task.add_done_callback(lambda task: self._cancelled_before_start(entry, task))
            self.changed(entry)

    def _cancelled_before_start(self, entry: AgentEntry, task: asyncio.Task) -> None:
        # Cancellation can prevent _run from entering its try/finally at all.
        if task.cancelled() and entry.task is task:
            entry.agent.session.state.last_turn_status = "interrupted"
            entry.task = None
            self.changed(entry)

    def changed(self, entry: AgentEntry) -> None:
        if self.on_changed is not None:
            self.on_changed(entry)

    async def _run(self, entry: AgentEntry) -> None:
        """One inbox consumer per child; steering the active turn is claimed by Agent itself.

        Held inputs remain queued for another turn. Sibling engines never share this queue or
        exception path, and notifications are presentation events rather than model messages.
        """
        agent, session = entry.agent, entry.agent.session
        drained = False
        try:
            while session.pending_user_inputs:
                value = session.pending_user_inputs.pop(0)
                if self.driver is None:
                    await agent.run(value.user_input())
                else:
                    await self.driver(agent, value)
            drained = True
        except asyncio.CancelledError:
            session.state.last_turn_status = "interrupted"
        except Exception as error:  # noqa: BLE001 - a child failure is reported, never takes down siblings.
            session.state.last_turn_status = "failed"
            session.state.last_turn_error = str(error)
        finally:
            try:
                await session.save_snapshot()
            except Exception as error:  # noqa: BLE001 - retain a failed save as a visible child failure.
                drained = False
                session.state.last_turn_status = "failed"
                session.state.last_turn_error = str(error)
            # Clear before reporting, so the notification reflects the settled state.
            entry.task = None
            # send() may have accepted work during the final snapshot while _start still saw
            # this consumer. Handoff only after that save settles; never overlap two writers.
            # Failure/cancellation intentionally pauses the inbox instead of retrying work.
            if drained and session.pending_user_inputs and not self.closed:
                self._start(entry)
            else:
                self.changed(entry)

    async def wait(self, uid: str, timeout: float = 30) -> AgentEntry:
        entry = self.entry(uid)
        if entry.agent is self.root:
            raise ToolError("Cannot wait for main from its own agent group")
        task = entry.task
        if task is not None:
            await asyncio.wait({task}, timeout=max(0, min(timeout, 60)))
        return entry

    def stop(self, uid: str) -> None:
        entry = self.entry(uid)
        entry.agent.cancel()
        if entry.task is not None:
            entry.task.cancel()

    async def close(self) -> None:
        """Stop admission, join engines and settle child snapshots before resources close.

        Clients, jobs and the shared MCP owner are closed by lifecycle.close_agent_resources;
        the group must quiesce all writers before that owner releases the family lease.
        """
        # Admission includes its durable save and scheduling: no child can start after this gate.
        async with self._admission_lock:
            if self.closed:
                return
            self.closed = True
            tasks = [entry.task for entry in self.entries.values() if entry.task is not None]
            for entry in self.entries.values():
                if entry.agent is not self.root:
                    self.stop(entry.agent.session.uid)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            saves = await asyncio.gather(
                *(entry.agent.session.save_snapshot() for entry in self.entries.values() if entry.agent is not self.root), return_exceptions=True
            )
            for result in saves:
                if isinstance(result, BaseException):
                    raise result
