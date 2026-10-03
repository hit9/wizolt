"""Agent-local plugin generations with staged reload and bounded callback execution.

Installation is explicit. Merely finding a Python file never executes it. Each agent gets
fresh modules and registration scopes, so a child cannot inherit a parent's plugin state.
Import-time side effects remain the responsibility of trusted Python plugin authors.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from wizolt.plugins.loading import LoadedPlugin, PluginSource
from wizolt.sdk import Context, Event, Panel, PluginError, Text, Value


@dataclass
class Generation(LoadedPlugin):
    error: str = ""
    calls: int = 0
    seconds: float = 0.0


@dataclass
class Entry:
    """A live generation plus at most one rollback revision and one staged replacement.

    Staging never changes the callbacks seen by an in-flight turn. Superseding a candidate must
    close it immediately, otherwise repeated edits leak module identities without activation.
    """

    active: Generation
    previous: Generation | None = None
    pending: Generation | None = None
    disabling: bool = False

    def stage(self, candidate: Generation | None) -> None:
        if self.pending is not None:
            self.pending.close()
        self.pending = candidate
        self.disabling = candidate is None

    def publish(self) -> None:
        if self.pending is not None:
            if self.previous is not None:
                self.previous.close()
            self.previous, self.active, self.pending = self.active, self.pending, None

    def close(self) -> None:
        for generation in (self.active, self.previous, self.pending):
            if generation is not None:
                generation.close()


class PluginRuntime:
    """One agent owns this runtime; no global registry is changed by loading a plugin.

    Management is serialized and replacements wait for the turn lease. UI reads are synchronous,
    bounded callbacks. Failed renderers are suppressed until reload, avoiding a redraw error loop.
    """

    # Host budgets, not plugin-specific branches. Async timeouts are cooperative; panel limits
    # bound returned data before rendering and do not make arbitrary Python safe to execute.
    OBSERVER_TIMEOUT = 1.0
    ACTION_TIMEOUT = 60.0
    PANEL_ROWS = 12
    ROW_CHARACTERS = 4096

    def __init__(self, context: Callable[[], Context]):
        self.context = context
        self.entries: dict[str, Entry] = {}
        self._pending_new: dict[str, Generation] = {}
        self.turn_active = False
        self._invocations = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def prepare(path: str, source: str | None = None) -> Generation:
        loaded = LoadedPlugin.load(PluginSource.read(path, source))
        return Generation(loaded.source, loaded.plugin, loaded.module_name)

    async def manage(self, action: str, target: str = "") -> dict[str, Any]:
        """Prepare first, then stage: a bad candidate must leave live registrations untouched."""
        async with self._lock:
            if self._closed:
                raise PluginError("Plugin runtime is closed")
            if action in ("list", "inspect"):
                items = [self.describe(name, entry) for name, entry in self.entries.items() if not target or name == target]
                items.extend(self.staged(item) for name, item in self._pending_new.items() if not target or name == target)
                if target and not items:
                    raise PluginError(f"Unknown plugin: {target}")
                return {"plugins": items}
            if action in ("validate", "enable"):
                candidate = self.prepare(target)
                name = candidate.plugin.name
                if action == "validate":
                    candidate.close()
                    return {"name": name, "status": "validated", "version": candidate.source.digest}
                entry = self.entries.get(name)
                if entry and entry.active.source.path != candidate.source.path:
                    candidate.close()
                    raise PluginError(f"Plugin name {name!r} already belongs to {entry.active.source.path}")
                if entry is None:
                    pending = self._pending_new.get(name)
                    if pending and pending.source.path != candidate.source.path:
                        candidate.close()
                        raise PluginError(f"Plugin name {name!r} already belongs to {pending.source.path}")
                    if pending:
                        pending.close()
                    if self.busy:
                        self._pending_new[name] = candidate
                        return self.staged(candidate)
                    self.entries[name] = Entry(candidate)
                    return self.describe(name, self.entries[name])
            else:
                name = target
                entry = self.entries.get(name)
                if entry is None:
                    if action == "disable" and (pending := self._pending_new.pop(name, None)) is not None:
                        pending.close()
                        return {"name": name, "status": "disabled"}
                    raise PluginError(f"Unknown plugin: {name}")
                if action == "reload":
                    candidate = self.prepare(entry.active.source.path)
                elif action == "rollback":
                    if entry.previous is None:
                        raise PluginError(f"{name}: no previous version")
                    candidate = self.prepare(entry.previous.source.path, entry.previous.source.text)
                elif action == "disable":
                    entry.stage(None)
                    if not self.busy:
                        entry.close()
                        del self.entries[name]
                    return {"name": name, "status": "pending" if self.busy else "disabled"}
                else:
                    raise PluginError(f"Unknown plugin action: {action}")
            assert entry is not None
            entry.stage(candidate)
            if not self.busy:
                self._publish()
            return self.describe(name, entry)

    @staticmethod
    def staged(generation: Generation) -> dict[str, Any]:
        return {"name": generation.plugin.name, "path": generation.source.path, "status": "pending", "version": "", "pending_version": generation.source.digest}

    @property
    def busy(self) -> bool:
        return self.turn_active or self._invocations > 0

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
            "tools": {key: {"description": action.description, "parameters": dict(action.parameters)} for key, action in item.plugin.tools.items()},
            "slots": list(item.plugin.components),
            "error": item.error,
            "calls": item.calls,
            "seconds": round(item.seconds, 6),
        }

    def _publish(self) -> None:
        """Swap generations only when both turn and explicit invocation leases have ended.

        No await belongs in this transition: readers on the owning event loop must observe the
        complete old registry or the complete new one, never a partially published replacement.
        """
        if self.busy or self._closed:
            return
        for name, candidate in self._pending_new.items():
            self.entries[name] = Entry(candidate)
        self._pending_new.clear()
        for name, entry in tuple(self.entries.items()):
            if entry.disabling:
                entry.close()
                del self.entries[name]
            else:
                entry.publish()

    async def start_turn(self) -> None:
        self.turn_active = True
        await self.emit("turn.started")

    async def finish_turn(self) -> None:
        """Notify the old generation before releasing its lease, including on cancellation."""
        if not self.turn_active:
            return
        try:
            await self.emit("turn.finished")
        finally:
            self.turn_active = False
            self._publish()

    async def emit(self, name: str) -> None:
        event = Event(name, self.context())
        for entry in tuple(self.entries.values()):
            generation = entry.active
            for observer in generation.plugin.observers.get(name, ()):
                try:
                    async with asyncio.timeout(self.OBSERVER_TIMEOUT):
                        await observer(event)
                except Exception as error:  # noqa: BLE001 - observer failures must not fail the agent turn.
                    generation.error = f"{name}: {error}"

    def _read(self, generation: Generation, callback: Callable, context: Context) -> Any:
        """Measure synchronous reads; timing cannot preempt a blocking Python callback.

        Keeping UI callbacks pure and cheap is an author contract. Moving them to a thread would
        introduce concurrent access to agent-local state without making Python a safe sandbox.
        """
        started = time.monotonic()
        try:
            return callback(context)
        finally:
            generation.calls += 1
            generation.seconds += time.monotonic() - started

    def fields(self) -> dict[str, Value]:
        values: dict[str, Value] = {}
        context = self.context()
        for name, entry in self.entries.items():
            if entry.active.error:
                continue
            for key, callback in entry.active.plugin.fields.items():
                try:
                    value = self._read(entry.active, callback, context)
                    if type(value) not in (str, int, float, bool):
                        raise PluginError(f"Field {key} must return a scalar")
                    if isinstance(value, float) and not math.isfinite(value):
                        raise PluginError(f"Field {key} must return a finite number")
                    values[f"plugins.{name}.{key}"] = value
                except Exception as error:  # noqa: BLE001 - disable faulty callbacks instead of crashing rendering.
                    entry.active.error = f"field {key}: {error}"
        return values

    def panels(self, slot: str, columns: int = 80) -> list[Panel]:
        panels = []
        context = replace(self.context(), columns=columns)
        for entry in self.entries.values():
            callback = entry.active.plugin.components.get(slot)
            if callback is None or entry.active.error:
                continue
            try:
                panel = self._read(entry.active, callback, context)
                if not isinstance(panel, Panel) or len(panel.rows) > self.PANEL_ROWS:
                    raise PluginError(f"Component must return Panel with at most {self.PANEL_ROWS} rows")
                if any(
                    not isinstance(row, Text) or not isinstance(row.text, str) or not isinstance(row.role, str) or len(row.text) > self.ROW_CHARACTERS
                    for row in panel.rows
                ):
                    raise PluginError(f"Panel rows must be Text with at most {self.ROW_CHARACTERS} characters")
                panels.append(panel)
            except Exception as error:  # noqa: BLE001 - plugin UI is an external error boundary.
                entry.active.error = f"component {slot}: {error}"
        return panels

    async def invoke(self, name: str, kind: str, action: str, arguments: Mapping[str, Any]) -> str:
        """Pin callbacks until completion; cancellation releases the same lease as success.

        Authorization belongs to the caller (human command or core tool runner), not this
        execution layer. Never route model invocations through the human command parser.
        """
        if self._closed:
            raise PluginError("Plugin runtime is closed")
        if kind not in ("command", "tool"):
            raise PluginError(f"Unknown action kind: {kind}")
        entry = self.entries.get(name)
        if entry is None:
            raise PluginError(f"Unknown plugin: {name}")
        registry = entry.active.plugin.commands if kind == "command" else entry.active.plugin.tools
        operation = registry.get(action)
        if operation is None:
            raise PluginError(f"Unknown {kind}: {name}.{action}")
        if kind == "tool":
            from jsonschema import Draft202012Validator

            # Provider-side schema enforcement varies. Validate here before handing arguments
            # to user code; the static gateway schema cannot describe every plugin operation.
            error = next(Draft202012Validator(dict(operation.parameters)).iter_errors(arguments), None)
            if error is not None:
                raise PluginError(f"Invalid arguments for {name}.{action}: {error.message}")
        self._invocations += 1
        try:
            async with asyncio.timeout(self.ACTION_TIMEOUT):
                result = await operation.handler(self.context(), arguments)
        finally:
            self._invocations -= 1
            self._publish()
        if not isinstance(result, str):
            raise PluginError("Plugin actions must return text")
        return result

    async def close(self) -> None:
        self._closed = True
        for entry in self.entries.values():
            entry.close()
        for candidate in self._pending_new.values():
            candidate.close()
        self.entries.clear()
        self._pending_new.clear()
