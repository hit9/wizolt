"""`/theme`: one tabbed picker for how wizolt looks, and the direct forms that set one thing by name.

The tabs are colors, diff colors, statusbar layout/placement, divider layout/sweep, and input
prefixes. Each row the cursor lands on is applied at once, so the screen around the picker is the
preview; bar rows preview without choosing: Space pins a choice, Tab jumps groups, and `f` opens
the bar's format to view, copy or edit. Enter saves the choices across tabs, and Esc restores all
of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import shutil
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.formatted_text.utils import fragment_list_width, split_lines
from prompt_toolkit.layout.dimension import to_dimension
from prompt_toolkit.utils import get_cwidth

from wizolt.agentsmd import display_path
from wizolt.base import SELECTION_BACK, ConfigError, Text
from wizolt.config import ConfigFile
from wizolt.tools import transcript
from wizolt.ui.bars import PRESETS, status_layout, status_source
from wizolt.ui.cli import bars, transcripts
from wizolt.ui.cli.formats import FormatPanel
from wizolt.ui.cli.modals import picker_height
from wizolt.ui.render import InputStyle, StatusBar, Theme, UiPrinter
from wizolt.ui.themes import DIFF_STYLES
from wizolt.ui.tui import TUI_MODAL_PENDING, ChoiceViewState, TabbedViewState, TuiApp

if TYPE_CHECKING:
    from wizolt.ui.cli.loop import CommandLoop

# Picker settings; the first six also name direct `/theme KIND NAME` commands.
KINDS = ("theme", "diff", "statusbar", "divider", "sweep", "input", "statusbar_theme", "divider_theme")
TAB_KINDS = ("theme", "diff", "statusbar", "divider", "input", "transcript")
TITLES = ("Colorscheme", "Diff", "StatusBar", "Divider", "Input", "Transcript")
LEGEND = ("j/k move", "h/l tab", "/ search")
CLOSING_KEYS = ("Enter save", "Esc cancel")


def fit_keys(keys: Sequence[str], width: int) -> str:
    """A tab's key legend in `width` columns. `keys` run from the most familiar, which give way
    first on a narrow pane; saving and cancelling are always shown."""
    shown = [*keys, *CLOSING_KEYS]
    while len(shown) > len(CLOSING_KEYS) and get_cwidth(" · ".join(shown)) > width:
        shown.pop(0)
    return " · ".join(shown)


# The row that keeps a layout no preset names: the user's own template, or an older preset.
CUSTOM = "custom"
# Where a statusbar preset puts its usage group. Not a setting: each is a different format.
PLACEMENTS = {"left": "All left", "split": "Left / Right"}
BAR_FORMATS = KINDS[2:5]  # the bar settings a format string holds
_SAVE = object()


def theme_preview(_name: str) -> StyleAndTextTuples:
    """Labeled color samples; dedicated tabs preview the statusbar, divider and input layout."""
    fg = Theme.fg
    added = Theme.diff_style("diff.added.bg") + " " + Theme.diff_style("diff.added.fg")
    removed = Theme.diff_style("diff.removed.bg") + " " + Theme.diff_style("diff.removed.fg")
    rows: list[tuple[str, Sequence[tuple[str, str]]]] = [
        ("Code", UiPrinter.syntax_segments("def split_words(text: str) -> list[str]:", "python", fg("text"))),
        ("Removed", [(removed, "-    return text.split()")]),
        ("Added", [(added, "+    return WORD.findall(text)")]),
        ("Your message", [(fg("user", f"bg:{Theme.color('user_bg')}"), " • tighten the tokenizer ")]),
        ("Reply", [(fg("text"), "Renamed "), (fg("text", "bold"), "split_words"), (fg("text"), " and kept the old name as an alias.")]),
        ("Tool call", [(fg("tool"), "Edit "), *UiPrinter.tool_arg_segments('path="parser.py" replace_all=false', fg("text"))]),
        ("Results", [(fg("success"), "12 passed"), (fg("warning"), " · 1 skipped"), (fg("error"), " · 0 failed")]),
        ("Menu selection", [("class:choice.selected", " /theme "), (fg("muted"), " Choose a color theme")]),
    ]
    return [fragment for label, row in rows for fragment in ((fg("muted"), f"  {label:15} "), *row, ("", "\n"))]


DIFF_STYLE_SAMPLE = """@@ -8,4 +8,4 @@ def tokenize(text):
 def tokenize(text: str) -> list[str]:
-    return text.split(" ", 1)  # first word
+    return text.split(maxsplit=1)  # first word
     count = len(words)
"""


def diff_style_preview(_name: str) -> StyleAndTextTuples:
    """A modified line in the highlighted diff style, which the picker has already applied."""
    note = f"{Theme.diff_style_name()} diff colors on the {Theme.name()} theme\n"
    return [(Theme.fg("muted"), note), *UiPrinter().diff_segments(DIFF_STYLE_SAMPLE)]


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
        placement = status_layout(self.layout_before.sources["statusbar"])
        self.original = {
            "theme": theme,
            "diff": Theme.selected_diff_style(),
            **{kind: self.preset(kind) for kind in BAR_FORMATS},
            "placement": CUSTOM if placement is None else "split" if placement[1] else "left",
            "input": input_name,
            "transcript": transcripts.name(transcripts.saved(loop), CUSTOM),
            **{transcripts.SETTINGS[group]: transcripts.current(loop, group) for group in ("thinking", "close")},
            **{kind + "_theme": Theme.selected_bar_theme(kind) for kind in ("statusbar", "divider")},
        }
        self.selected = dict(self.original)
        # The formats behind `custom` rows: what was saved, or a draft accepted in the format panel.
        self.custom = {kind: self.layout_before.sources[kind] for kind in BAR_FORMATS if self.original[kind] == CUSTOM}
        if self.original["transcript"] == CUSTOM:
            self.custom["transcript"] = transcripts.saved(loop)
        self.format: FormatPanel | None = None
        self.tabs = TabbedViewState(TITLES)
        self.height = picker_height()
        self.width = shutil.get_terminal_size((80, 24)).columns
        self.lists = {kind: ChoiceViewState((), {}, set(), max_rows=20, height=self.height) for kind in TAB_KINDS}
        self.bar_focus = {kind: kind + ":" + self.selected[kind] for kind in ("statusbar", "divider")}
        self.transcript_focus = "format:" + self.selected["transcript"]
        self.transcript_edit = self.selected["transcript"]

    def presets(self, kind: str) -> tuple[str, ...]:
        builtins = {"statusbar": tuple(PRESETS["statusbar"]), "divider": bars.DIVIDER_CHOICES, "sweep": bars.SWEEP_CHOICES}[kind]
        return (*builtins, *(name for name in self.layout.presets[kind] if name not in PRESETS[kind]))

    def preset(self, kind: str) -> str:
        source = self.layout_before.sources[kind]
        if source.startswith("preset:plugins.") and source[7:] in self.presets(kind):
            return source[7:]
        if kind == "statusbar":
            placement = status_layout(source)
            return CUSTOM if placement is None else placement[0]
        name = source.removeprefix("preset:") if source.startswith("preset:") else ""
        return name if name in self.presets(kind) else CUSTOM

    def source(self, kind: str, chosen: Mapping[str, str] | None = None) -> str:
        """The format a selection (the current one by default) gives one bar setting."""
        chosen = chosen or self.selected
        name = chosen[kind]
        if name == CUSTOM:
            return self.custom[kind]
        if kind == "statusbar" and name in PRESETS[kind]:
            return status_source(name, chosen["placement"] == "split")
        return "preset:" + name

    def choice(self, setting: str, name: str) -> dict[str, str]:
        """The selection with `setting` set to `name`. Placement is one toggle for whichever preset
        is shown, highlighted or chosen; only a custom format has none, and leaving one for a
        preset starts with both groups on the left."""
        chosen = {**self.selected, setting: name}
        if setting == "statusbar" and (name not in PRESETS[setting] or self.selected["placement"] == CUSTOM):
            chosen["placement"] = CUSTOM if name not in PRESETS[setting] else "left"
        return chosen

    def accept_format(self, setting: str, source: str) -> None:
        """Select an edited setting: as the preset (and placement) it spells, if any, else custom."""
        if setting == "statusbar" and (placement := status_layout(source)) is not None:
            self.selected.update(statusbar=placement[0], placement="split" if placement[1] else "left")
        elif name := next((name for name in self.presets(setting) if source in ("preset:" + name, self.layout.presets[setting][name])), None):
            self.selected[setting] = name
            if setting == "statusbar":
                self.selected["placement"] = CUSTOM
        else:
            self.custom[setting] = source
            self.selected[setting] = CUSTOM
            if setting == "statusbar":
                self.selected["placement"] = CUSTOM
        self.bar_focus[self.kind()] = setting + ":" + self.selected[setting]
        self.layout.configure({setting: self.source(setting)}, Theme.bar_styles)

    def transcript_value(self, group: str) -> str:
        """The picker's selection for one transcript group."""
        return self.selected[transcripts.SETTINGS[group]]

    def transcript_source(self, name: str) -> str:
        """The format a transcript selection stands for: the preset it names, or the template in
        hand -- the one accepted in the panel, else the one the config holds."""
        return "preset:" + name if name != CUSTOM else self.custom.get("transcript") or transcripts.saved(self.loop)

    def accept_transcript_format(self, value: str, source: str) -> None:
        """Select an edited record format. Edited back to the preset it came from it is that preset
        again; otherwise it is the custom format, which the tab then lists under its own row. The
        panel keeps showing the accepted text, so its value is what Enter will save."""
        accepted = value if source == transcripts.body("preset:" + value) else transcripts.name(source, CUSTOM)
        if accepted == CUSTOM:
            self.custom["transcript"] = source
        self.selected["transcript"] = accepted
        self.transcript_edit = accepted
        self.transcript_focus = "format:" + accepted

    def open_transcript_format(self, value: str) -> None:
        """Edit the format of the highlighted row -- the row, not the choice, so `f` on a preset the
        user has not chosen yet edits that preset. A draft accepted becomes the selection, and the
        panel reads `transcript_edit` from then on, so it shows what Enter would save."""
        self.transcript_edit = value
        self.transcript_focus = "format:" + value
        self.format = FormatPanel(
            "transcript",
            lambda: self.transcript_source(self.transcript_edit),
            transcripts.saved(self.loop),
            apply=transcripts.problems,
            accept=lambda source: self.accept_transcript_format(value, source),
            presets=transcript.PRESETS,
            preview=lambda: transcripts.preview(self.transcript_source(self.transcript_edit), self.width),
        )

    def open_format(self, setting: str) -> None:
        """Open the panel on `setting`, a bar format or the sweep. It shows the selection, so the
        preview leaves any merely highlighted row."""
        tab = self.kind()
        self.layout.configure({group: self.source(group) for group in self.groups(tab) if group in BAR_FORMATS}, Theme.bar_styles)
        self.format = FormatPanel(
            setting,
            lambda: self.source(setting),
            self.layout_before.sources[setting],
            apply=lambda source: self.layout.configure({setting: source}, Theme.bar_styles),
            accept=lambda source: self.accept_format(setting, source),
            # The divider's samples animate its sweep; the statusbar previews itself below.
            preview=(lambda: bars.preview(self.loop, "divider", self.started)) if tab == "divider" else None,
        )

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
        elif kind == "transcript":
            choices: list[str] = []
            state.labels = {}
            state.disabled = set()
            for group in transcripts.GROUPS:
                choices.append(group)
                state.disabled.add(group)
                state.labels[group] = transcripts.TITLES[group]
                # The custom row stays while any edited format is in hand, not only while the saved
                # one is: a draft accepted in the panel has to be visible to be chosen again.
                values = (*transcripts.choices(group), *((CUSTOM,) if group == "format" and "transcript" in self.custom else ()))
                chosen = self.transcript_value(group)
                for value in values:
                    row = group + ":" + value
                    choices.append(row)
                    label = transcripts.LABELS.get(value, value) if group != "format" else transcripts.format_label(value)
                    state.labels[row] = ("* " if value == chosen else "  ") + label
            state.choices = tuple(choices)
        elif kind == "input":
            state.choices = (*InputStyle.PRESETS, CUSTOM)
            state.labels = {
                name: ("* " if name == self.selected[kind] else "  ") + ("custom (e to edit)" if name == CUSTOM else f"{name}   {prefix.strip()}")
                for name, prefix in (*InputStyle.PRESETS.items(), (CUSTOM, ""))
            }
        else:
            choices: list[str] = []
            state.labels = {}
            state.disabled = set()
            for setting in self.groups(kind):
                choices.append(setting)
                state.disabled.add(setting)
                state.labels[setting] = "Colorscheme" if setting.endswith("_theme") else "Sweep" if setting == "sweep" else "Layout"
                if setting.endswith("_theme"):
                    presets = tuple(dict.fromkeys(("inherit", *Theme.choices())))
                    if self.selected[setting] not in presets:
                        presets += (self.selected[setting],)
                else:
                    presets = self.presets(setting) + ((CUSTOM,) if setting in self.custom else ())
                for name in presets:
                    row = setting + ":" + name
                    choices.append(row)
                    label = "inherit (follows Colorscheme)" if name == "inherit" else name
                    if name == CUSTOM and not setting.endswith("_theme"):
                        source = self.custom[setting]
                        label = f"current ({source.removeprefix('preset:')})" if source.startswith("preset:") else "custom (current)"
                        if source != self.layout_before.sources[setting]:
                            label = "custom (edited)"
                    state.labels[row] = ("* " if name == self.selected[setting] else "  ") + label
            state.choices = tuple(choices)
        options = state.enabled()
        focused = self.bar_focus[kind] if kind in self.bar_focus else self.transcript_focus if kind == "transcript" else self.selected[kind]
        if not state.searching and not state.query and focused in options:
            state.selected = options.index(focused)
        return state

    def apply(self, kind: str, name: str) -> None:
        if kind == "theme":
            Theme.set_mode(Theme.resolve(name))
        elif kind == "diff":
            Theme.set_diff_style(name)
        elif kind.endswith("_theme"):
            Theme.set_bar_theme(kind.removesuffix("_theme"), name)
        elif kind == "input":
            apply_input(self.loop, self.input_style(name))
        if self.loop.presentation.tui is not None:
            self.loop.presentation.tui.invalidate()

    def preview_limit(self, kind: str, framed: bool) -> int:
        # A roomy layout spends six rows on header spacing, borders and inner padding.
        # Charge these to the preview, not the choices, so navigation keeps a useful window.
        room = self.height - (6 if framed else 0)
        return max(0, min(8, room - 13)) if kind == "theme" else max(0, min(9, room - 8 - (kind == "divider")))

    def preview(self, kind: str, *, framed: bool = False, title: str = "") -> Any:
        if kind == "statusbar" and not framed:
            return lambda _name: Text.clip_width("The status bar below shows the highlighted layout and colors.", self.width - 4)

        def draw(name: str) -> StyleAndTextTuples:
            if kind == "statusbar":
                fragments = [(Theme.fg("muted"), "The status bar below previews the highlighted layout and colors.")]
            elif kind == "input":
                # Inside a frame the sample must fit the box, or clipping its padding draws "…".
                fragments = self.input_preview(self.input_style(name), self.box_width() - 4 if framed else self.width)
            elif kind == "transcript":
                group, _, value = name.partition(":")
                fragments = (
                    transcripts.thinking_preview(value, self.width)
                    if group == "thinking"
                    else transcripts.close_preview(value, self.width)
                    if group == "close"
                    else transcripts.preview(self.transcript_source(value), self.width)
                )
            else:
                fragments = (
                    theme_preview(name) if kind == "theme" else diff_style_preview(name) if kind == "diff" else bars.preview(self.loop, kind, self.started)
                )
            # Leave room for about six choices and the key legend. Small panes prioritize the
            # list; the surrounding prompt and statusbar still preview the selected theme.
            limit = self.preview_limit(kind, framed)
            rows = list(split_lines(fragments))
            # A trailing newline leaves a last row that is empty or holds only empty fragments.
            if rows and not fragment_list_width(rows[-1]):
                rows.pop()
            if kind == "input" and limit < 4:
                rows = rows[1:4:2]
            rows = rows[:limit]
            if framed:
                return self.preview_box(rows, title)
            rows = [StatusBar.clip_fragments(row, self.width) if fragment_list_width(row) > self.width else row for row in rows]
            return [fragment for row in rows for fragment in (*row, ("", "\n"))]

        return draw

    def box_width(self) -> int:
        return min(self.width - 4, 100)

    def preview_box(self, rows: list[StyleAndTextTuples], title: str) -> StyleAndTextTuples:
        """A bounded sample panel; preserve sample colors and measure terminal cells, not chars."""
        width = self.box_width()
        inside = width - 4
        border = Theme.fg("rule")
        caption = " " + Text.clip_width(title, width - 4) + " "
        parts: StyleAndTextTuples = [(border, "  ╭" + caption + "─" * (width - 2 - get_cwidth(caption)) + "╮\n")]
        for row in [[], *rows, []]:
            clipped = StatusBar.clip_fragments(row, inside)
            parts.extend([(border, "  │ "), *clipped, ("", " " * (inside - fragment_list_width(clipped))), (border, " │\n")])
        parts.append((border, "  ╰" + "─" * (width - 2) + "╯\n"))
        return parts

    def input_preview(self, style: InputStyle, columns: int | None = None) -> StyleAndTextTuples:
        def sample(running: bool, text: str) -> StyleAndTextTuples:
            prefix = style.prefix(running=running)
            width = max(0, (self.width if columns is None else columns) - 2 - get_cwidth(prefix))
            text = Text.clip_width(text, width)
            background = f"bg:{Theme.color('user_bg')}"
            return [(f"class:prompt {background}", prefix), (Theme.fg("text", background), text + " " * max(0, width - get_cwidth(text)) + "\n")]

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
        framed = self.height >= 20 and self.width >= 52
        state.height = self.height - (self.kind() in self.bar_focus) - (2 if framed else 0)
        titles = TITLES if self.width >= 60 else ("Colors", "Diff", "Status", "Divider", "Input", "Record")
        if self.width < 52:
            titles = ("Color", "Diff", "Bar", "Line", "Input", "Rec")
        tabs: StyleAndTextTuples = [("", "  "), *UiPrinter.tab_segments(titles, self.tabs.tab), ("", "\n")]
        if self.format is not None:
            # The panel keeps its keys first and its draft at the cursor at any height.
            rows = self.format.rows(max(1, self.width - 4), max(1, self.height - 1))
            parts = [*tabs, *(fragment for row in rows for fragment in (("", "  "), *row, ("", "\n")))]
            return [*parts, ("", "\n" * max(0, self.height - 1 - len(rows)))]
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
                if self.kind() in self.bar_focus:
                    keys = "Space choose · Tab group · f format · Enter save · Esc cancel"
                elif self.kind() == "input":
                    keys = "↑↓ move · e edit · Enter save · Esc cancel"
                elif self.kind() == "transcript":
                    keys = "↑↓ move · Space choose · Tab group · f format · Enter save · Esc cancel"
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
        shown = self.preview_limit("theme", framed)
        preview_title = {
            "theme": f"Color samples · {shown}/8 shown" + (" · enlarge to see all" if shown < 8 else ""),
            "diff": "Diff · removed, added and changed words",
            "statusbar": "Status bar · live preview below",
            "divider": "Divider · idle, running and queued",
            "input": "Input symbols · chat and follow-up",
            "transcript": "Transcript · a sample call at this format",
        }[self.kind()]
        fragments = state.fragments(
            "",
            self.preview(self.kind(), framed=framed, title=preview_title),
            preview_title=" " if framed else Text.clip_width(preview_title, self.width - 2),
            keys=fit_keys(
                ("j/k move", "h/l tabs", "Tab group", "Space choose", *(("p placement",) if self.kind() == "statusbar" else ()), "f format")
                if self.kind() in self.bar_focus
                else ("j/k move", "h/l tab", "e edit")
                if self.kind() == "input"
                else ("j/k move", "h/l tabs", "Tab group", "Space choose", "f format")
                if self.kind() == "transcript"
                else LEGEND,
                self.width - 2,
            ),
        )
        # Keep navigation beside the tabs, rather than below a preview or padding.
        legend = fragments.pop(-2 if state.searching else -1)
        fragments[1] = config_hint
        if self.kind() in self.bar_focus:
            # Chosen values remain visible even when the cursor scrolls into the other group.
            kind = self.kind()
            muted = Theme.fg("muted")
            summary: StyleAndTextTuples = [(muted, f"  Layout: {self.selected[kind]} · ")]
            if kind == "divider":
                summary.append((muted, f"Sweep: {self.selected['sweep']} · "))
            elif self.selected["placement"] == CUSTOM:
                # A template no preset produces is never rearranged; its groups move in the format.
                summary.append((muted, "custom format, f to edit · "))
            else:
                # Placement is a toggle on this line, not a list: p switches it.
                for name, label in PLACEMENTS.items():
                    summary.append(("class:tab.active" if name == self.selected["placement"] else "class:tab.inactive", f" {label} "))
                summary.append((muted, " p · "))
            summary.append((muted, f"Colors: {self.selected[kind + '_theme']}"))
            fragments[2:2] = [*StatusBar.clip_fragments(summary, self.width), ("", "\n")]
        # Keep the list's filter text beside the tabs and its blank row below them.
        gap = [("", "\n")] if framed else []
        parts = [*tabs[:-1], fragments[0], *gap, legend, fragments[1], *gap, *fragments[2:]]
        # The inline window anchors to the bottom. Shorter tabs must occupy the same rows,
        # otherwise switching tabs moves the header and scrolls the context above the picker.
        rows = 1 + sum(fragment[1].count("\n") for fragment in parts)
        padding = max(0, self.height - rows)
        if padding:
            parts.append(("", "\n" * padding))
        return parts

    @staticmethod
    def groups(kind: str) -> tuple[str, ...]:
        return (kind, "sweep", kind + "_theme") if kind == "divider" else (kind, kind + "_theme")

    def preview_bar(self, kind: str, row: str) -> None:
        """Preview the highlighted row as if it were chosen, with the other groups as they are."""
        self.bar_focus[kind] = row
        chosen = self.choice(*row.split(":", 1))
        self.apply(kind + "_theme", chosen[kind + "_theme"])
        self.layout.configure({group: self.source(group, chosen) for group in self.groups(kind) if group in BAR_FORMATS}, Theme.bar_styles)

    def handle_key(self, key: str, data: str = "") -> Any:
        if key == "c-c":
            return KeyboardInterrupt()
        if key == "any" and len(data) == 1 and data.isprintable():
            key = data  # a typed letter that has no binding of its own
        if self.format is not None:
            if not self.format.handle_key(key, data):
                self.format = None
                # A bar's preview is restored by re-applying its selection; the record tab has no
                # live preview to undo, only the selected format the sample draws from.
                if self.kind() in self.bar_focus:
                    self.preview_bar(self.kind(), self.bar_focus[self.kind()])
            return TUI_MODAL_PENDING
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
        if kind in self.bar_focus and not state.searching and key == "f":
            # The divider's Sweep rows open its formula; any other row, the bar's format.
            self.open_format("sweep" if self.bar_focus[kind].startswith("sweep:") else kind)
            return TUI_MODAL_PENDING
        if kind == "transcript" and not state.searching and before is not None:
            group, value = before.split(":", 1)
            if key == "f" and group == "format":
                self.open_transcript_format(value)
                return TUI_MODAL_PENDING
            if key in {"space", " "}:
                self.selected[transcripts.SETTINGS[group]] = value
                self.transcript_focus = before
                return TUI_MODAL_PENDING
            if key in {"tab", "s-tab"}:
                # Land on the first row of the neighbouring group, whatever the cursor was on.
                groups = transcripts.GROUPS
                other = groups[(groups.index(group) + (-1 if key == "s-tab" else 1)) % len(groups)]
                target = next((row for row in state.enabled() if row.startswith(other + ":")), before)
                state.selected = state.enabled().index(target)
                self.transcript_focus = target
                return TUI_MODAL_PENDING
        if kind == "statusbar" and not state.searching and key == "p" and self.selected["placement"] != CUSTOM:
            self.selected["placement"] = "left" if self.selected["placement"] == "split" else "split"
            self.preview_bar(kind, self.bar_focus[kind])
            return TUI_MODAL_PENDING
        if kind == "input" and not state.searching and key == "e":
            style = self.input_style(self.selected["input"])
            self.input_edit = [style.prefix(), style.prefix(running=True)]
            self.input_field = 0
            self.input_error = ""
            return TUI_MODAL_PENDING
        if not state.searching and key in {"h", "left", "l", "right"}:
            if kind in self.bar_focus:
                focused = self.bar_focus[kind]
                self.preview_bar(kind, kind + "_theme:" + self.selected[kind + "_theme"])
                self.bar_focus[kind] = focused
            self.tabs.switch(-1 if key in {"h", "left"} else 1)
            if self.kind() in self.bar_focus and (row := self.current_list().selected_choice()) is not None:
                self.preview_bar(self.kind(), row)
            return TUI_MODAL_PENDING
        if kind in self.bar_focus and not state.searching and before is not None:
            setting, name = before.split(":", 1)
            if key in {"space", " "}:
                self.selected = self.choice(setting, name)
                self.preview_bar(kind, before)
                return TUI_MODAL_PENDING
            if key in {"tab", "s-tab"}:
                groups = self.groups(kind)
                other = groups[(groups.index(setting) + (-1 if key == "s-tab" else 1)) % len(groups)]
                options = state.enabled()
                target = other + ":" + self.selected[other]
                if target not in options:
                    target = next((row for row in options if row.startswith(other + ":")), before)
                state.selected = options.index(target)
                self.preview_bar(kind, target)
                return TUI_MODAL_PENDING
        result = state.handle_key(key, data)
        if (landed := state.selected_choice()) is not None and landed != before:
            if kind in self.bar_focus:
                self.preview_bar(kind, landed)
            elif kind == "transcript":
                # Moving only moves: the tab lists three settings, so the highlighted row says
                # which one the sample belongs to and what Space would choose, and nothing more.
                self.transcript_focus = landed
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
        for kind in self.bar_focus:
            Theme.set_bar_theme(kind, self.original[kind + "_theme"])
        apply_input(self.loop, self.input_before)

    def save(self) -> list[str]:
        """Persist every setting a tab changed; the lines say what was saved where."""
        lines: list[str] = []
        for kind in ("theme", "diff", "statusbar_theme", "divider_theme", "transcript", *KINDS[2:6]):
            name = self.selected[kind]
            if kind in BAR_FORMATS:
                # A bar is saved when its format text changes. One the picker merely recognizes,
                # such as a saved placement template, stays exactly as it was written.
                source = self.source(kind)
                if source != self.layout_before.sources[kind]:
                    lines.append(bars.select_layout(self.loop, kind, source))
                continue
            if kind == "transcript":
                # The tab holds three settings, and any one of them can be the only change: `name`
                # is the format selection, the two look choices ride beside it.
                if self.transcript_source(name) != transcripts.saved(self.loop):
                    lines.append(transcripts.select(self.loop, "format", self.transcript_source(name)))
                for group in ("thinking", "close"):
                    setting = transcripts.SETTINGS[group]
                    if self.selected[setting] != self.original[setting]:
                        lines.append(transcripts.select(self.loop, group, self.selected[setting]))
                continue
            if name == self.original[kind] and (kind != "input" or self.input_style(name) == self.input_before):
                continue
            if kind == "theme":
                lines.append(save_theme(self.loop, name))
            elif kind == "diff":
                lines.append(save_diff_style(self.loop, name))
            elif kind.endswith("_theme"):
                lines.append(bars.select_theme(self.loop, kind.removesuffix("_theme"), name))
            elif kind == "input":
                lines.append(save_input(self.loop, self.input_style(name)))
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
    if len(words) == 3 and words[0] in ("statusbar", "divider") and words[1] == "theme":
        return "\n".join([*problems, bars.select_theme(loop, words[0], words[2])])
    if len(words) == 2 and words[0] in KINDS[1:6]:
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
                f"Statusbar presets: {', '.join(loop.presentation.status_bar.layout.presets['statusbar'])}",
                f"Statusbar theme: {Theme.selected_bar_theme('statusbar')}",
                f"Divider layouts: {', '.join(bars.DIVIDER_CHOICES)}. Sweeps: {', '.join(bars.SWEEP_CHOICES)}",
                f"Divider theme: {Theme.selected_bar_theme('divider')}",
                f"Input presets: {', '.join(InputStyle.PRESETS)}",
                "Use /theme NAME, /theme diff|statusbar|divider|sweep|input NAME, or /theme statusbar|divider theme NAME.",
            ]
        )
    for problem in problems:
        loop.presentation.emit(problem)
    return await pick(loop)
