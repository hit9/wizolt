"""Assembly adapter: project-local installations and agent-local live plugin state."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from wizolt.plugins.catalog import Installation, PluginCatalog
from wizolt.plugins.loading import PluginSource
from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, PluginError

if TYPE_CHECKING:
    from wizolt.session import Session


class SessionPlugins(PluginRuntime):
    """Bind generic plugin execution to one session; persist installation choices separately.

    A shared project catalog does not imply shared live state: existing agents keep their own
    generations, while future agents read the latest installation choices. Session snapshots do
    not serialize Python callbacks or inherit a parent's mutable plugin state.
    """

    def __init__(self, session: Session):
        from wizolt.session.store import SessionSnapshotStore

        self.session = session
        self.loaded = False
        self.problems: dict[str, str] = {}
        self._management_lock = asyncio.Lock()
        directory = Path(SessionSnapshotStore.project_dir(session.config.data_dir, session.cwd)) / "plugins"
        bundled = Path(__file__).parent / "builtin"
        defaults = tuple(Installation(path.stem, str(path), enabled=False) for path in sorted(bundled.glob("*.py")))
        self.catalog = PluginCatalog(directory, defaults)
        super().__init__(self.snapshot)

    def snapshot(self) -> Context:
        session = self.session
        state = session.state
        return Context(
            session.uid,
            session.agent_name,
            session.cwd,
            "waiting" if state.awaiting_input else state.last_turn_status,
            session.usage.context_percent(state.context_percent),
            state.elapsed,
            session.config.provider.model,
            time.monotonic(),
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
                await super().manage("enable", item.path)
            except Exception as error:  # noqa: BLE001 - one bad plugin must not prevent startup.
                self.problems[item.name] = str(error)

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
        if action == "install":
            from wizolt.plugins.dependencies import DependencyEnvironment

            candidate = PluginSource.read(target)
            if candidate.name in records and records[candidate.name].path != candidate.path:
                raise PluginError(f"Plugin name {candidate.name!r} is already installed from another path")
            sources = [PluginSource.read(item.path) for item in records.values() if item.enabled and item.name != candidate.name]
            sources.append(candidate)
            if not candidate.dependencies:
                return await self._manage("enable", candidate.path)
            environment = DependencyEnvironment(self.catalog.directory / "environments", sources, self.session.cwd)
            result = await environment.prepare()
            self.catalog.save(Installation(candidate.name, candidate.path, launch=result["launch"]))
            return {"name": candidate.name, **result}
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
            self.catalog.save(Installation(name, path, action == "enable", item.launch if item else ""))
        if action in ("list", "inspect"):
            present = {item["name"] for item in result["plugins"]}
            result["plugins"].extend(
                {"name": item.name, "path": item.path, "status": "not loaded" if item.enabled else "disabled", "launch": item.launch}
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
