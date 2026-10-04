"""Assembly adapter: user-level installations and agent-local live plugin state."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from wizolt.base import ConfigError
from wizolt.plugins.catalog import Installation, PluginCatalog
from wizolt.plugins.interception import InterceptionOrder
from wizolt.plugins.layout import LayoutPreferences
from wizolt.plugins.loading import PluginSource
from wizolt.plugins.presenters import PresenterChoices
from wizolt.plugins.process import WorkerError
from wizolt.plugins.runtime import PluginRuntime, Revision
from wizolt.plugins.settings import PluginSettings
from wizolt.sdk import Context, ContextWindow, PluginError, Usage

if TYPE_CHECKING:
    from wizolt.session import Session


class SessionPlugins(PluginRuntime):
    """Bind generic plugin execution to one session; persist installation choices separately.

    A shared user catalog does not imply shared live state: existing agents keep their own
    generations, while future agents read the latest installation choices. Session snapshots do
    not serialize Python callbacks or inherit a parent's mutable plugin state.
    """

    def __init__(self, session: Session):
        self.session = session
        self.loaded = False
        self.problems: dict[str, str] = {}
        self._management_lock = asyncio.Lock()
        self.catalog = PluginCatalog.for_user(session.config.data_dir, session.config.path)
        self.context_parts: tuple[tuple[str, int], ...] = ()
        self.read_context: Callable[[], list[tuple[str, int]]] | None = None
        super().__init__(self.snapshot, PluginSettings(session.config.plugins, session.config.path))
        self.order = LayoutPreferences(self.catalog.preferences)
        self.interception_order = InterceptionOrder(self.catalog.preferences)
        self.presenters.choices = PresenterChoices(self.catalog.preferences)

    def reload_layout(self) -> None:
        if self.reload_preferences is not None:
            self.reload_preferences()
        try:
            self.order.load()
            self._layout_version += 1
            self.problems.pop("layout", None)
        except (OSError, ValueError, ConfigError) as error:
            self.problems["layout"] = str(error)
        try:
            self.interception_order.load()
            self.problems.pop("interception order", None)
        except (OSError, ValueError, ConfigError) as error:
            self.problems["interception order"] = str(error)
        try:
            self.presenters.choices.load()
            self.problems.pop("presenters", None)
        except (OSError, ValueError, ConfigError) as error:
            self.problems["presenters"] = str(error)

    async def launch(self, revision: Revision):
        # Populate an idle/resumed agent's meter when enabling a plugin too. This is an
        # admission boundary, not a render/sample callback; the engine supplies the estimator.
        if self.read_context is not None:
            self.context_parts = tuple(self.read_context())
        records, _ = self.catalog.read()
        for item in records.values():
            if item.path == revision.source.path:
                revision.source.require_name(item.name)
        return await super().launch(revision)

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
            yolo=session.settings.yolo,
        )

    async def load(self) -> None:
        """Attempt startup once per agent; safe mode skips imports while preserving recovery tools.

        Startup holds the management lock too: a disable accepted while it loads must wait and
        then apply, not be undone by an activation from the records read before it.
        """
        async with self._management_lock:
            await self._load()

    async def _load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        self.reload_layout()
        records, _ = self.catalog.read()
        if os.environ.get("WIZOLT_NO_PLUGINS") == "1":
            return
        names, revisions = [], []
        for item in records.values():
            if not item.enabled:
                continue
            self.interpreters[item.name] = item.python
            try:
                revisions.append(self.read_revision(item.path))
                names.append(item.name)
            except Exception as error:  # noqa: BLE001 - one bad plugin must not prevent startup.
                self.problems[item.name] = str(error)
        # Plugins start concurrently; admission keeps saved order (see enable_many).
        for name, failure in zip(names, await self.enable_many(revisions), strict=True):
            if failure is not None:
                self.problems[name] = str(failure)

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
            if source.digest != live.revision.source.digest:
                changes.append("code changed")
            if settings != live.revision.settings:
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
            if os.environ.get("WIZOLT_NO_PLUGINS") == "1":
                raise PluginError("Plugins are disabled for this launch (--no-plugins); restart normally to reload")
            records, problems = self.catalog.read()
            self.reload_layout()
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
                        result = {"name": key, "status": "off"}
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
        """Serialize preference writes with live generation preparation.

        The runtime's lock only protects generations. Holding this separate admission lock
        prevents a late activation from silently undoing a disable accepted while it loaded.
        Rendering reads remain lock-free. Dependency installation belongs to the standalone CLI.
        """
        async with self._management_lock:
            return await self._manage(action, target)

    async def _manage(self, action: str, target: str) -> dict:
        if self._closed:
            raise PluginError("Plugin runtime is closed")
        if os.environ.get("WIZOLT_NO_PLUGINS") == "1" and action in {"enable", "reload"}:
            raise PluginError("Plugins are disabled for this launch (--no-plugins); restart normally to enable")
        await self._load()
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
            return {"name": target, "status": "off"}
        query = "list" if action == "inspect" else action
        result = await super().manage(query, "" if query == "list" else target, commit=self._save_choice if action in {"enable", "disable"} else None)
        if action in ("enable", "reload", "disable"):
            self.problems.pop(str(result["name"]), None)
        if action in ("list", "inspect"):
            present = {item["name"] for item in result["plugins"]}
            result["plugins"].extend(
                {"name": item.name, "path": item.path, "status": "off", "python": item.python} for item in records.values() if item.name not in present
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
            result["scope"] = "current agent; installation changes apply to future agents in any directory"
            result["runtime_directory"] = str(self.catalog.directory)
            result["config_path"] = str(self.catalog.preferences.path)
        return result

    def _save_choice(self, action: str, source: PluginSource) -> None:
        """Commit a validated preference before the runtime changes its live registry."""
        self.catalog.save(Installation(source.name, source.path, action == "enable", self.interpreters.get(source.name, "")))
