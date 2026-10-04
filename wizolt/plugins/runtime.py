"""Agent-local plugin processes with atomic replacement and cached, nonblocking UI reads.

The terminal never calls user Python. Worker deadlines bound setup and callbacks; synchronous
rendering only reads completed snapshots. A worker is fault isolation, not a data sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import Any

from wizolt.plugins.activity import TurnActivity
from wizolt.plugins.interactions import Interactions
from wizolt.plugins.interception import Interception, InterceptionOrder
from wizolt.plugins.layout import SLOTS, LayoutBudget, LayoutPreferences
from wizolt.plugins.loading import PluginSource
from wizolt.plugins.process import PluginProcess, WorkerError
from wizolt.plugins.protocol import Capabilities, Snapshot
from wizolt.plugins.settings import PluginSettings
from wizolt.sdk import Context, Panel, PluginError, ToolActivity, Value, Viewport
from wizolt.sdk.models import HostCall
from wizolt.sdk.ui import Component


@dataclass(frozen=True)
class Revision:
    """Complete launch inputs, captured once before the candidate starts.

    Settings are an owned deep copy. Runtime services may persist new settings, but must not
    mutate this snapshot. Source history belongs to the user's version control, not here.
    """

    source: PluginSource
    settings: dict
    python: str


def visibility(panel: Panel, available: int) -> str:
    """How a component fared in its allocation, as the layout service reports it."""
    if available == 0:
        return "hidden by height budget"
    if len(panel.rows) > available:
        return "clipped by height budget"
    if panel.rows:
        return "visible"
    return "empty"


@dataclass
class Generation:
    revision: Revision
    plugin: Capabilities
    worker: PluginProcess
    snapshot: Snapshot = field(default_factory=lambda: Snapshot({}, {}))
    # Health per registration ("presentation", "observer:<event>", "intercept:<operation>"),
    # never one generation-wide flag: a failed component skips presentation, a failed
    # interceptor blocks its operations, and commands keep working while the worker lives.
    failures: dict[str, str] = field(default_factory=dict)
    calls: int = 0
    seconds: float = 0
    invocations: int = 0  # Pin this worker, never unrelated plugin generations.
    # Tasks running an operation whose chain includes this generation. Disable cancels them:
    # recovery must not wait for a poisoned turn to finish.
    operations: set[asyncio.Task] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    events: asyncio.Lock = field(default_factory=asyncio.Lock)

    def fail(self, registration: str, reason: str) -> None:
        self.failures.setdefault(registration, reason)

    def failure(self, registration: str) -> str:
        """Why a registration is unavailable: its own failure, or its worker's exit."""
        return self.failures.get(registration) or self.worker.error

    @property
    def error(self) -> str:
        """A summary for status and inspection only; decisions use ``failure(registration)``."""
        return "; ".join(f"{key}: {reason}" for key, reason in self.failures.items()) or self.worker.error

    async def refresh(self, context: Context, *, components: bool = False) -> None:
        """Serialize samples, never enqueue a sample for every terminal layout query."""
        async with self.lock:
            if self.failure("presentation"):
                return
            started = time.monotonic()
            try:
                sampled = Snapshot.decode(await self.worker.request("snapshot" if components else "sample", context=asdict(context)))
                self.snapshot = sampled if components else Snapshot(sampled.fields, self.snapshot.panels)
            except Exception as error:  # noqa: BLE001 - UI callback failures cannot fail turns.
                self.fail("presentation", str(error))
            finally:
                self.calls += 1
                self.seconds += time.monotonic() - started

    async def render(self, context: Context, *, sample: bool) -> Panel:
        """Fuse the first render with this generation's sample; retain one IPC per component."""
        assert context.layout is not None
        started = time.monotonic()
        try:
            value = await self.worker.request("sample_render" if sample else "render", context=asdict(context))
            snapshot = Snapshot.decode(value if sample else {"fields": {}, "panels": {context.layout.slot: value}})
            if sample:
                self.snapshot = Snapshot(snapshot.fields, self.snapshot.panels)
            return snapshot.panels[context.layout.slot]
        finally:
            self.calls += 1
            self.seconds += time.monotonic() - started


@dataclass
class Entry:
    """One live generation, and at most one validated replacement waiting to publish."""

    active: Generation
    pending: Generation | None = None
    disabling: bool = False

    @property
    def status(self) -> str:
        """The live word; a waiting disable or replacement outranks the failure it would clear."""
        if self.disabling:
            return "stopping"
        if self.pending:
            return "reloading"
        if self.active.error:
            return "failed"
        return "running"


class PluginRuntime:
    OBSERVER_TIMEOUT = 1.0
    ACTION_TIMEOUT = 60.0
    REFRESH_INTERVAL = 0.2

    def __init__(self, context: Callable[[], Context], settings: PluginSettings | None = None):
        self.context = context
        self.settings = settings or PluginSettings()
        self.entries: dict[str, Entry] = {}
        self._pending_new: dict[str, Generation] = {}
        self.turn_active = False
        self._lock = asyncio.Lock()
        self._closed = False
        self.viewport = Viewport()
        self.order = LayoutPreferences()
        self.activity = TurnActivity()
        self._layout_version = 0
        self._sampling = asyncio.Lock()
        self._components: dict[str, Component] = {}
        self._refresh_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._retiring: set[asyncio.Task] = set()
        self._launching: set[asyncio.Task] = set()  # Startup candidates not yet admitted.
        self.reserved_commands: frozenset[str] = frozenset()
        self.interpreters: dict[str, str] = {}
        self.validate: Callable[[Capabilities], None] | None = None
        self.on_change: Callable[[], None] | None = None
        self.host_service: HostCall | None = None
        self.interactions = Interactions()
        self.reload_preferences: Callable[[], None] | None = None
        self.interception_order = InterceptionOrder()
        self.interception = Interception(self)

    def read_revision(self, path: str) -> Revision:
        """Capture disk and preferences before starting any candidate worker."""
        source = PluginSource.read(path)
        return Revision(source, self.settings.read(source.name), self.interpreters.get(source.name, ""))

    async def prepare(self, revision: Revision) -> Generation:
        """Launch, then admit; the caller holds the registry lock."""
        candidate = await self.launch(revision)
        try:
            self.admit(candidate)
        except BaseException:
            await candidate.worker.close()
            raise
        return candidate

    async def launch(self, revision: Revision) -> Generation:
        """Start and sample an unpublished candidate. This is nearly all of a plugin's startup
        cost and touches only its own process, so candidates may launch concurrently."""
        source = revision.source
        # An installed path owns its name: reject a renamed source before running any of it.
        for name, entry in self.entries.items():
            if entry.active.revision.source.path == source.path:
                source.require_name(name)
        for name, pending in self._pending_new.items():
            if pending.revision.source.path == source.path:
                source.require_name(name)
        worker, description = await PluginProcess.start(source, python=revision.python, cwd=self.context().cwd, config=revision.settings)
        worker.host_calls.handler = partial(self.call_host, owner=source.name)
        try:
            capabilities = Capabilities.decode(source.name, description)
            if self.validate is not None:
                self.validate(capabilities)
            candidate = Generation(revision, capabilities, worker)
            await candidate.refresh(self.facts(), components=True)
            if reason := candidate.failure("presentation"):
                raise WorkerError(reason, log=worker.stderr, traceback=worker.traceback)
            return candidate
        except BaseException:
            await worker.close()
            raise

    def admit(self, candidate: Generation) -> None:
        """Check a launched candidate against the registry. No awaits: the caller holds the
        lock, so the registry it checks is the one it stages into."""
        name, capabilities = candidate.plugin.name, candidate.plugin
        occupied = set(self.reserved_commands)
        others = [entry.active for key, entry in self.entries.items() if key != name]
        others += [entry.pending for key, entry in self.entries.items() if key != name and entry.pending]
        others += [pending for key, pending in self._pending_new.items() if key != name]
        for other in others:
            occupied.update(other.plugin.commands)
        if collisions := occupied.intersection(capabilities.commands):
            raise PluginError(f"Command names already registered: {', '.join(sorted(collisions))}")

    async def manage(self, action: str, target: str = "", *, commit: Callable[[str, PluginSource], None] | None = None) -> dict[str, Any]:
        """Prepare before publication; failed candidates leave active generations intact.

        The optional synchronous commit saves a validated enable/disable choice before any
        registry mutation. Persistence failures discard only the candidate, not live work.
        """
        async with self._lock:
            if self._closed:
                raise PluginError("Plugin runtime is closed")
            if action in ("list", "inspect"):
                items = [self.describe(name, entry) for name, entry in self.entries.items() if not target or name == target]
                items.extend(self.staged(item) for name, item in self._pending_new.items() if not target or name == target)
                if target and not items:
                    raise PluginError(f"Unknown plugin: {target}")
                return {"plugins": items}
            if action == "enable":
                return await self._enable(await self.prepare(self.read_revision(target)), commit)
            else:
                name = target
                entry = self.entries.get(name)
                if entry is None:
                    if action == "disable" and (pending := self._pending_new.get(name)) is not None:
                        if commit is not None:
                            commit(action, pending.revision.source)
                        self._pending_new.pop(name)
                        self._retire(pending)
                        return {"name": name, "status": "off"}
                    raise PluginError(f"Unknown plugin: {name}")
                if action == "reload":
                    candidate = await self.prepare(self.read_revision(entry.active.revision.source.path))
                elif action == "disable":
                    if commit is not None:
                        commit(action, entry.active.revision.source)
                    if entry.pending:
                        self._retire(entry.pending)
                        entry.pending = None
                    entry.disabling = True
                    self.interactions.dismiss(name)
                    for task in tuple(entry.active.operations):
                        task.cancel()  # An operation this plugin intercepts must not outlast the disable.
                    self._publish()
                    return {"name": name, "status": "stopping" if name in self.entries else "off"}
                else:
                    raise PluginError(f"Unknown plugin action: {action}")
            return self._replace(entry, candidate)

    async def _enable(self, candidate: Generation, commit: Callable[[str, PluginSource], None] | None) -> dict[str, Any]:
        """Stage an admitted candidate; the caller holds the lock. It owns the candidate's worker."""
        name = candidate.plugin.name
        entry = self.entries.get(name)
        existing = entry.active if entry else self._pending_new.get(name)
        try:
            if existing and existing.revision.source.path != candidate.revision.source.path:
                raise PluginError(f"Plugin name {name!r} already belongs to {existing.revision.source.path}")
            # Persistence belongs to the assembly layer, but must succeed before
            # staging/retiring any generation. A failed write owns only this candidate.
            if commit is not None:
                commit("enable", candidate.revision.source)
        except BaseException:
            await candidate.worker.close()
            raise
        if entry is not None:
            return self._replace(entry, candidate)
        if pending := self._pending_new.pop(name, None):
            self._retire(pending)
        self._pending_new[name] = candidate
        self._publish()
        return self.describe(name, self.entries[name]) if name in self.entries else self.staged(candidate)

    def _replace(self, entry: Entry, candidate: Generation) -> dict[str, Any]:
        name = candidate.plugin.name
        if entry.pending:
            self._retire(entry.pending)
        entry.pending, entry.disabling = candidate, False
        self.interactions.dismiss(name)
        self._publish()
        return self.describe(name, entry)

    async def enable_many(self, revisions: list[Revision]) -> list[BaseException | None]:
        """Startup: launch every candidate concurrently, then admit them one by one in order.

        Launching is the slow part and is independent per plugin. Admission stays serial under
        the registry lock, in the given order, so command collisions resolve
        exactly as a sequential startup would. Returns each revision's failure, or None.
        """
        launches = [asyncio.create_task(self.launch(revision)) for revision in revisions]
        self._launching.update(launches)
        try:
            if launches:
                await asyncio.wait(launches)
        except BaseException:
            await self._discard(launches)
            raise
        finally:
            self._launching.difference_update(launches)
        failures: list[BaseException | None] = []
        async with self._lock:
            for index, task in enumerate(launches):
                try:
                    failures.append(await self._admit_launched(task))
                except BaseException:
                    await self._discard(launches[index + 1 :])
                    raise
        return failures

    async def _admit_launched(self, task: asyncio.Task) -> BaseException | None:
        """Admit and stage one finished launch; return its failure. One bad plugin must not
        prevent the others from starting."""
        if task.cancelled():
            return PluginError("Plugin startup was cancelled")
        if (error := task.exception()) is not None:
            return error
        candidate = task.result()
        try:
            if self._closed:
                raise PluginError("Plugin runtime is closed")
            self.admit(candidate)
        except Exception as error:  # noqa: BLE001
            await candidate.worker.close()
            return error
        try:
            await self._enable(candidate, None)  # Closes the worker itself on failure.
        except Exception as error:  # noqa: BLE001
            return error
        return None

    @staticmethod
    async def _discard(launches: list[asyncio.Task]) -> None:
        """Cancel launches and close the workers of those that finished, unpublished."""
        for task in launches:
            task.cancel()
        results = await asyncio.gather(*launches, return_exceptions=True)
        await asyncio.gather(*(item.worker.close() for item in results if isinstance(item, Generation)), return_exceptions=True)

    # Live status words, each naming one situation: starting (a new plugin waits to publish),
    # running, reloading (a replacement waits), stopping (a disable waits), failed, and off.
    # The saved enable choice is reported separately; "pending" meant too many of these at once.
    @staticmethod
    def staged(generation: Generation) -> dict[str, Any]:
        source = generation.revision.source
        return {"name": generation.plugin.name, "path": source.path, "status": "starting", "version": "", "pending_version": source.digest}

    @property
    def active_count(self) -> int:
        """Count usable live generations, not saved preferences or unpublished candidates.

        A deferred disable still runs until its lease ends, so it remains counted. Failed
        callbacks and dead workers are excluded even before the next sampler notices EOF.
        """
        return sum(
            not entry.active.error and not entry.active.worker.error and entry.active.worker.process.returncode is None for entry in self.entries.values()
        )

    @staticmethod
    def describe(name: str, entry: Entry) -> dict[str, Any]:
        item = entry.active
        return {
            "name": name,
            "path": item.revision.source.path,
            "status": entry.status,
            "version": item.revision.source.digest,
            "pending_version": entry.pending.revision.source.digest if entry.pending else "",
            "fields": list(item.plugin.fields),
            "commands": list(item.plugin.commands),
            "tools": {key: {"description": action.description, "parameters": action.parameters} for key, action in item.plugin.tools.items()},
            "slots": list(item.plugin.components),
            "intercepts": {
                operation: {"match": {key: sorted(values) for key, values in spec.match.items()}, **({"response": spec.response} if spec.response else {})}
                for operation, spec in item.plugin.intercepts.items()
            },
            "themes": list(item.plugin.themes),
            "presets": {kind: list(values) for kind, values in item.plugin.presets.items()},
            "error": item.error,
            "calls": item.calls,
            "seconds": round(item.seconds, 6),
            **(WorkerError.diagnostics(item.worker.stderr, item.worker.traceback) if item.error else {}),
        }

    def _retire(self, generation: Generation) -> None:
        task = asyncio.create_task(generation.worker.close())
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    def after_lease(self) -> None:
        """An operation released the generations it pinned; publish what was waiting."""
        self._publish()

    def _publish(self) -> None:
        """Atomic registry switch: no await, and no imported Python objects cross generations."""
        # Turns freeze the complete registry; explicit actions pin only their worker.
        # A window in A must not prevent idle B from publishing a validated replacement.
        if self.turn_active or self._closed:
            return
        eligible = {name: entry for name, entry in self.entries.items() if not entry.active.invocations and (entry.disabling or entry.pending)}
        if not self._pending_new and not eligible:
            return
        self._layout_version += 1
        self._components.clear()
        self.entries.update((name, Entry(candidate)) for name, candidate in self._pending_new.items())
        self._pending_new.clear()
        for name, entry in eligible.items():
            if entry.disabling:
                self._retire(entry.active)
                del self.entries[name]
            elif entry.pending:
                self._retire(entry.active)
                entry.active, entry.pending = entry.pending, None
        if self.entries and (self._refresh_task is None or self._refresh_task.done()):
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        if self.on_change is not None:
            self.on_change()

    async def _refresh_loop(self) -> None:
        while self.entries and not self._closed:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.REFRESH_INTERVAL):
                    await self._wake.wait()
            self._wake.clear()
            await self.refresh()

    def refresh_soon(self) -> None:
        """Ask for the next pass now without waiting for it.

        Refreshing is presentation. Turns and actions must not wait for every plugin's sample:
        one slow field would otherwise add up to two sample deadlines to each of them.
        """
        self._wake.set()

    async def refresh(self) -> None:
        """Allocate in visual order outside painting, then publish one coherent layout.

        Samples may overlap actions, but never another layout pass. A resize, replacement or
        move during IO invalidates this pass rather than publishing stale allocations.
        """
        if not self.entries:
            return
        async with self._sampling:
            version = self._layout_version
            generations = {name: entry.active for name, entry in self.entries.items()}
            context = self.facts()
            await asyncio.gather(*(item.refresh(context) for item in generations.values() if not item.plugin.components))
            sampled: set[str] = set()
            budget = LayoutBudget(self.viewport)
            panels: dict[str, dict[str, Panel]] = {name: {} for name in generations}
            components = {}
            for slot in SLOTS:
                candidates = {
                    f"{name}.{slot}": item for name, item in generations.items() if slot in item.plugin.components and not item.failure("presentation")
                }
                for identity in self.order.ordered(slot, candidates):
                    generation = candidates[identity]
                    if generation.failure("presentation"):
                        continue
                    gap = self.order.gap(identity, slot, generation.plugin.components[slot])
                    allocated = budget.context(context, slot, gap)
                    assert allocated.layout is not None
                    try:
                        panel = await generation.render(allocated, sample=generation.plugin.name not in sampled)
                        sampled.add(generation.plugin.name)
                    except Exception as error:  # noqa: BLE001 - broken components cannot fail the host.
                        generation.fail("presentation", str(error))
                        continue
                    clipped = budget.consume(allocated.layout, panel)
                    rendered_gap = allocated.layout.gap_before if clipped.rows else 0
                    panels[generation.plugin.name][slot] = clipped
                    available = allocated.layout.rows
                    components[identity] = Component(
                        identity, generation.plugin.name, slot, len(clipped.rows) - rendered_gap, available, visibility(panel, available), gap, rendered_gap
                    )
            if version == self._layout_version:
                for name, item in generations.items():
                    item.snapshot = Snapshot(item.snapshot.fields, panels[name])
                self._components = components

    def facts(self) -> Context:
        return replace(self.context(), columns=self.viewport.columns, viewport=self.viewport, layout=None, turn=self.activity.snapshot())

    def resize(self, columns: int, rows: int) -> None:
        viewport = Viewport(max(0, columns), max(0, rows))
        if viewport != self.viewport:
            self.viewport = viewport
            self._layout_version += 1

    def components(self) -> tuple[Component, ...]:
        result = []
        for slot in SLOTS:
            identities = {f"{name}.{slot}": name for name, entry in self.entries.items() if slot in entry.active.plugin.components}
            for identity in self.order.ordered(slot, identities):
                name = identities[identity]
                gap = self.order.gap(identity, slot, self.entries[name].active.plugin.components[slot])
                item = self._components.get(identity, Component(identity, name, slot, 0, 0, "pending layout"))
                result.append(replace(item, gap_before=gap))
        return tuple(result)

    async def call_host(self, service: str, arguments: dict, *, owner: str = "") -> dict:
        """Route explicit host capabilities; model credentials stay in the assembly adapter."""
        if service.startswith("settings."):
            return await self.settings.call(owner, service, arguments)
        if service.startswith("ui.components."):
            action = service.removeprefix("ui.components.")
            expected = {"list": set(), "move": {"component", "before", "after"}, "reset_order": {"slot"}, "set_gap": {"component", "gap_before"}}
            if (
                action not in expected
                or set(arguments) != expected[action]
                or any(not isinstance(value, str) for key, value in arguments.items() if key != "gap_before")
            ):
                raise PluginError("Invalid component layout request")
            if action == "move":
                self.order.move(arguments["component"], arguments["before"], arguments["after"], {item.id: item.slot for item in self.components()})
            elif action == "reset_order":
                self.order.reset(arguments["slot"])
            elif action == "set_gap":
                self.order.set_gap(arguments["component"], arguments["gap_before"], {item.id: item.slot for item in self.components()})
            if action != "list":
                self._layout_version += 1
            return {"components": [asdict(item) for item in self.components()]}
        if service.startswith("ui."):
            if service == "ui.views.show":
                entry = self.entries.get(owner)
                # Dismissing existing windows is only half of retirement. An action may
                # handle cancellation by opening another one, indefinitely pinning its worker.
                if entry is None or entry.disabling or entry.pending is not None:
                    raise PluginError("Plugin is being replaced or disabled; cannot open another view")
            return await self.interactions.call(owner, service, arguments)
        if self.host_service is None:
            raise PluginError("Host services unavailable in offline trials")
        return await self.host_service(service, arguments)

    async def start_turn(self) -> None:
        self.activity.reset()
        self.turn_active = True
        await self.emit("turn.started")
        self.refresh_soon()

    async def finish_turn(self) -> None:
        if not self.turn_active:
            return
        try:
            await self.emit("turn.finished")
            self.refresh_soon()
        finally:
            self.turn_active = False
            self._publish()

    async def emit(self, name: str, *, tool: ToolActivity | None = None, reason: str = "") -> None:
        generations = [
            entry.active for entry in self.entries.values() if name in entry.active.plugin.observers and not entry.active.failure(f"observer:{name}")
        ]
        if not generations:
            return
        context = self.facts()
        await asyncio.gather(*(self._notify(item, name, context, tool, reason) for item in generations))

    async def _notify(self, generation: Generation, name: str, context: Context, tool: ToolActivity | None, reason: str) -> None:
        # Serialize each plugin's observers, not tool execution. Parallel plugins do not
        # multiply the deadline, and a failed observer leaves other generations usable.
        async with generation.events:
            if generation.failure(f"observer:{name}"):
                return
            try:
                await generation.worker.request(
                    "event", timeout=self.OBSERVER_TIMEOUT, name=name, context=asdict(context), tool=asdict(tool) if tool else None, reason=reason
                )
            except Exception as error:  # noqa: BLE001 - observer failure cannot fail an agent turn.
                generation.fail(f"observer:{name}", str(error))

    def fields(self) -> dict[str, Value]:
        return {
            f"plugins.{name}.{key}": value
            for name, entry in self.entries.items()
            if not entry.active.failure("presentation")
            for key, value in entry.active.snapshot.fields.items()
        }

    def panels(self, slot: str, columns: int | None = None) -> list[Panel]:
        """Read cached panels. Only the live prompt projection passes its width: a reader without
        one, such as /status, must not resize what the sampler lays out for the prompt slots."""
        if columns is not None:
            self.resize(columns, self.viewport.rows)
        identities = {
            f"{name}.{slot}": entry.active
            for name, entry in self.entries.items()
            if not entry.active.failure("presentation") and slot in entry.active.snapshot.panels
        }
        return [identities[identity].snapshot.panels[slot] for identity in self.order.ordered(slot, identities)]

    async def invoke(self, name: str, kind: str, action: str, arguments: Mapping[str, Any]) -> str:
        """Pin the worker until completion; authorization remains at the user/model boundary."""
        if self._closed:
            raise PluginError("Plugin runtime is closed")
        if kind not in ("command", "tool"):
            raise PluginError(f"Unknown action kind: {kind}")
        entry = self.entries.get(name)
        if entry is None:
            raise PluginError(f"Unknown plugin: {name}")
        if entry.disabling:
            # Draining admits no new work: overlapping calls would keep renewing the lease.
            raise PluginError(f"{name} is being disabled")
        generation = entry.active
        operation = (generation.plugin.commands if kind == "command" else generation.plugin.tools).get(action)
        if operation is None:
            raise PluginError(f"Unknown {kind}: {name}.{action}")
        # Schema evaluation can itself block (for example, a pathological regex). The
        # worker validates before calling the handler, under the same action deadline.
        generation.invocations += 1
        try:
            result = await generation.worker.request(
                "invoke",
                timeout=self.ACTION_TIMEOUT,
                kind=kind,
                name=action,
                arguments=dict(arguments),
                context=asdict(self.facts()),
            )
            self.refresh_soon()
            return result
        finally:
            generation.invocations -= 1
            self._publish()

    async def close(self) -> None:
        # Candidate preparation awaits a child process. Serialize teardown with that admission
        # so a shutdown cannot leave a newly prepared worker outside the retirement set.
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        self._closed = True
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
        # Startup launches run outside the lock; their workers must not outlive the runtime.
        await self._discard(list(self._launching))
        for entry in self.entries.values():
            self._retire(entry.active)
            if entry.pending:
                self._retire(entry.pending)
        for candidate in self._pending_new.values():
            self._retire(candidate)
        self.entries.clear()
        self._pending_new.clear()
        await asyncio.gather(*tuple(self._retiring), return_exceptions=True)
