"""Adapt declarative plugin appearance to the existing palette and format engines.

Validation is pure with respect to live UI state. Layout catalogs belong to each agent; the
global Theme is a projection of the focused agent only. A background reload cannot repaint it.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wizolt.plugins.protocol import Capabilities
from wizolt.sdk import PluginError
from wizolt.ui.bars import PRESETS, BarLayout
from wizolt.ui.render import Theme
from wizolt.ui.themes import Palette, compile_themes

if TYPE_CHECKING:
    from wizolt.plugins.runtime import PluginRuntime
    from wizolt.session import Session
    from wizolt.ui.cli.presentation import Presentation


@dataclass
class AppearanceContribution:
    themes: dict[str, Palette]
    presets: dict[str, dict[str, str]]

    @classmethod
    def compile(cls, plugin: Capabilities) -> "AppearanceContribution":
        prefix = f"plugins.{plugin.name}."
        themes, problems = compile_themes([(prefix + name, prefix + name, data) for name, data in plugin.themes.items()], Theme.BUILTIN, Theme.ROLES)
        presets = {kind: {prefix + name: source for name, source in values.items()} for kind, values in plugin.presets.items()}
        layout = BarLayout()
        for kind, values in presets.items():
            if kind not in ("statusbar", "divider"):
                raise PluginError(f"Unsupported preset kind: {kind}")
            layout.presets[kind].update(values)
            for source in values.values():
                problems.extend(layout.configure({kind: source}, Theme.bar_styles))
        if problems:
            raise PluginError("; ".join(problems))
        return cls(themes, presets)

    @classmethod
    def validate(cls, plugin: Capabilities) -> None:
        cls.compile(plugin)


class PluginAppearance:
    def __init__(self, runtime: "PluginRuntime", session: "Session", presentation: "Presentation", focused: Callable[[], bool]):
        self.runtime, self.session, self.presentation, self.focused = runtime, session, presentation, focused
        self.themes: dict[str, Palette] = {}
        self.identity: tuple[int, ...] = ()
        runtime.validate = AppearanceContribution.validate
        runtime.on_change = self.update

    def update(self) -> None:
        """Publication changes catalogs; painting merely consumes these prepared values."""
        identity = tuple(id(entry.active) for entry in self.runtime.entries.values())
        if identity == self.identity:
            return
        self.identity = identity
        themes = {}
        presets = {kind: dict(values) for kind, values in PRESETS.items()}
        for entry in self.runtime.entries.values():
            contribution = AppearanceContribution.compile(entry.active.plugin)
            themes.update(contribution.themes)
            for kind, values in contribution.presets.items():
                presets[kind].update(values)
        self.themes = themes
        self.presentation.status_bar.layout.project_presets(presets, Theme.bar_styles)
        if self.focused():
            self.activate()

    def activate(self) -> None:
        """Called on focus changes as well as publication; never run from a background painter."""
        Theme.project_plugins(self.themes)
        Theme.set_mode(Theme.resolve(self.session.settings.theme))
        Theme.configure_bar_themes(self.session.config.ui)
        if self.presentation.tui is not None:
            self.presentation.tui.invalidate()
