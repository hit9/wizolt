"""`/theme`: one tabbed picker for how wizolt looks, and the direct forms that set one thing by name.

The tabs are colors, diff colors, statusbar, divider layout/sweep, and input prefixes.
Each row the cursor lands on is applied at once, so the screen around the picker is the preview;
Divider rows preview without choosing: Space pins a layout or sweep, and Tab jumps groups.
Enter saves the choices across tabs, and Esc restores all of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import shutil
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.layout.dimension import to_dimension
from prompt_toolkit.utils import get_cwidth

from wizolt.agentsmd import display_path
from wizolt.base import SELECTION_BACK, ConfigError, Text
from wizolt.config import ConfigFile
from wizolt.ui.bars import PRESETS
from wizolt.ui.cli import bars
from wizolt.ui.cli.modals import picker_height
from wizolt.ui.render import InputStyle, Theme, UiPrinter
from wizolt.ui.themes import DIFF_STYLES
from wizolt.ui.tui import TUI_MODAL_PENDING, ChoiceViewState, TabbedViewState, TuiApp

if TYPE_CHECKING:
    from wizolt.ui.cli.loop import CommandLoop

# Each tab's setting, in tab order, and the word `/theme KIND NAME` sets it by.
KINDS = ("theme", "diff", "statusbar", "divider", "sweep", "input")
TAB_KINDS = ("theme", "diff", "statusbar", "divider", "input")
TITLES = ("Colorscheme", "Diff", "StatusBar", "Divider", "Input")
LEGEND = "h/l tab · j/k move · / search · Enter save · Esc cancel"
# The row that keeps a layout no preset names: the user's own template, or an older preset.
CUSTOM = "custom"
_SAVE = object()


def theme_preview(_name: str) -> StyleAndTextTuples:
    """A few transcript rows drawn in the highlighted theme, which the picker has already applied."""
    fg = Theme.fg
    added = Theme.diff_style("diff.added.bg") + " " + Theme.diff_style("diff.added.fg")
    removed = Theme.diff_style("diff.removed.bg") + " " + Theme.diff_style("diff.removed.fg")
    rows: list[Sequence[tuple[str, str]]] = [
        UiPrinter.syntax_segments("def split_words(text: str) -> list[str]:", "python", fg("text")),
        [(removed, "-    return text.split()")],
        [(added, "+    return WORD.findall(text)")],
        [(fg("user"), "• tighten the tokenizer")],
        [(fg("tool"), "Edit "), *UiPrinter.tool_arg_segments('path="parser.py" replace_all=false', fg("text"))],
    ]
    return [fragment for row in rows for fragment in ((fg("muted"), "  │ "), *row, ("", "\n"))]


DIFF_STYLE_SAMPLE = """@@ -8,4 +8,4 @@ def tokenize(text):
 def tokenize(text: str) -> list[str]:
-    return text.split(" ", 1)  # first word
+    return text.split(maxsplit=1)  # first word
     count = len(words)
"""


def diff_style_preview(_name: str) -> StyleAndTextTuples:
    """A modified line in the highlighted diff style, which the picker has already applied."""
    note = f"{Theme.diff_style_name()} diff colors on the {Theme.name()} theme\n"
    return [(Theme.fg("muted"), note), *UiPrinter().diff_segments(DIFF_STYLE_SAMPLE, row_width=60)]


def save_theme(loop: CommandLoop, name: str) -> str:
    """Draw `name`, a theme, pair or `auto`, and save it as runtime.theme."""
    Theme.set_mode(Theme.resolve(name))
    loop.session.settings.theme = name
    result = f"Theme: {name}"
    if loop.session.config.path:
        try:
            ConfigFile.set_runtime(loop.session.config.path, "theme", name)
        except (OSError, ValueError, ConfigError) as error:  # tomlkit's ParseError is a ValueError
            result += f"\nNot saved to {loop.session.config.path}: {error}"
        else:
            result += f" (saved as runtime.theme in {display_path(loop.session.config.path)})"
    return result


def save_diff_style(loop: CommandLoop, name: str) -> str:
    """Draw diffs in `name`, a diff style or `auto`, and save it as ui.diff.style."""
    Theme.set_diff_style(name)
    previous = loop.session.config.ui.get("diff", {})
    loop.session.config.ui["diff"] = {**(previous if isinstance(previous, dict) else {}), "style": name}
    result = f"Diff colors: {name}"
    if loop.session.config.path:
        try:
            ConfigFile.set_ui_value(loop.session.config.path, ("ui", "diff"), "style", name)
        except (OSError, ValueError, ConfigError) as error:
            result += f"\nNot saved to {loop.session.config.path}: {error}"
        else:
            result += f" (saved as ui.diff.style in {display_path(loop.session.config.path)})"
    return result


def apply_input(loop: CommandLoop, style: InputStyle) -> None:
    loop.presentation.input_style = style
    if loop.presentation.tui is not None:
        loop.presentation.tui.set_input_style(style)


def save_input(loop: CommandLoop, style: InputStyle) -> str:
    apply_input(loop, style)
    table = {"prompt": style.prompt}
    if style.running is not None:
        table["running"] = style.running
    loop.session.config.ui["input"] = table
    result = f"Input: {style.prefix()!r} · running {style.prefix(running=True)!r}"
    if loop.session.config.path:
        try:
            ConfigFile.set_ui_value(loop.session.config.path, ("ui",), "input", table)
        except (OSError, ValueError, ConfigError) as error:
            result += f"\nNot saved to {loop.session.config.path}: {error}"
        else:
            result += f" (saved as ui.input in {display_path(loop.session.config.path)})"
    return result


class AppearancePicker:
    """The tabs' state: what each setting was when the picker opened, what the cursor chose, and
    one list per tab. Layouts are previewed on a copy of the statusbar's layout, which the command
    puts back afterwards; compiled templates are immutable, so the copy shares them safely."""

    def __init__(self, loop: CommandLoop, started: float) -> None:
        self.loop = loop
        self.started = started
        bar = loop.presentation.status_bar
        self.layout_before = bar.layout
        self.layout = copy.copy(bar.layout)
        self.layout.sources = dict(bar.layout.sources)
        self.layout.sweep = copy.copy(bar.layout.sweep)
        self.layout.errors = []
        bar.layout = self.layout
        self.mode_before = Theme.name()
        theme = Theme.canonical(loop.session.settings.theme or Theme.AUTO) or self.mode_before
        if theme in Theme.pairs():  # listed as the variant the terminal gets
            theme = Theme.resolve(theme)
        self.input_before = loop.presentation.input_style
        self.custom_input = self.input_before
        self.input_edit: list[str] | None = None
        self.input_field = 0
        self.input_error = ""
        input_name = (
            self.input_before.prompt.removeprefix("preset:") if self.input_before.prompt.startswith("preset:") and self.input_before.running is None else CUSTOM
        )
        self.original = {"theme": theme, "diff": Theme.selected_diff_style(), **{kind: self.preset(kind) for kind in KINDS[2:5]}, "input": input_name}
        self.selected = dict(self.original)
        self.tabs = TabbedViewState(TITLES)
        self.height = picker_height()
        self.width = shutil.get_terminal_size((80, 24)).columns
        self.lists = {kind: ChoiceViewState((), {}, set(), max_rows=20, height=self.height) for kind in TAB_KINDS}
        self.divider_focus = "divider:" + self.selected["divider"]

    @staticmethod
    def presets(kind: str) -> tuple[str, ...]:
        return {"statusbar": tuple(PRESETS["statusbar"]), "divider": bars.DIVIDER_CHOICES, "sweep": bars.SWEEP_CHOICES}[kind]

    def preset(self, kind: str) -> str:
        source = self.layout_before.sources[kind]
        name = source.removeprefix("preset:") if source.startswith("preset:") else ""
        return name if name in self.presets(kind) else CUSTOM

    def source(self, kind: str, name: str) -> str:
        return self.layout_before.sources[kind] if name == CUSTOM else "preset:" + name

    def kind(self) -> str:
        return TAB_KINDS[self.tabs.tab]

    def input_style(self, name: str) -> InputStyle:
        return self.custom_input if name == CUSTOM else InputStyle("preset:" + name)

    def current_list(self) -> ChoiceViewState:
        """The active tab's list, its rows refreshed: a theme switch changes what the diff tab
        offers and says about `auto`."""
        kind, state = self.kind(), self.lists[self.kind()]
        if kind == "theme":
            state.choices, state.labels = Theme.choices(), {Theme.AUTO: f"auto (follows the terminal: {Theme.resolve(Theme.AUTO)})"}
        elif kind == "diff":
            state.choices = Theme.diff_styles()
            state.labels = {
                Theme.AUTO: f"auto (this theme pairs with {Theme.active().diff_style})",
                **{name: f"{name} · {style.note}" for name, style in DIFF_STYLES.items()},
            }
        elif kind == "input":
            state.choices = (*InputStyle.PRESETS, CUSTOM)
            state.labels = {
                name: ("* " if name == self.selected[kind] else "  ") + ("custom (e to edit)" if name == CUSTOM else f"{name}   {prefix.strip()}")
                for name, prefix in (*InputStyle.PRESETS.items(), (CUSTOM, ""))
            }
        else:
            kinds = ("divider", "sweep") if kind == "divider" else (kind,)
            choices: list[str] = []
            state.labels = {}
            state.disabled = set()
            for setting in kinds:
                source = self.layout_before.sources[setting]
                presets = self.presets(setting) + ((CUSTOM,) if self.original[setting] == CUSTOM else ())
                if kind == "divider":
                    choices.append(setting)
                    state.disabled.add(setting)
                    state.labels[setting] = "Layout" if setting == "divider" else "Sweep"
                for name in presets:
                    row = setting + ":" + name if kind == "divider" else name
                    choices.append(row)
                    label = name
                    if name == CUSTOM:
                        label = f"current ({source.removeprefix('preset:')})" if source.startswith("preset:") else "custom (current)"
                    state.labels[row] = ("* " if name == self.selected[setting] else "  ") + label
            state.choices = tuple(choices)
        options = state.enabled()
        focused = self.divider_focus if kind == "divider" else self.selected[kind]
        if not state.searching and not state.query and focused in options:
            state.selected = options.index(focused)
        return state

    def apply(self, kind: str, name: str) -> None:
        if kind == "theme":
            Theme.set_mode(Theme.resolve(name))
        elif kind == "diff":
            Theme.set_diff_style(name)
        elif kind == "input":
            apply_input(self.loop, self.input_style(name))
        else:
            self.layout.configure({kind: self.source(kind, name)}, Theme.bar_styles)
        if self.loop.presentation.tui is not None:
            self.loop.presentation.tui.invalidate()

    def preview(self, kind: str) -> Any:
        if kind == "statusbar":
            return lambda _name: "The status bar below shows the highlighted layout."

        def draw(name: str) -> StyleAndTextTuples:
            if kind == "input":
                fragments = self.input_preview(self.input_style(name))
            else:
                fragments = (
                    theme_preview(name) if kind == "theme" else diff_style_preview(name) if kind == "diff" else bars.preview(self.loop, kind, self.started)
                )
            # Leave room for about six choices and the key legend. Small panes prioritize the
            # list; the surrounding prompt and statusbar still preview the selected theme.
            limit = max(0, min(5, self.height - 13)) if kind == "theme" else max(0, min(9, self.height - 8 - (kind == "divider")))
            rows = list(split_lines(fragments))
            if kind == "input" and limit < 4:
                rows = rows[1:4:2]
            rows = rows[:limit]
            return [fragment for row in rows for fragment in (*row, ("", "\n"))]

        return draw

    def input_preview(self, style: InputStyle) -> StyleAndTextTuples:
        def sample(running: bool, text: str) -> StyleAndTextTuples:
            prefix = style.prefix(running=running)
            return [("class:prompt", prefix), (Theme.fg("text"), Text.clip_width(text, max(0, self.width - 2 - get_cwidth(prefix))) + "\n")]

        return [
            (Theme.fg("muted"), "  Chat\n  "),
            *sample(False, "Explain this function"),
            (Theme.fg("muted"), "  Follow-up while running\n  "),
            *sample(True, "Add a test too"),
        ]

    def fragments(self) -> StyleAndTextTuples:
        # Inline modal bounds follow terminal resizes. Reuse the same budget for every
        # tab rather than keeping the dimensions from when the picker first opened.
        self.height = picker_height()
        self.width = shutil.get_terminal_size((80, 24)).columns
        tui = self.loop.presentation.tui
        if tui is not None and tui.app is not None:
            size = tui.app.output.get_size()
            self.height, self.width = TuiApp.modal_rows(size.rows), size.columns
            if tui.modal_window is not None:
                self.height = min(self.height, to_dimension(tui.modal_window.height).max)
        state = self.current_list()
        state.height = self.height - (self.kind() == "divider")
        titles = TITLES if self.width >= 60 else ("Colors", "Diff", "Status", "Divider", "Input")
        if self.width < 52:
            titles = ("Color", "Diff", "Bar", "Line", "Input")
        tabs: StyleAndTextTuples = [("", "  "), *UiPrinter.tab_segments(titles, self.tabs.tab), ("", "\n")]
        if self.height < (11 if self.input_edit is not None else 9):
            # At a few rows there is no room for a list plus a sample. Keep the focused
            # value and its controls visible so a resize cannot strand the user in a menu.
            if self.input_edit is not None:
                label = ("Chat", "Running")[self.input_field]
                focused = f"{label}: {self.input_edit[self.input_field]}▏"
                keys = "Tab field · Enter apply · Esc back"
            else:
                choice = state.selected_choice()
                focused = state.labels.get(choice, choice) if choice is not None else "no matches"
                keys = "↑↓ move · h/l tab · Enter save · Esc cancel"
                if self.kind() == "divider":
                    keys = "Space choose · Tab group · Enter save · Esc cancel"
                elif self.kind() == "input":
                    keys = "↑↓ move · e edit · Enter save · Esc cancel"
            rows = [("class:choice.selected", "  " + focused)]
            if self.height > 1:
                rows.insert(0, (Theme.fg("accent"), "  " + TITLES[self.tabs.tab] + (" /" + state.query if state.searching else "")))
            if self.height > 2:
                rows.append((Theme.fg("muted"), "  " + keys))
            if self.kind() == "input" and self.input_edit is None and self.height > 3 and choice is not None:
                rows.append((Theme.fg("text"), "  " + self.input_style(choice).prefix(running=False) + "Explain this function"))
            if self.input_error and self.height > 3:
                rows.append((Theme.fg("error"), "  " + self.input_error))
            return [(style, Text.clip_width(text, self.width) + ("\n" if index < len(rows) - 1 else "")) for index, (style, text) in enumerate(rows)]
        path = self.loop.session.config.path
        hint = (
            f"{'Full customization: edit config' if self.width >= 70 else 'Customize config:'} {display_path(path)}"
            if path
            else "Full customization: edit your config file"
        )
        config_hint = (Theme.fg("muted"), "  " + Text.clip_width(hint, self.width - 2) + "\n")
        if self.input_edit is not None:
            parts = [*tabs, (Theme.fg("muted"), "  Tab field · Ctrl-U clear · Enter apply · Esc back\n"), config_hint]
            for index, (label, value) in enumerate(zip(("Chat", "Running"), self.input_edit, strict=True)):
                parts.extend(
                    [
                        (Theme.fg("accent") if index == self.input_field else Theme.fg("muted"), f"  {label:8} "),
                        (Theme.fg("text"), Text.clip_width(value, self.width - 13)),
                        (Theme.fg("accent"), "▏" if index == self.input_field else ""),
                        ("", "\n"),
                    ]
                )
            if self.input_error:
                parts.append((Theme.fg("error"), "  " + Text.clip_width(self.input_error, self.width - 2) + "\n"))
            parts.append(("", "\n"))
            if not self.input_error:
                parts.extend(self.input_preview(InputStyle(*self.input_edit)))
            rows = 1 + sum(fragment[1].count("\n") for fragment in parts)
            return [*parts, ("", "\n" * max(0, self.height - rows))]
        # The list's own title row gives way to the tabs; its blank row after them stays.
        fragments = state.fragments(
            "",
            self.preview(self.kind()),
            keys=(
                "h/l tabs · j/k move · Tab group · Space choose · Enter save · Esc cancel"
                if self.kind() == "divider"
                else "h/l tab · j/k move · e edit · Enter save · Esc cancel"
                if self.kind() == "input"
                else LEGEND
            ),
        )
        # Keep navigation beside the tabs, rather than below a preview or padding.
        legend = fragments.pop(-2 if state.searching else -1)
        fragments[1] = config_hint
        if self.kind() == "divider":
            # Chosen values remain visible even when the cursor scrolls into the other group.
            fragments.insert(2, (Theme.fg("muted"), f"  Layout: {self.selected['divider']} · Sweep: {self.selected['sweep']}\n"))
        # Keep the list's filter text beside the tabs and its blank row below them.
        parts = [*tabs[:-1], fragments[0], legend, *fragments[1:]]
        # The inline window anchors to the bottom. Shorter tabs must occupy the same rows,
        # otherwise switching tabs moves the header and scrolls the context above the picker.
        rows = 1 + sum(fragment[1].count("\n") for fragment in parts)
        padding = max(0, self.height - rows)
        if padding:
            parts.append(("", "\n" * padding))
        return parts

    def preview_divider(self, row: str) -> None:
        self.divider_focus = row
        setting, name = row.split(":", 1)
        sources = {kind: self.source(kind, self.selected[kind]) for kind in ("divider", "sweep")}
        sources[setting] = self.source(setting, name)
        self.layout.configure(sources, Theme.bar_styles)
        if self.loop.presentation.tui is not None:
            self.loop.presentation.tui.invalidate()

    def handle_key(self, key: str, data: str = "") -> Any:
        if key == "c-c":
            return KeyboardInterrupt()
        if self.input_edit is not None:
            if key == "escape":
                self.input_edit = None
            elif key in {"tab", "s-tab"}:
                self.input_field = 1 - self.input_field
            elif key == "enter" and not self.input_error:
                self.custom_input = InputStyle(*self.input_edit)
                self.selected["input"] = CUSTOM
                self.apply("input", CUSTOM)
                self.input_edit = None
            else:
                value = self.input_edit[self.input_field]
                if key in {"backspace", "c-h"}:
                    value = value[:-1]
                elif key == "c-u":
                    value = ""
                elif key == "any" or len(key) == 1:
                    value += data or key
                self.input_edit[self.input_field] = value
            self.input_error = ""
            if self.input_edit is not None:
                try:
                    InputStyle(*self.input_edit)
                except ValueError as error:
                    self.input_error = str(error)
            return TUI_MODAL_PENDING
        state = self.current_list()
        kind, before = self.kind(), state.selected_choice()
        if kind == "input" and not state.searching and (key == "e" or (key == "any" and data == "e")):
            style = self.input_style(self.selected["input"])
            self.input_edit = [style.prefix(), style.prefix(running=True)]
            self.input_field = 0
            self.input_error = ""
            return TUI_MODAL_PENDING
        if not state.searching and key in {"h", "left", "l", "right"}:
            if kind == "divider":
                self.layout.configure({setting: self.source(setting, self.selected[setting]) for setting in ("divider", "sweep")}, Theme.bar_styles)
            self.tabs.switch(-1 if key in {"h", "left"} else 1)
            if self.kind() == "divider" and (row := self.current_list().selected_choice()) is not None:
                self.preview_divider(row)
            return TUI_MODAL_PENDING
        if kind == "divider" and not state.searching and before is not None:
            setting, name = before.split(":", 1)
            if key in {"space", " "} or (key == "any" and data == " "):
                self.selected[setting] = name
                self.preview_divider(before)
                return TUI_MODAL_PENDING
            if key in {"tab", "s-tab"}:
                other = "sweep" if setting == "divider" else "divider"
                options = state.enabled()
                target = other + ":" + self.selected[other]
                if target not in options:
                    target = next((row for row in options if row.startswith(other + ":")), before)
                state.selected = options.index(target)
                self.preview_divider(target)
                return TUI_MODAL_PENDING
        result = state.handle_key(key, data)
        if (landed := state.selected_choice()) is not None and landed != before:
            if kind == "divider":
                self.preview_divider(landed)
            else:
                self.selected[kind] = landed
                self.apply(kind, landed)
        if result is SELECTION_BACK:
            return None
        if isinstance(result, str):
            return _SAVE
        return result

    def restore(self) -> None:
        self.loop.presentation.status_bar.layout = self.layout_before
        Theme.set_mode(self.mode_before)
        Theme.set_diff_style(self.original["diff"])
        apply_input(self.loop, self.input_before)

    def save(self) -> list[str]:
        """Persist every setting a tab changed; the lines say what was saved where."""
        lines: list[str] = []
        for kind in KINDS:
            name = self.selected[kind]
            if name == self.original[kind] and (kind != "input" or self.input_style(name) == self.input_before):
                continue
            if kind == "theme":
                lines.append(save_theme(self.loop, name))
            elif kind == "diff":
                lines.append(save_diff_style(self.loop, name))
            elif kind == "input":
                lines.append(save_input(self.loop, self.input_style(name)))
            else:
                lines.append(bars.select_layout(self.loop, kind, self.source(kind, name)))
        return lines


async def pick(loop: CommandLoop) -> str | None:
    tui = loop.presentation.tui
    assert tui is not None
    picker = AppearancePicker(loop, time.monotonic())
    diff_before = Theme.selected_diff_style()

    async def animate() -> None:
        # The divider's previews sweep while the picker is open.
        while True:
            await asyncio.sleep(0.05)
            tui.invalidate()

    timer = asyncio.create_task(animate())
    result: Any = None
    try:
        result = await tui.show_modal(picker.fragments, picker.handle_key)
    finally:
        timer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timer
        # Every preview is undone first; saving then applies each change for good.
        picker.restore()
        tui.invalidate()
    if isinstance(result, KeyboardInterrupt):
        raise result
    if result is not _SAVE:
        return None
    lines = picker.save()
    if not lines:
        return None
    if picker.selected["theme"] != picker.original["theme"] or Theme.selected_diff_style() != diff_before:
        tui.recolor()
    if warning := Theme.true_color_warning():
        lines.append(warning)
    return "\n".join(lines)


def direct(loop: CommandLoop, kind: str, name: str) -> str:
    """`/theme diff|statusbar|divider|sweep|input NAME`: set one thing without the picker."""
    if kind == "diff":
        if name not in (Theme.AUTO, *DIFF_STYLES):
            return f"Unknown diff style: {name}. Available: {', '.join((Theme.AUTO, *DIFF_STYLES))}"
        result = save_diff_style(loop, name)
        if loop.presentation.tui is not None:
            loop.presentation.tui.recolor()
        return result
    if kind == "input":
        if name not in InputStyle.PRESETS:
            return f"Unknown input preset: {name}. Available: {', '.join(InputStyle.PRESETS)}"
        return save_input(loop, InputStyle("preset:" + name))
    return bars.select_layout(loop, kind, "preset:" + name)


async def theme_command(loop: CommandLoop, args: str) -> str | None:
    """`/theme`: the tabbed picker. `/theme NAME` switches the colorscheme, and `/theme diff NAME`,
    `/theme statusbar NAME`, `/theme divider NAME`, `/theme sweep NAME` and `/theme input NAME`
    set the others.

    The colorscheme is saved to runtime.theme, as Codex and Claude Code save theirs; the transcript
    already on screen is redrawn in it once, the way a resize redraws it."""
    problems = Theme.load_custom(loop.session.data_path("themes"), loop.session.config.ui.get("themes"))
    words = args.split()
    if len(words) == 2 and words[0] in KINDS[1:]:
        return "\n".join([*problems, direct(loop, words[0], words[1])])
    tui = loop.presentation.tui
    if args.strip():
        chosen = Theme.canonical(args)
        if chosen is None:
            return "\n".join([*problems, f"Unknown theme: {args.strip()}. Available: {', '.join(Theme.choices())}"])
        lines = [*problems, save_theme(loop, chosen)]
        if warning := Theme.true_color_warning():
            lines.append(warning)
        if tui is not None:
            tui.recolor()
        return "\n".join(lines)
    if tui is None or not loop.interactive_input:
        current = Theme.canonical(loop.session.settings.theme or Theme.AUTO) or Theme.name()
        listing = [("* " if name == current else "  ") + name for name in Theme.choices()]
        return "\n".join(
            [
                *problems,
                *listing,
                f"Diff styles: {', '.join((Theme.AUTO, *DIFF_STYLES))}",
                f"Statusbar presets: {', '.join(PRESETS['statusbar'])}",
                f"Divider layouts: {', '.join(bars.DIVIDER_CHOICES)}. Sweeps: {', '.join(bars.SWEEP_CHOICES)}",
                f"Input presets: {', '.join(InputStyle.PRESETS)}",
                "Use /theme NAME, or /theme diff|statusbar|divider|sweep|input NAME.",
            ]
        )
    for problem in problems:
        loop.presentation.emit(problem)
    return await pick(loop)
