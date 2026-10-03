"""Assembly adapter: project-local installations and agent-local live plugin state."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from wizolt.plugins.catalog import Installation, PluginCatalog
from wizolt.plugins.loading import PluginSource
from wizolt.plugins.process import WorkerError
from wizolt.plugins.runtime import PluginRuntime
from wizolt.plugins.settings import PluginSettings
from wizolt.sdk import Context, ContextWindow, PluginError, Usage

if TYPE_CHECKING:
    from wizolt.session import Session


class SessionPlugins(PluginRuntime):
    """Bind generic plugin execution to one session; persist installation choices separately.

    A shared project catalog does not imply shared live state: existing agents keep their own
    generations, while future agents read the latest installation choices. Session snapshots do
    not serialize Python callbacks or inherit a parent's mutable plugin state.
    """

    def __init__(self, session: Session):
        self.session = session
        self.loaded = False
        self.problems: dict[str, str] = {}
        self._management_lock = asyncio.Lock()
        self.catalog = PluginCatalog.for_project(session.config.data_dir, session.cwd)
        self.context_parts: tuple[tuple[str, int], ...] = ()
        self.read_context: Callable[[], list[tuple[str, int]]] | None = None
        super().__init__(self.snapshot, PluginSettings(session.config.plugins, session.config.path))

    async def prepare(self, path: str, source: PluginSource | None = None, settings: dict | None = None, *, python: str | None = None):
        # Populate an idle/resumed agent's meter when enabling a plugin too. This is an
        # admission boundary, not a render/sample callback; the engine supplies the estimator.
        if self.read_context is not None:
            self.context_parts = tuple(self.read_context())
        source = source if source is not None else PluginSource.read(path)
        records, _ = self.catalog.read()
        for item in records.values():
            if item.path == source.path:
                source.require_name(item.name)
        return await super().prepare(path, source, settings, python=python)

    def snapshot(self) -> Context:
        session = self.session
        state = session.state
        now = time.monotonic()
        usage = session.usage
        fill = session.context_fill()
        return Context(
            session.uid,
            session.agent_name,
            session.cwd,
            "waiting" if state.awaiting_input else state.last_turn_status,
            session.usage.context_percent(state.context_percent),
            state.elapsed,
            session.config.provider.model,
            now,
            usage=Usage(usage.calls, usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens, state.estimated_output_rate(now) or 0),
            window=ContextWindow(
                fill["used"],
                session.config.provider.context_token_limit(session.settings.max_context_tokens),
                session.request_token_budget(),
                self.context_parts,
            ),
        )

    async def load(self) -> None:
        """Attempt startup once per agent; safe mode skips imports while preserving recovery tools."""
        if self.loaded:
            return
        self.loaded = True
        records, _ = self.catalog.read()
        if os.environ.get("WIZOLT_NO_PLUGINS") == "1":
            return
        for item in records.values():
            if not item.enabled:
                continue
            try:
                self.interpreters[item.name] = item.python
                await super().manage("enable", item.path)
            except Exception as error:  # noqa: BLE001 - one bad plugin must not prevent startup.
                self.problems[item.name] = str(error)

    def reload_plan(self, name: str = "") -> list[tuple[str, str]]:
        """What `hot_reload(name)` would change, as (plugin, change), without running plugin code.

        Versions come from source digests and settings from the config table, both read without
        executing setup; capabilities are only known once setup runs, so the reload result reports
        them. An unchanged plugin still restarts, which resets whatever it keeps in memory.
        """
        records, _ = self.catalog.read()
        plan = []
        for key, item in records.items():
            if name and key != name:
                continue
            entry = self.entries.get(key)
            live = entry.active if entry is not None else self._pending_new.get(key)
            if not item.enabled:
                if live is not None:
                    plan.append((key, "disable"))
                continue
            try:
                source = PluginSource.read(item.path)
                settings = self.settings.read(key)
            except Exception as error:  # noqa: BLE001 - the reload itself reports the failure in full.
                plan.append((key, f"will fail: {error}"))
                continue
            if live is None:
                plan.append((key, "enable"))
                continue
            changes = []
            if source.digest != live.source.digest:
                changes.append("code changed")
            if settings != live.settings:
                changes.append(f"settings changed in [plugins.{key}]")
            plan.append((key, ", ".join(changes) or self.UNCHANGED))
        return plan

    UNCHANGED = "restart"

    async def hot_reload(self, name: str = "") -> dict:
        """Reconcile saved choices into this agent only, with per-plugin failure isolation.

        No preference writes occur here. A CLI can save choices while an agent holds a turn
        lease; this method stages those choices and reports pending until publication is safe.
        """
        async with self._management_lock:
            if self._closed:
                raise PluginError("Plugin runtime is closed")
            records, problems = self.catalog.read()
            if name and name not in records:
                raise PluginError(f"Unknown installed plugin: {name}")
            self.loaded = True
            results = []
            for key, item in records.items():
                if name and key != name:
                    continue
                try:
                    self.interpreters[key] = item.python
                    if item.enabled:
                        result = await super().manage("enable", item.path)
                    elif key in self.entries or key in self._pending_new:
                        result = await super().manage("disable", key)
                    else:
                        result = {"name": key, "status": "disabled"}
                    self.problems.pop(key, None)
                    results.append(result)
                except Exception as error:  # noqa: BLE001 - one candidate must not prevent other reloads.
                    self.problems[key] = str(error)
                    results.append(
                        {
                            "name": key,
                            "status": "failed",
                            "error": str(error),
                            "previous_retained": key in self.entries,
                            **(WorkerError.diagnostics(error.log, error.traceback) if isinstance(error, WorkerError) else {}),
                        }
                    )
            return {"plugins": results, "problems": problems, "scope": "current agent"}

    async def manage(self, action: str, target: str = "") -> dict:
        """Serialize preference writes with preparation, including long dependency installs.

        The runtime's lock only protects generations. Holding this separate admission lock
        prevents a late installation from silently undoing a disable accepted while it built.
        Reads from rendering remain lock-free and never wait for package resolution.
        """
        async with self._management_lock:
            return await self._manage(action, target)

    async def _manage(self, action: str, target: str) -> dict:
        if self._closed:
            raise PluginError("Plugin runtime is closed")
        await self.load()
        records, problems = self.catalog.read()
        for item in records.values():
            self.interpreters[item.name] = item.python
        if action == "enable" and target in records:
            target = records[target].path
        if action == "enable":
            candidate = PluginSource.read(target)
            if candidate.name in records and records[candidate.name].path != candidate.path:
                raise PluginError(f"Plugin name {candidate.name!r} is already installed from another path")
        if action == "disable" and target not in self.entries and target not in self._pending_new and target in records:
            self.catalog.save(replace(records[target], enabled=False))
            self.problems.pop(target, None)
            return {"name": target, "status": "disabled"}
        query = "list" if action == "inspect" else action
        result = await super().manage(query, "" if query == "list" else target)
        if action in ("enable", "reload", "rollback", "disable"):
            self.problems.pop(str(result["name"]), None)
        if action in ("enable", "disable"):
            name = str(result["name"])
            item = records.get(name)
            path = str(result.get("path") or (item.path if item else ""))
            self.catalog.save(Installation(name, path, action == "enable", item.python if item else ""))
        if action in ("list", "inspect"):
            present = {item["name"] for item in result["plugins"]}
            result["plugins"].extend(
                {"name": item.name, "path": item.path, "status": "not loaded" if item.enabled else "disabled", "python": item.python}
                for item in records.values()
                if item.name not in present
            )
            # Preference and activation answer different questions. For example, disabling
            # during a turn persists immediately while its live generation remains pinned.
            for item in result["plugins"]:
                installed = records.get(item["name"])
                item["enabled"] = installed.enabled if installed else True
                item["installed"] = installed is not None
                item["builtin"] = item["name"] in self.catalog.defaults
            if target:
                result["plugins"] = [item for item in result["plugins"] if item["name"] == target]
                if not result["plugins"]:
                    raise PluginError(f"Unknown plugin: {target}")
            result["problems"] = [*(f"{name}: {error}" for name, error in self.problems.items()), *problems]
            result["scope"] = "current agent; installation changes apply to future agents in this project"
            result["catalog_directory"] = str(self.catalog.directory)
        return result
