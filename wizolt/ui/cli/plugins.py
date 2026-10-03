"""Plugin management and bounded, theme-aware projection into host-owned UI surfaces."""

from __future__ import annotations

import json
import shlex
import shutil
from typing import TYPE_CHECKING, ClassVar

from prompt_toolkit.formatted_text import StyleAndTextTuples

from wizolt.agentsmd import display_path
from wizolt.base import Text
from wizolt.plugins.layout import INPUT_SLOTS, LayoutBudget
from wizolt.sdk import Line, Panel, PluginError
from wizolt.ui.bars import Fragments, clean, clip
from wizolt.ui.cli.modals import choice_application, picker_height
from wizolt.ui.render import Theme
from wizolt.ui.tui import ChoiceViewState

if TYPE_CHECKING:
    from wizolt.plugins.runtime import PluginRuntime
    from wizolt.plugins.session import SessionPlugins
    from wizolt.ui.cli.loop import CommandLoop


class PluginView:
    """Project the host's ordered cache under one height budget, without terminal IO."""

    INPUT_SLOTS: ClassVar = INPUT_SLOTS

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
                spans = row.spans if isinstance(row, Line) else (row,)
                fragments = [(Theme.fg(span.role if span.role in Theme.ROLES else "text"), clean(span.text)) for span in spans]
                result.extend(clip(fragments, max(0, columns)))
                remaining -= 1
        return result

    @staticmethod
    def input_rows(rows: int) -> int:
        """One height policy for live projection and offline component previews."""
        return LayoutBudget.input_rows(rows)

    @classmethod
    def project(cls, panels: dict[str, list[Panel]], columns: int, rows: int) -> dict[str, Fragments]:
        """All prompt-adjacent slots share one budget, in visual order, leaving input room."""
        remaining = cls.input_rows(rows)
        projected = {}
        for slot in cls.INPUT_SLOTS:
            fragments = cls.render(panels.get(slot, []), columns, remaining)
            projected[slot] = fragments
            if fragments:
                remaining -= 1 + sum(text.count("\n") for _, text in fragments)
        return projected

    def fragments(self, columns: int, rows: int) -> dict[str, StyleAndTextTuples]:
        self.runtime.resize(columns, rows)
        return {
            slot: list(parts) for slot, parts in self.project({slot: self.runtime.panels(slot, columns) for slot in self.INPUT_SLOTS}, columns, rows).items()
        }


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
        # The file name is the useful end of a long path: clip from the left, behind the rail.
        width = max(10, shutil.get_terminal_size((80, 24)).columns - 6)
        lines = [Text.clip_width(display_path(str(item["path"]))[::-1], width)[::-1]]
        if item.get("builtin"):
            lines.insert(0, "Built in · enabled per project")
        for key in ("fields", "commands", "tools", "slots", "themes"):
            if values := item.get(key):
                lines.append(f"{key.capitalize()}: {', '.join(values)}")
        # Presets arrive grouped by kind, and every kind is present even when it is empty.
        if presets := [f"{kind} {', '.join(names)}" for kind, names in item.get("presets", {}).items() if names]:
            lines.append(f"Presets: {'; '.join(presets)}")
        if item.get("summarizer"):
            lines.append("Compaction: supplies summary text")
        for key in ("error", "python"):
            if value := item.get(key):
                lines.append(f"{key.capitalize()}: {value}")
        for component in self.runtime.components():
            if component.plugin == name:
                lines.append(f"{component.slot}: {component.visibility} · {component.rows}/{component.available_rows} rows")
        return "\n".join(lines)

    def fragments(self) -> StyleAndTextTuples:
        self.state.height = picker_height()
        self.state.labels = self.labels(max(1, shutil.get_terminal_size((80, 24)).columns - 8))
        title = "Plugins · current agent"
        if self.notice:
            title += " · " + clean(self.notice)
        return self.state.fragments(title, self.preview, keys="↑/↓ j/k move · Enter manage · / search · Esc back")

    def labels(self, columns: int) -> dict[str, str]:
        """Reserve state columns before clipping names; recompute after terminal resizing."""
        states = max((len(str(item["status"])) for item in self.records.values()), default=0)
        width = min(max(map(len, self.records), default=0), 28, max(4, columns - states - 12))
        return {
            name: Text.clip_width(f"{Text.clip_width(name, width):<{width}}  {'enabled' if item['enabled'] else 'disabled':<8}  {item['status']}", columns)
            for name, item in self.records.items()
        }

    async def run(self) -> None:
        tui = self.loop.presentation.tui
        assert tui is not None
        current = ""
        while True:
            listing = await self.runtime.manage("list")
            self.records = {item["name"]: item for item in listing["plugins"]}
            names = tuple(self.records)
            if not names:
                self.loop.presentation.emit("No installed plugins. Ask the agent to create or install one with plugin-workshop.")
                return
            self.state = ChoiceViewState(names, self.labels(80), set(), height=picker_height())
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
        if entry and entry.active.plugin.components:
            choices += ("layout",)
        # Unlike convenience selectors, an action menu must not auto-accept its only item.
        # Opening a disabled plugin shows Enable; it does not itself grant activation.
        action = await choice_application(self.loop, name, choices, {}, "", set())
        if not isinstance(action, str):
            return
        if action == "layout":
            await self.arrange(name)
            return
        result = await self.runtime.manage(action, name)
        self.notice = f"{result['name']}: {result['status']}"

    async def arrange(self, name: str) -> None:
        """The human frontend uses the same host operation as a management plugin."""
        components = self.runtime.components()
        own = tuple(item.id for item in components if item.plugin == name)
        selected = own[0] if len(own) == 1 else await choice_application(self.loop, "Component", own, {}, "", set())
        if not isinstance(selected, str):
            return
        component = next(item for item in components if item.id == selected)
        siblings = [item.id for item in components if item.slot == component.slot]
        index = siblings.index(selected)
        actions = (*(("move up",) if index else ()), *(("move down",) if index + 1 < len(siblings) else ()), "reset slot order")
        action = await choice_application(self.loop, selected, actions, {}, "", set())
        if action == "reset slot order":
            await self.runtime.call_host("ui.components.reset_order", {"slot": component.slot})
        elif action in ("move up", "move down"):
            before = siblings[index - 1] if action == "move up" else ""
            after = siblings[index + 1] if action == "move down" else ""
            await self.runtime.call_host("ui.components.move", {"component": selected, "before": before, "after": after})
        else:
            return
        await self.runtime.refresh()
        self.notice = f"{selected}: {action}"


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
