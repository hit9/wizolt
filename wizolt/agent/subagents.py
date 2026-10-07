"""Parallel engines with independent state in one shared working directory.

The group owns admission and child tasks, not a combined conversation. A child receives only
its task and detached configuration; parent notes, messages and usage must never be projected
into it. Shared capability services have one root owner. See design/STATE_OWNERSHIP.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from wizolt.agent.inspection import inspect_session
from wizolt.agent.results import settled_result
from wizolt.base import Json, ToolError, oneline, run_blocking
from wizolt.config import MAX_SUBAGENTS
from wizolt.image import UserInput
from wizolt.session import QueuedInput, Session, SessionSnapshotStore
from wizolt.session.ownership import subagent_root_uid
from wizolt.session.types import SubagentRecord

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


def latest_answer(session: Session) -> str:
    """Latest textual answer; a tool-call-only assistant message may have null content.

    Interruption can leave such a message as the history tail. It is valid conversation state,
    not a damaged agent, and must not break list/stop/report or turn into literal 'None'.
    """
    return next(
        (text for message in reversed(session.messages) if message.get("role") == "assistant" and isinstance(text := message.get("content"), str) and text),
        "",
    )


def report_text(session: Session, *, status: str) -> str:
    """`report`'s payload: the answer as written, or why there is none to show.

    A child that never produced text -- a failure before its first reply -- would otherwise
    report an empty result, which reads as a truncated one. State the status instead.
    """
    if answer := latest_answer(session):
        return answer
    error = session.state.last_turn_error
    return f"{session.agent_name}: no answer text (status: {status})" + (f"; last error: {error}" if error else "")


@dataclass
class AgentEntry:
    agent: Agent
    parent: str = ""
    instruction: str = ""
    task: asyncio.Task | None = None
    restart_requested: bool = False
    # Published only after the child's final snapshot succeeds. Retain the previous
    # result during a subsequent turn so a busy parent can still receive it.
    result: Json = field(default_factory=dict)

    @property
    def error(self) -> str:
        return self.agent.session.state.last_turn_error

    @property
    def answer(self) -> str:
        """Latest textual answer; `report` reads a retained snapshot with the same rule."""
        return latest_answer(self.agent.session)

    @property
    def status(self) -> str:
        if self.agent.session.state.awaiting_input:
            return "waiting for input"
        if self.agent.turn_active:
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

    DEFAULT_WAIT_TIMEOUT = 180
    MAX_WAIT_TIMEOUT = 600

    def __init__(self, root: Agent):
        self.root = root
        self.entries = {root.session.uid: AgentEntry(root)}
        self.driver: Callable[[Agent, QueuedInput], Awaitable[object]] | None = None
        self.on_created: Callable[[Agent], Awaitable[None]] | None = None
        self.on_changed: Callable[[AgentEntry], None] | None = None
        self.on_archived: Callable[[str], Awaitable[None]] | None = None
        self.archive_admission: Callable[[set[str]], AbstractAsyncContextManager[None]] | None = None
        self.closed = False
        self._admission_lock = asyncio.Lock()
        self._archive_lock = asyncio.Lock()

    @property
    def limit(self) -> int:
        """How many children may run at once; 0 turns subagents off."""
        return self.root.session.settings.max_subagents

    @property
    def active(self) -> int:
        """Children holding a slot: an inbox consumer is live, including while it awaits approval.

        A settled child keeps its history and frees its slot; only retention counts it.
        """
        return sum(1 for entry in self.entries.values() if entry.agent is not self.root and entry.task is not None and not entry.task.done())

    def _admit(self, entry: AgentEntry | None = None) -> None:
        """Refuse model-started work beyond the running limit; call under the admission lock.

        Steering an agent that already holds a slot needs no new one.
        """
        if entry is not None and entry.task is not None and not entry.task.done():
            return
        if self.limit == 0:
            raise ToolError("Subagents are off (runtime.max_subagents = 0)")
        if self.active >= self.limit:
            raise ToolError(
                f"Subagent limit reached: {self.active} of {self.limit} agents running (excluding main); wait for one to settle before starting another"
            )

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
            (entry.task is None or entry.task.done()) and entry.agent.session.quiescent for entry in self.entries.values() if entry.agent is not self.root
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
                if item.get("archived"):
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
                    await self._attach(session, str(item.get("parent", self.root.session.uid)), str(item.get("instruction", "")))
                except Exception as error:  # noqa: BLE001 - isolate each child's decoding and feature/frontend assembly.
                    # A damaged child's log cannot make healthy family conversations unusable.
                    # Keep its manifest reference and files for recovery; never overwrite them.
                    problems.append(f"Could not restore subagent {uid}: {error}")
                    continue
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
        entry = AgentEntry(agent, parent, instruction, result=session.state.turn_result)
        self.entries[session.uid] = entry
        try:
            if session.resumed and session.state.last_turn_status == "interrupted" and not entry.result:
                session.state.turn_result = settled_result(session, "")
                await session.save_snapshot()
                entry.result = session.state.turn_result
            if self.on_created is not None:
                await self.on_created(agent)
        except BaseException:
            # The frontend must publish only a fully assembled runtime. Roll back the
            # engine handle without shutting down services borrowed from the family.
            del self.entries[session.uid]
            await agent.model.close()
            session.close()
            raise
        return entry

    async def spawn(self, parent: Session, name: str, message: str, *, model_settings: Session | None = None) -> AgentEntry:
        async with self._admission_lock:
            if self.closed:
                raise ToolError("Agent group is closed")
            if parent.uid not in self.entries:
                raise ToolError("Cannot spawn from an archived agent")
            self._admit()
            if len(self.entries) - 1 >= MAX_SUBAGENTS:
                raise ToolError(f"{MAX_SUBAGENTS} agents retained (excluding main); reuse one with send, or ask the user to archive finished agents")
            if not isinstance(name, str) or not isinstance(message, str) or not name.strip() or not message.strip():
                raise ToolError("spawn requires name and message")
            name = oneline("".join(char if char.isprintable() else " " for char in name), 40)
            if not name:
                raise ToolError("spawn requires name and message")
            if any(entry.agent.session.agent_name.casefold() == name.casefold() for entry in self.entries.values()):
                raise ToolError(f"Agent name already in use: {name}; choose a unique task-based name")
            # Inherit values, never semantic state or request clients. A full provider-choice
            # snapshot also preserves an inherited model if the parent changes it before resume.
            session = Session(
                uid=self.root.session.uid + ".a" + uuid4().hex[:12],
                cwd=parent.cwd,
                config=deepcopy(model_settings.config) if model_settings is not None else parent.config.for_subagent(policy=parent.policy),
                settings=replace(parent.settings),
                created_at=parent.created_at,
                system_prompt=self.root.session.system_prompt,
                system_info=parent.system_info,
                agent_name=name,
                agent_parent=parent.uid,
            )
            session.provider_overrides = session.frozen_provider_overrides()
            entry = await self._attach(session, parent.uid, message)
            # Save the child before publishing its reference, so resume never points at a missing log.
            session.enqueue_user_input(message, origin="child")  # The parent model's task.
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
            if start and not commands:
                # The user typing into a child's frontend is not admitted against the limit.
                self._admit(entry)
            # Commands come only from the user's frontend; everything else is the parent model's.
            entry.agent.session.enqueue_user_input(message, commands=commands, origin="user" if commands else "child")
            await entry.agent.session.save_snapshot()
            if start:
                self._start(entry)

    def _start(self, entry: AgentEntry) -> None:
        if entry.task is None or entry.task.done():
            entry.restart_requested = False
            entry.agent.session.state.last_turn_error = ""
            entry.task = asyncio.create_task(self._run(entry), name=f"agent:{entry.agent.session.uid}")
            entry.task.add_done_callback(lambda task: self._cancelled_before_start(entry, task))
            self.changed(entry)
        elif not entry.agent.turn_active or entry.agent.session.state.last_turn_status in {"failed", "interrupted"}:
            # A follow-up after the turn has settled may resume it, including while the engine
            # or frontend is still publishing its last snapshot. Mid-turn steering is not retry.
            entry.restart_requested = True

    def _cancelled_before_start(self, entry: AgentEntry, task: asyncio.Task) -> None:
        # Cancellation can prevent _run from entering its try/finally at all.
        if task.cancelled() and entry.task is task:
            entry.agent.session.state.last_turn_status = "interrupted"
            entry.task = None
            self.changed(entry)

    def changed(self, entry: AgentEntry) -> None:
        if self.on_changed is not None:
            self.on_changed(entry)

    def results_for(self, parent: str) -> list[Json]:
        results = [entry.result for entry in self.entries.values() if entry.parent == parent and entry.result]
        # Archival must not swallow a completion the parent has not received yet.
        results.extend(
            item.get("result", {})
            for item in self.root.session.subagent_entries
            if item.get("archived") and item.get("parent") == parent and item.get("result")
        )
        return results

    async def _run(self, entry: AgentEntry) -> None:
        """One inbox consumer per child; steering the active turn is claimed by Agent itself.

        Held inputs remain queued for another turn. Sibling engines never share this queue or
        exception path, and notifications are presentation events rather than model messages.
        """
        agent, session = entry.agent, entry.agent.session
        drained = False
        try:
            while session.pending_user_inputs:
                entry.restart_requested = False
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
                entry.restart_requested = False
                session.state.last_turn_status = "failed"
                session.state.last_turn_error = str(error)
            # Clear before reporting, so the notification reflects the settled state.
            entry.task = None
            # send() may have accepted work during the final snapshot while _start still saw
            # this consumer. Handoff only after that save settles; never overlap two writers.
            # Failure/cancellation intentionally pauses the inbox instead of retrying work.
            if (drained or entry.restart_requested) and session.pending_user_inputs and not self.closed:
                self._start(entry)
            else:
                self.changed(entry)

    async def wait(self, uids: list[str], timeout: float = DEFAULT_WAIT_TIMEOUT, mode: str = "any") -> list[AgentEntry]:
        """Observe selected inboxes settling without cancelling their work.

        ``any`` returns the settled targets once one settles, or [] on timeout. ``all`` returns
        once every target has settled; on timeout it returns every target, running ones
        included, so the caller sees what finished and what did not.

        A consumer can hand off queued input to a replacement task during cleanup.
        Recheck the entries after wakeup so that handoff is not reported as completion.
        """
        if mode not in ("any", "all"):
            raise ToolError('wait mode must be "any" or "all"')
        if not isinstance(uids, list) or not uids or any(not isinstance(uid, str) or not uid for uid in uids):
            raise ToolError("wait requires a non-empty agent_ids list")
        entries = [self.entry(uid) for uid in dict.fromkeys(uids)]
        if any(entry.agent is self.root for entry in entries):
            raise ToolError("Cannot wait for main from its own agent group")
        remaining = max(0, min(timeout, self.MAX_WAIT_TIMEOUT))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + remaining
        while True:
            settled = [entry for entry in entries if entry.task is None or entry.task.done()]
            if settled and (mode == "any" or len(settled) == len(entries)):
                return settled
            pending = {entry.task for entry in entries if entry.task is not None and not entry.task.done()}
            done, _ = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                return entries if mode == "all" else []
            remaining = max(0, deadline - loop.time())

    def stop(self, uid: str) -> None:
        entry = self.entry(uid)
        entry.restart_requested = False
        entry.agent.cancel()
        if entry.task is not None:
            entry.task.cancel()

    def branch(self, uid: str) -> set[str]:
        """An archive owns a whole descendant branch; no active child loses its parent."""
        self.entry(uid)
        result = {uid}
        while children := {key for key, entry in self.entries.items() if entry.parent in result} - result:
            result.update(children)
        return result

    async def _retained(self, uid: str) -> tuple[Session, SubagentRecord]:
        """Open an archived child's snapshot for reading; the caller closes it."""
        item = next((item for item in self.root.session.subagent_entries if item.get("uid") == uid and item.get("archived")), None)
        if item is None:
            raise ToolError(f"Unknown agent: {uid}")
        root = self.root.session
        session = await run_blocking(lambda: SessionSnapshotStore.load(uid, config=deepcopy(root.config), settings=replace(root.settings), cwd=root.cwd))
        return session, item

    async def report(self, uid: str) -> str:
        """One child's latest answer whole, live or archived.

        `inspect` and `wait` are bounded by design; this is where the text is read as written.
        An archived child is read from its snapshot, so a finished review stays readable after
        it leaves the group.
        """
        entry = self.entries.get(uid)
        if entry is not None:
            return report_text(entry.agent.session, status=entry.status)
        session, _item = await self._retained(uid)
        try:
            return report_text(session, status="archived")
        finally:
            session.close()

    async def inspect(self, uid: str) -> Json:
        if uid in self.entries:
            entry = self.entries[uid]
            return inspect_session(entry.agent.session, status=entry.status, instruction=entry.instruction, calls=entry.agent.tools.active_calls)
        session, item = await self._retained(uid)
        try:
            return inspect_session(session, status="archived", instruction=item.get("instruction", ""))
        finally:
            session.close()

    async def archive(self, uid: str, *, expected: frozenset[str] | None = None) -> None:
        """Confirmed retirement, retaining snapshots/assets while releasing retained room.

        Publish the archived manifest before disposing engines. Resume consults this manifest,
        never filesystem discovery, so no tombstone or destructive log deletion is needed.
        The caller must outlive the branch (the frontend schedules this on the root runtime).
        """
        from wizolt.agent.lifecycle import close_agent_resources

        async with self._archive_lock, AsyncExitStack() as admission:
            if uid == self.root.session.uid:
                raise ToolError("Cannot archive main")
            uids = self.branch(uid)
            if expected is not None and uids != expected:
                raise ToolError("Agent branch changed since approval; request archive again to approve the current targets")
            # A frontend consumer may be in send(), and a cancelled turn waits for that
            # consumer's FIFO boundary. Drain it BEFORE acquiring group admission.
            if self.archive_admission is not None:
                await admission.enter_async_context(self.archive_admission(uids))
            async with self._admission_lock:
                if self.closed:
                    raise ToolError("Agent group is closed")
                if self.branch(uid) != uids:
                    raise ToolError("Agent branch changed during archival; request archive again")
                entries = [self.entries[key] for key in uids]
                tasks = {task for entry in entries for task in (entry.task, entry.agent._active_task) if task is not None}
                if asyncio.current_task() in tasks:
                    raise ToolError("Archive must run outside the agent being archived")
                for entry in entries:
                    if entry.task is not None or entry.agent._active_task is not None:
                        self.stop(entry.agent.session.uid)
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                for entry in entries:
                    entry.agent.session.pending_user_inputs.clear()
                    await entry.agent.session.save_snapshot()
                root = self.root.session
                previous = root.subagent_entries
                root.subagent_entries = [
                    {
                        **item,
                        "archived": True,
                        "name": self.entries[item["uid"]].agent.session.agent_name,
                        "result": self.entries[item["uid"]].result,
                    }
                    if item.get("uid") in uids
                    else item
                    for item in previous
                ]
                try:
                    await root.save_snapshot()
                except BaseException:
                    root.subagent_entries = previous
                    raise
                for entry in entries:
                    key = entry.agent.session.uid
                    if self.on_archived is not None:
                        await self.on_archived(key)
                    await close_agent_resources(entry.agent, shared_mcp=True)
                    entry.agent.session.close()
                    del self.entries[key]

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
