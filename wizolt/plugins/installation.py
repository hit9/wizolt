"""Persistent plugin choices, usable from a standalone CLI without a live agent.

Changing a preference does not mutate any running interpreter. A live runtime must explicitly
reconcile it; the distinction prevents a shell command from claiming a reload it did not perform.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace

from wizolt.plugins.catalog import Installation, PluginCatalog
from wizolt.plugins.loading import PluginSource, unmet_dependencies
from wizolt.plugins.process import PluginProcess
from wizolt.plugins.protocol import Capabilities
from wizolt.plugins.settings import PluginSettings
from wizolt.sdk import PluginError


class PluginInstallations:
    def __init__(self, catalog: PluginCatalog, cwd: str, settings: PluginSettings | None = None):
        self.catalog = catalog
        self.cwd = cwd
        self.settings = settings or PluginSettings()
        self.validate: Callable[[Capabilities], None] | None = None

    def needs_environment(self, python: str, source: PluginSource) -> bool:
        """One verb enables: prepare a worker environment only when the saved one cannot serve."""
        if not python:
            return bool(unmet_dependencies(source.dependencies))
        if self.catalog.environment(python):
            from wizolt.plugins.dependencies import DependencyEnvironment

            return not DependencyEnvironment.built_for(python, source)
        return False  # An explicit custom interpreter remains the user's responsibility.

    async def manage(self, action: str, target: str = "") -> dict:
        records, problems = self.catalog.read()
        if action in ("list", "inspect"):
            if target and target not in records:
                raise PluginError(f"Unknown installed plugin: {target}")
            return {
                "plugins": [asdict(item) for name, item in records.items() if not target or name == target],
                "problems": problems,
                "runtime_directory": str(self.catalog.directory),
                "config_path": str(self.catalog.preferences.path),
                "scope": "saved user preferences; not live agent status",
            }
        if action == "disable":
            if target not in records:
                raise PluginError(f"Unknown installed plugin: {target}")
            item = replace(records[target], enabled=False)
        elif action == "enable":
            previous = records.get(target)
            source = PluginSource.read(previous.path if previous else target)
            for record in records.values():
                if record.path == source.path:
                    source.require_name(record.name)
            previous = records.get(source.name)
            if previous and previous.path != source.path:
                raise PluginError(f"Plugin name {source.name!r} is already installed from another path")
            python = previous.python if previous else ""
            if source.dependencies and self.needs_environment(python, source):
                from wizolt.plugins.dependencies import DependencyEnvironment

                result = await DependencyEnvironment(self.catalog.directory / "environments", [source], self.cwd, self.settings).prepare()
                python = result["python"]
            worker, description = await PluginProcess.start(source, python=python, cwd=self.cwd, config=self.settings.read(source.name))
            try:
                if self.validate is not None:
                    self.validate(Capabilities.decode(source.name, description))
            finally:
                await worker.close()
            item = Installation(source.name, source.path, python=python)
        else:
            raise PluginError(f"Unknown installation action: {action}")
        self.catalog.save(item)
        return {**asdict(item), "status": "saved", "note": "Use Plugin(action=reload) in an existing agent; new agents read this preference on startup."}
