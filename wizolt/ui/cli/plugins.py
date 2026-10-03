"""Plugin management and bounded, theme-aware projection into host-owned UI surfaces."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, ClassVar

from prompt_toolkit.formatted_text import StyleAndTextTuples

from wizolt.sdk import Panel, PluginError
from wizolt.ui.bars import clean, clip
from wizolt.ui.cli.modals import choice_application, picker_height
from wizolt.ui.render import Theme
from wizolt.ui.tui import ChoiceViewState

if TYPE_CHECKING:
    from wizolt.plugins.runtime import PluginRuntime
    from wizolt.plugins.session import SessionPlugins
    from wizolt.ui.cli.loop import CommandLoop


class PluginView:
    """Compose all plugins in registration order under one height budget, without terminal IO."""

    ROLES: ClassVar = frozenset(("text", "muted", "accent", "success", "warning", "error"))

    def __init__(self, runtime: PluginRuntime):
        self.runtime = runtime

    @classmethod
    def render(cls, panels: list[Panel], columns: int, rows: int) -> list[tuple[str, str]]:
        result = []
        remaining = rows
        for panel in panels:
            for row in panel.rows:
                if remaining <= 0:
                    return result
                if result:
                    result.append(("", "\n"))
                style = Theme.fg(row.role if row.role in cls.ROLES else "text")
                result.extend(clip([(style, clean(row.text))], max(0, columns)))
                remaining -= 1
        return result

    @staticmethod
    def input_rows(rows: int) -> int:
        """One height policy for live projection and offline component previews."""
        return min(6, max(0, rows // 4 - 2))

    def above_input(self, columns: int, rows: int) -> StyleAndTextTuples:
        """Use the host viewport; querying the shell's terminal can disagree after a resize."""
        rows = self.input_rows(rows)
        if not rows:
            return []
        return list(self.render(self.runtime.panels("above_input", columns), columns, rows))


class PluginManager:
    """Human management over the same lifecycle API used by live reload.

    Selectors close before an action starts. This keeps one modal owner,
    preserves cancellation, and avoids background mutations that outlive the manager. Rows show
    live state separately from saved installation preferences; pending never means active.
    """

    def __init__(self, loop: CommandLoop, runtime: SessionPlugins):
        self.loop = loop
        self.runtime = runtime
        self.records: dict[str, dict] = {}
        self.notice = ""
        self.state = ChoiceViewState((), {}, set())

    def preview(self, name: str) -> str:
        item = self.records[name]
        lines = [str(item["path"])]
        if item.get("builtin"):
            lines.insert(0, "Built in · enabled per project")
        for key in ("fields", "commands", "tools", "slots"):
            if values := item.get(key):
                lines.append(f"{key.capitalize()}: {', '.join(values)}")
        for key in ("error", "python"):
            if value := item.get(key):
                lines.append(f"{key.capitalize()}: {value}")
        return "\n".join(lines)

    def fragments(self) -> StyleAndTextTuples:
        self.state.height = picker_height()
        title = "Plugins · current agent"
        if self.notice:
            title += " · " + clean(self.notice)
        return self.state.fragments(title, self.preview, keys="↑/↓ j/k move · Enter manage · / search · Esc back")

    async def run(self) -> None:
        tui = self.loop.presentation.tui
        assert tui is not None
        current = ""
        while True:
            listing = await self.runtime.manage("list")
            self.records = {item["name"]: item for item in listing["plugins"]}
            names = tuple(self.records)
            width = max(map(len, names), default=0)
            labels = {name: f"{name:<{width}}  {'enabled' if item['enabled'] else 'disabled':<8}  {item['status']}" for name, item in self.records.items()}
            if not names:
                self.loop.presentation.emit("No installed plugins. Ask the agent to create or install one with plugin-workshop.")
                return
            self.state = ChoiceViewState(names, labels, set(), height=picker_height())
            if current in self.state.choices:
                self.state.selected = self.state.choices.index(current)
            if problems := listing.get("problems"):
                self.notice = "; ".join(problems)
            chosen = await tui.show_modal(self.fragments, self.state.handle_key)
            if not isinstance(chosen, str):
                return
            current = chosen
            try:
                await self.manage(chosen)
            except Exception as error:  # noqa: BLE001 - keep a failed plugin recoverable in its manager.
                self.notice = f"Error: {error}"

    async def manage(self, name: str) -> None:
        """Collect user intent first, then invoke the lifecycle once with that exact action."""
        entry = self.runtime.entries.get(name)
        toggle = "disable" if self.records[name]["enabled"] else "enable"
        choices = ("reload", toggle) if entry else (toggle,)
        if entry and entry.previous is not None:
            choices += ("rollback",)
        # Unlike convenience selectors, an action menu must not auto-accept its only item.
        # Opening a disabled plugin shows Enable; it does not itself grant activation.
        action = await choice_application(self.loop, name, choices, {}, "", set())
        if not isinstance(action, str):
            return
        result = await self.runtime.manage(action, name)
        self.notice = f"{result['name']}: {result['status']}"


async def plugins_command(loop: CommandLoop, args: str) -> str:
    runtime = loop.session.plugins
    if runtime is None:
        return "Plugins are unavailable"
    try:
        parts = shlex.split(args)
        if not parts and loop.interactive_input and loop.presentation.tui is not None:
            await PluginManager(loop, runtime).run()
            return ""
        action = parts[0] if parts else "list"
        if action == "run":
            if len(parts) not in (3, 4):
                raise PluginError('Usage: /plugins run NAME COMMAND [\'{"key": "value"}\']')
            arguments = json.loads(parts[3]) if len(parts) == 4 else {}
            if not isinstance(arguments, dict):
                raise PluginError("Command arguments must be a JSON object")
            await runtime.load()
            return await runtime.invoke(parts[1], "command", parts[2], arguments)
        if len(parts) > 2:
            raise PluginError("Usage: /plugins [list|inspect|enable|reload|disable|rollback] [NAME]")
        if action not in ("list", "inspect", "enable", "reload", "disable", "rollback"):
            raise PluginError("Use wizolt plugin via plugin-workshop to create, validate or install plugins")
        if action == "enable":
            if len(parts) != 2:
                raise PluginError("Usage: /plugins enable NAME")
            await runtime.manage("inspect", parts[1])  # Only existing installations belong in this human manager.
        result = await runtime.manage(action, parts[1] if len(parts) > 1 else "")
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as error:  # noqa: BLE001 - plugin errors are user-visible, never a CLI crash.
        return f"Plugin error: {error}"
