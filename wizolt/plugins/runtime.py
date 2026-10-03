"""Agent-local plugin processes with atomic replacement and cached, nonblocking UI reads.

The terminal never calls user Python. Worker deadlines bound setup and callbacks; synchronous
rendering only reads completed snapshots. A worker is fault isolation, not a data sandbox.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from wizolt.plugins.activity import TurnActivity
from wizolt.plugins.layout import SLOTS, LayoutBudget, LayoutPreferences
from wizolt.plugins.loading import PluginSource
from wizolt.plugins.process import PluginProcess, WorkerError
from wizolt.plugins.protocol import Capabilities, Snapshot
from wizolt.plugins.settings import PluginSettings
from wizolt.sdk import Context, Panel, PluginError, ToolActivity, Value, Viewport
from wizolt.sdk.models import HostCall
from wizolt.sdk.ui import Component


@dataclass
class Generation:
    source: PluginSource
    plugin: Capabilities
    worker: PluginProcess
    settings: dict = field(default_factory=dict)
    python: str = ""
    snapshot: Snapshot = field(default_factory=lambda: Snapshot({}, {}))
    error: str = ""
    calls: int = 0
    seconds: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    events: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def refresh(self, context: Context, *, components: bool = False) -> None:
        """Serialize samples, never enqueue a sample for every terminal layout query."""
        async with self.lock:
            if self.error:
                return
            started = time.monotonic()
            try:
                sampled = Snapshot.decode(await self.worker.request("snapshot" if components else "sample", context=asdict(context)))
                self.snapshot = sampled if components else Snapshot(sampled.fields, self.snapshot.panels)
            except Exception as error:  # noqa: BLE001 - UI callback failures cannot fail turns.
                self.error = str(error)
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
    """Rollback retains launch inputs, not a second idle worker or its mutable state."""

    active: Generation
    previous: PluginSource | None = None
    previous_settings: dict = field(default_factory=dict)
    previous_python: str = ""
    pending: Generation | None = None
    disabling: bool = False


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
        self._invocations = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self.viewport = Viewport()
        self.order = LayoutPreferences()
        self.activity = TurnActivity()
        self._layout_version = 0
        self._sampling = asyncio.Lock()
        self._components: dict[str, Component] = {}
        self._refresh_task: asyncio.Task | None = None
        self._retiring: set[asyncio.Task] = set()
        self.reserved_commands: frozenset[str] = frozenset()
        self.interpreters: dict[str, str] = {}
        self.validate: Callable[[Capabilities], None] | None = None
        self.on_change: Callable[[], None] | None = None
        self.host_service: HostCall | None = None

    async def prepare(self, path: str, source: PluginSource | None = None, settings: dict | None = None, *, python: str | None = None) -> Generation:
        revision = source if source is not None else PluginSource.read(path)
        for name, entry in self.entries.items():
            if entry.active.source.path == revision.path:
                revision.require_name(name)
        for name, pending in self._pending_new.items():
            if pending.source.path == revision.path:
                revision.require_name(name)
        settings = self.settings.read(revision.name) if settings is None else settings
        python = self.interpreters.get(revision.name, "") if python is None else python
        worker, description = await PluginProcess.start(revision, python=python, cwd=self.context().cwd, config=settings)
        worker.host_calls.handler = self.call_host
        try:
            capabilities = Capabilities.decode(revision.name, description)
            if self.validate is not None:
                self.validate(capabilities)
            occupied = set(self.reserved_commands)
            for name, entry in self.entries.items():
                if name != revision.name:
                    if capabilities.summarizer and (entry.active.plugin.summarizer or (entry.pending and entry.pending.plugin.summarizer)):
                        raise PluginError(f"Summarizer already supplied by {name}; disable it before enabling another")
                    occupied.update(entry.active.plugin.commands)
                    if entry.pending:
                        occupied.update(entry.pending.plugin.commands)
            for name, candidate in self._pending_new.items():
                if name != revision.name:
                    if capabilities.summarizer and candidate.plugin.summarizer:
                        raise PluginError(f"Summarizer already supplied by {name}; disable it before enabling another")
                    occupied.update(candidate.plugin.commands)
            if collisions := occupied.intersection(capabilities.commands):
                raise PluginError(f"Command names already registered: {', '.join(sorted(collisions))}")
            candidate = Generation(revision, capabilities, worker, settings=settings, python=python)
            await candidate.refresh(self.facts(), components=True)
            if candidate.error:
                raise WorkerError(candidate.error, log=worker.stderr, traceback=worker.traceback)
            return candidate
        except BaseException:
            await worker.close()
            raise

    async def manage(self, action: str, target: str = "") -> dict[str, Any]:
        """Prepare before publication; failed candidates leave active generations intact."""
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
                candidate = await self.prepare(target)
                name = candidate.plugin.name
                entry = self.entries.get(name)
                existing = entry.active if entry else self._pending_new.get(name)
                if existing and existing.source.path != candidate.source.path:
                    await candidate.worker.close()
                    raise PluginError(f"Plugin name {name!r} already belongs to {existing.source.path}")
                if entry is None:
                    if pending := self._pending_new.pop(name, None):
                        self._retire(pending)
                    self._pending_new[name] = candidate
                    self._publish()
                    return self.describe(name, self.entries[name]) if name in self.entries else self.staged(candidate)
            else:
                name = target
                entry = self.entries.get(name)
                if entry is None:
                    if action == "disable" and (pending := self._pending_new.pop(name, None)) is not None:
                        self._retire(pending)
                        return {"name": name, "status": "disabled"}
                    raise PluginError(f"Unknown plugin: {name}")
                if action == "reload":
                    candidate = await self.prepare(entry.active.source.path)
                elif action == "rollback":
                    if entry.previous is None:
                        raise PluginError(f"{name}: no previous version")
                    candidate = await self.prepare(entry.previous.path, entry.previous, entry.previous_settings, python=entry.previous_python)
                elif action == "disable":
                    if entry.pending:
                        self._retire(entry.pending)
                        entry.pending = None
                    entry.disabling = True
                    self._publish()
                    return {"name": name, "status": "pending" if self.busy else "disabled"}
                else:
                    raise PluginError(f"Unknown plugin action: {action}")
            assert entry is not None
            if entry.pending:
                self._retire(entry.pending)
            entry.pending, entry.disabling = candidate, False
            self._publish()
            return self.describe(name, entry)

    @staticmethod
    def staged(generation: Generation) -> dict[str, Any]:
        return {"name": generation.plugin.name, "path": generation.source.path, "status": "pending", "version": "", "pending_version": generation.source.digest}

    @property
    def busy(self) -> bool:
        return self.turn_active or self._invocations > 0

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
            "path": item.source.path,
            "status": "pending" if entry.pending or entry.disabling else "error" if item.error else "active",
            "version": item.source.digest,
            "pending_version": entry.pending.source.digest if entry.pending else "",
            "fields": list(item.plugin.fields),
            "commands": list(item.plugin.commands),
            "tools": {key: {"description": action.description, "parameters": action.parameters} for key, action in item.plugin.tools.items()},
            "slots": list(item.plugin.components),
            "summarizer": item.plugin.summarizer,
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

    def _publish(self) -> None:
        """Atomic registry switch: no await, and no imported Python objects cross generations."""
        if self.busy or self._closed:
            return
        if not self._pending_new and not any(entry.disabling or entry.pending for entry in self.entries.values()):
            return
        self._layout_version += 1
        self._components.clear()
        self.entries.update((name, Entry(candidate)) for name, candidate in self._pending_new.items())
        self._pending_new.clear()
        for name, entry in tuple(self.entries.items()):
            if entry.disabling:
                self._retire(entry.active)
                del self.entries[name]
            elif entry.pending:
                self._retire(entry.active)
                entry.previous_settings = entry.active.settings
                # The installation may already point at a newer dependency environment.
                # Rollback needs the interpreter that actually ran this source revision.
                entry.previous_python = entry.active.python
                entry.previous, entry.active, entry.pending = entry.active.source, entry.pending, None
        if self.entries and (self._refresh_task is None or self._refresh_task.done()):
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        if self.on_change is not None:
            self.on_change()

    async def _refresh_loop(self) -> None:
        while self.entries and not self._closed:
            await asyncio.sleep(self.REFRESH_INTERVAL)
            await self.refresh()

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
                candidates = {f"{name}.{slot}": item for name, item in generations.items() if slot in item.plugin.components and not item.error}
                for identity in self.order.ordered(slot, candidates):
                    generation = candidates[identity]
                    if generation.error:
                        continue
                    gap = self.order.gap(identity, slot, generation.plugin.components[slot])
                    allocated = budget.context(context, slot, gap)
                    assert allocated.layout is not None
                    try:
                        panel = await generation.render(allocated, sample=generation.plugin.name not in sampled)
                        sampled.add(generation.plugin.name)
                    except Exception as error:  # noqa: BLE001 - broken components cannot fail the host.
                        generation.error = str(error)
                        continue
                    clipped = budget.consume(allocated.layout, panel)
                    rendered_gap = allocated.layout.gap_before if clipped.rows else 0
                    panels[generation.plugin.name][slot] = clipped
                    available = allocated.layout.rows
                    visibility = (
                        "hidden by height budget"
                        if available == 0
                        else "clipped by height budget"
                        if len(panel.rows) > available
                        else "visible"
                        if panel.rows
                        else "empty"
                    )
                    components[identity] = Component(
                        identity, generation.plugin.name, slot, len(clipped.rows) - rendered_gap, available, visibility, gap, rendered_gap
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

    async def call_host(self, service: str, arguments: dict) -> dict:
        """Route explicit host capabilities; model credentials stay in the assembly adapter."""
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
        if self.host_service is None:
            raise PluginError("Host services unavailable in offline trials")
        return await self.host_service(service, arguments)

    async def start_turn(self) -> None:
        self.activity.reset()
        self.turn_active = True
        await self.emit("turn.started")
        await self.refresh()

    async def finish_turn(self) -> None:
        if not self.turn_active:
            return
        try:
            await self.emit("turn.finished")
            await self.refresh()
        finally:
            self.turn_active = False
            self._publish()

    async def emit(self, name: str, *, tool: ToolActivity | None = None, reason: str = "") -> None:
        generations = [entry.active for entry in self.entries.values() if name in entry.active.plugin.observers and not entry.active.error]
        if not generations:
            return
        context = self.facts()
        await asyncio.gather(*(self._notify(item, name, context, tool, reason) for item in generations))

    async def _notify(self, generation: Generation, name: str, context: Context, tool: ToolActivity | None, reason: str) -> None:
        # Serialize each plugin's observers, not tool execution. Parallel plugins do not
        # multiply the deadline, and a failed observer leaves other generations usable.
        async with generation.events:
            if generation.error:
                return
            try:
                await generation.worker.request(
                    "event", timeout=self.OBSERVER_TIMEOUT, name=name, context=asdict(context), tool=asdict(tool) if tool else None, reason=reason
                )
            except Exception as error:  # noqa: BLE001 - observer failure cannot fail an agent turn.
                generation.error = f"{name}: {error}"

    def fields(self) -> dict[str, Value]:
        return {
            f"plugins.{name}.{key}": value
            for name, entry in self.entries.items()
            if not entry.active.error
            for key, value in entry.active.snapshot.fields.items()
        }

    def panels(self, slot: str, columns: int | None = None) -> list[Panel]:
        """Read cached panels. Only the live prompt projection passes its width: a reader without
        one, such as /status, must not resize what the sampler lays out for the prompt slots."""
        if columns is not None:
            self.resize(columns, self.viewport.rows)
        identities = {f"{name}.{slot}": entry.active for name, entry in self.entries.items() if not entry.active.error and slot in entry.active.snapshot.panels}
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
        generation = entry.active
        operation = (generation.plugin.commands if kind == "command" else generation.plugin.tools).get(action)
        if operation is None:
            raise PluginError(f"Unknown {kind}: {name}.{action}")
        # Schema evaluation can itself block (for example, a pathological regex). The
        # worker validates before calling the handler, under the same action deadline.
        self._invocations += 1
        try:
            result = await generation.worker.request(
                "invoke",
                timeout=self.ACTION_TIMEOUT,
                kind=kind,
                name=action,
                arguments=dict(arguments),
                context=asdict(self.facts()),
            )
            await self.refresh()
            return result
        finally:
            self._invocations -= 1
            self._publish()

    @property
    def _summary_generation(self) -> Generation | None:
        """Admission guarantees one strategy; failed generations fall back until reload."""
        return next((entry.active for entry in self.entries.values() if entry.active.plugin.summarizer and not entry.active.error), None)

    @property
    def has_summarizer(self) -> bool:
        return self._summary_generation is not None

    async def summarize(self, text: str, validate: Callable[[str], None]) -> tuple[str, str] | None:
        """Lease the generation through manual compaction too; core validates semantics."""
        generation = self._summary_generation
        if generation is None or self._closed:
            return None
        self._invocations += 1
        try:
            summary = await generation.worker.request("compact", timeout=self.ACTION_TIMEOUT, text=text, context=asdict(self.facts()))
            validate(summary)
            return generation.plugin.name, summary
        except Exception as error:  # noqa: BLE001 - a broken strategy must not strand compaction.
            generation.error = f"summarizer: {error}"
            return None
        finally:
            self._invocations -= 1
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
        for entry in self.entries.values():
            self._retire(entry.active)
            if entry.pending:
                self._retire(entry.pending)
        for candidate in self._pending_new.values():
            self._retire(candidate)
        self.entries.clear()
        self._pending_new.clear()
        await asyncio.gather(*tuple(self._retiring), return_exceptions=True)
