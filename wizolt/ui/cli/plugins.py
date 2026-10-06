"""Plugin management and bounded, theme-aware projection into host-owned UI surfaces."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from functools import lru_cache
from typing import TYPE_CHECKING, ClassVar

from prompt_toolkit.formatted_text import StyleAndTextTuples

from wizolt.agentsmd import display_path
from wizolt.base import Text
from wizolt.formats import Fragments, clean, clip
from wizolt.plugins.layout import INPUT_SLOTS, LayoutBudget
from wizolt.plugins.loading import PluginSource
from wizolt.sdk import Line, Panel, PluginError
from wizolt.sdk import Text as PluginText
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

    @staticmethod
    @lru_cache(maxsize=128)
    def _row(row: Line | PluginText, columns: int, theme: tuple[str, int]) -> tuple[tuple[str, str], ...]:
        """Memoize pure projection, not live plugin state.

        Rows decoded from workers are immutable and bounded. Include width and the existing
        theme revision so resize/recolor needs no invalidation callbacks. Keep only 128 rows
        (at most 512 Ki characters of source text), including animated/retired generations;
        return immutable fragments so callers cannot corrupt another frame's cache entry.
        """
        spans = row.spans if isinstance(row, Line) else (row,)
        fragments = [(Theme.fg(span.role if span.role in Theme.ROLES else "text"), clean(span.text)) for span in spans]
        return tuple(clip(fragments, columns))

    @classmethod
    def render(cls, panels: list[Panel], columns: int, rows: int) -> list[tuple[str, str]]:
        result = []
        remaining = rows
        theme = Theme.key()
        for panel in panels:
            for row in panel.rows:
                if remaining <= 0:
                    return result
                if result:
                    result.append(("", "\n"))
                result.extend(cls._row(row, max(0, columns), theme))
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
        self.pages: dict[tuple[str, int], list] = {}  # Rendered About pages by (plugin, width).
        self.notice = ""
        self.state = ChoiceViewState((), {}, set())

    def preview(self, name: str) -> StyleAndTextTuples:
        """The plugin's About page, rendered, then the facts the host knows about it."""
        columns, rows = shutil.get_terminal_size((80, 24))
        width = max(10, columns - 6)
        fragments: StyleAndTextTuples = []
        page = self.page(name, width)
        limit = max(6, rows - 8)  # The list above adapts to the preview; leave it a few rows.
        for row in page[:limit]:
            fragments.extend((("class:choice.preview", "  │ "), *row, ("", "\n")))
        if len(page) > limit:
            fragments.append(("class:choice.preview", f"  │ … {len(page) - limit} more lines in this plugin's docstring\n"))
        fragments.append(("class:choice.preview", "  │\n"))
        fragments.extend(("class:choice.preview", "  │ " + line + "\n") for line in self.facts(name, width))
        return fragments

    def page(self, name: str, width: int) -> list:
        """Rendered once per plugin and width: the preview repaints on every key press."""
        key = (name, width)
        if key not in self.pages:
            from wizolt.ui.tui.details import DetailSheet

            self.pages[key] = DetailSheet._markdown_rows(self.about(name), width)
            while self.pages[key] and not self.pages[key][-1]:
                self.pages[key].pop()
        return self.pages[key]

    def facts(self, name: str, width: int) -> list[str]:
        item = self.records[name]
        # The file name is the useful end of a long path: clip from the left, behind the rail.
        lines = [Text.clip_width(display_path(str(item["path"]))[::-1], width)[::-1]]
        if item.get("builtin"):
            lines.insert(0, "Built in · enabled for your user")
        for key in ("fields", "commands", "tools", "slots", "themes"):
            if values := item.get(key):
                lines.append(f"{key.capitalize()}: {', '.join(values)}")
        # Presets arrive grouped by kind, and every kind is present even when it is empty.
        if presets := [f"{kind} {', '.join(names)}" for kind, names in item.get("presets", {}).items() if names]:
            lines.append(f"Presets: {'; '.join(presets)}")
        if intercepts := item.get("intercepts"):
            lines.append(f"Intercepts: {', '.join(intercepts)}")
        for key in ("error", "python"):
            if value := item.get(key):
                lines.append(f"{key.capitalize()}: {value}")
        for component in self.runtime.components():
            if component.plugin == name:
                lines.append(f"{component.slot}: {component.visibility} · {component.rows}/{component.available_rows} rows")
        return lines

    def fragments(self) -> StyleAndTextTuples:
        self.state.height = picker_height()
        self.state.labels = self.labels(max(1, shutil.get_terminal_size((80, 24)).columns - 8))
        title = "Plugins · current agent"
        if self.notice:
            title += " · " + clean(self.notice)
        # Read-only while a turn runs: browsing is safe, changes wait until the agent is idle.
        manage = "read-only while the agent works" if self.busy else "Enter manage"
        return self.state.fragments(title, self.preview, keys=f"↑/↓ j/k move · {manage} · / search · Esc back")

    @property
    def busy(self) -> bool:
        agent = getattr(self.loop, "agent", None)
        return bool(agent is not None and agent.turn_active)

    def labels(self, columns: int) -> dict[str, str]:
        """Reserve origin and state columns before clipping names on terminal resizing."""
        details = {
            name: f"{'builtin' if item.get('builtin') else 'user':<7}  {'enabled' if item['enabled'] else 'disabled':<8}  {item['status']}"
            for name, item in self.records.items()
        }
        reserved = max(map(len, details.values()), default=0) + 2
        width = min(max(map(len, self.records), default=0), 28, max(4, columns - reserved))
        return {name: Text.clip_width(f"{Text.clip_width(name, width):<{width}}  {detail}", columns) for name, detail in details.items()}

    async def run(self) -> None:
        tui = self.loop.presentation.tui
        assert tui is not None
        current = ""
        while True:
            listing = await self.runtime.manage("list")
            self.records = {item["name"]: item for item in listing["plugins"]}
            self.pages.clear()  # A reload may have changed a plugin's documentation.
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
            if self.busy:
                self.notice = "changes wait until the agent finishes; press Ctrl-C to stop it"
                continue
            try:
                await self.manage(chosen)
            except Exception as error:  # noqa: BLE001 - keep a failed plugin recoverable in its manager.
                self.notice = f"Error: {error}"

    async def manage(self, name: str) -> None:
        """Collect user intent first, then invoke the lifecycle once with that exact action."""
        entry = self.runtime.entries.get(name)
        toggle = "disable" if self.records[name]["enabled"] else "enable"
        choices = ("reload", toggle) if entry else (toggle,)
        # Unlike convenience selectors, an action menu must not auto-accept its only item.
        # Opening a disabled plugin shows Enable; it does not itself grant activation.
        action = await choice_application(self.loop, name, choices, {}, "", set())
        if not isinstance(action, str):
            return
        result = await self.runtime.manage(action, name)
        self.notice = f"{result['name']}: {result['status']}"

    def about(self, name: str) -> str:
        """The plugin's own documentation in Markdown, read from source without running it."""
        try:
            text = PluginSource.read(str(self.records[name]["path"])).about.strip()
        except Exception as error:  # noqa: BLE001 - an unreadable source still shows why.
            text = f"Could not read this plugin's source: {error}"
        return unwrap(text) or "This plugin has no documentation: its source starts with no docstring."


BLOCK_START = re.compile(r"\s*([-*+>#|]|\d+[.)])\s")


def unwrap(markdown: str) -> str:
    """Join a docstring's hard-wrapped prose lines, so paragraphs reflow to the viewer's width.

    The renderer keeps line breaks, which suits chat answers but leaves a source file's 100-column
    wrapping as ragged lines. Code blocks, list items, headings, quotes and tables keep theirs.
    """
    lines: list[str] = []
    fenced = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            lines.append(line)
            continue
        previous = lines[-1] if lines else ""
        continuation = line.strip() and previous.strip() and not BLOCK_START.match(line) and not previous.lstrip().startswith(("#", "```", "|"))
        if not fenced and continuation:
            lines[-1] = previous.rstrip() + " " + line.strip()
        else:
            lines.append(line)
    return "\n".join(lines)


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
            raise PluginError("Usage: /plugins [list|inspect|enable|reload|disable] [NAME]")
        if action not in ("list", "inspect", "enable", "reload", "disable"):
            raise PluginError("Use wizolt plugin via plugin-workshop to create, validate or enable new plugins")
        if action == "enable":
            if len(parts) != 2:
                raise PluginError("Usage: /plugins enable NAME")
            await runtime.manage("inspect", parts[1])  # Only existing installations belong in this human manager.
        result = await runtime.manage(action, parts[1] if len(parts) > 1 else "")
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as error:  # noqa: BLE001 - plugin errors are user-visible, never a CLI crash.
        return f"Plugin error: {error}"
