"""wizolt terminal rendering, live output, and status display."""

from __future__ import annotations

import contextlib
import difflib
import io
import math
import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable, Hashable
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from itertools import accumulate, pairwise
from typing import TYPE_CHECKING, Any, ClassVar, Self

from prompt_toolkit import print_formatted_text
from prompt_toolkit.application import get_app_or_none, get_app_session
from prompt_toolkit.cache import SimpleCache
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import ANSI, FormattedText, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.output import ColorDepth, create_output
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.renderer import print_formatted_text as render_fragments_to_output
from prompt_toolkit.styles import ANSI_COLOR_NAMES, DEFAULT_ATTRS, Attrs, BaseStyle, Style, default_pygments_style, default_ui_style, merge_styles
from prompt_toolkit.utils import get_cwidth

from wizolt.base import (
    MEMORY_PREFIXES,
    MODEL_REQUEST_RETRIES,
    Json,
    LogBlock,
    LogEdge,
    LogLine,
    LogRole,
    Text,
)
from wizolt.ui.bars import BarLayout, Value
from wizolt.ui.themes import BUILTIN as NAMED_THEMES
from wizolt.ui.themes import DIFF_STYLES, Palette, blend, contrast, generated_pygments_style, lift, load_custom, normalize_color
from wizolt.utils import terminal

if TYPE_CHECKING:
    from pygments.style import Style as PygmentsStyle
    from rich.console import Console
    from rich.theme import Theme as RichTheme

    from wizolt.session import Session

try:
    import pygments
    from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
    from pygments.styles import get_style_by_name
    from pygments.token import Token
except ImportError:  # pragma: no cover - optional highlighting dependency
    pygments = Token = None
    get_lexer_by_name = get_lexer_for_filename = get_style_by_name = None


ScrollbackText = str | Callable[[int], str]


@dataclass(frozen=True)
class InputStyle:
    """Session-local input prefixes. Custom text is literal, never terminal control markup."""

    PRESETS: ClassVar[dict[str, str]] = {"default": "> ", "chevron": "❯ ", "arrow": "→ ", "lambda": "λ ", "bullet": "• "}
    prompt: str = "preset:default"
    running: str | None = None

    def __post_init__(self) -> None:
        for key, value in (("prompt", self.prompt), ("running", self.running)):
            if key == "running" and value is None:
                continue
            if not isinstance(value, str):
                raise TypeError(f"ui.input.{key}: must be a string")
            text = value
            if value.startswith("preset:"):
                name = value.removeprefix("preset:")
                if name not in self.PRESETS:
                    raise ValueError(f"ui.input.{key}: unknown preset {name!r}; choose from {', '.join(self.PRESETS)}")
                text = self.PRESETS[name]
            if len(text) > 64 or get_cwidth(text) > 32 or any(not char.isprintable() for char in text):
                raise ValueError(f"ui.input.{key}: use one line of printable text, at most 32 columns")

    def prefix(self, *, running: bool = False) -> str:
        source = self.running if running and self.running is not None else self.prompt
        text = self.PRESETS[source.removeprefix("preset:")] if source.startswith("preset:") else source
        return "+" + text if running and self.running is None else text

    @classmethod
    def load(cls, ui: dict) -> tuple[Self, list[str]]:
        table = ui.get("input", {})
        if not isinstance(table, dict):
            return cls(), ["ui.input must be a table"]
        if unknown := set(table) - {"prompt", "running"}:
            return cls(), [f"ui.input: unknown settings: {', '.join(sorted(unknown))}"]
        try:
            return cls(**table), []
        except (TypeError, ValueError) as error:
            return cls(), [str(error)]


class WidthDependent:
    """A completed block that keeps its source and lays itself out for the width it lands in.

    The projection replays recorded bytes, and bytes carry the width they were rendered at: a
    table laid out for 120 columns re-wraps by character into 80, and one laid out for 60 stays
    narrow in 120. Anything that subclasses this is re-rendered instead of replayed, which is what
    lets a resize re-flow it. Everything else is still replayed as captured.
    """

    def fragments(self, width: int) -> StyleAndTextTuples:
        raise NotImplementedError

    def __pt_formatted_text__(self) -> StyleAndTextTuples:
        return self.fragments(shutil.get_terminal_size((80, 20)).columns)


@dataclass(frozen=True)
class MessageBlock(WidthDependent):
    """A completed message, laid out by Rich for the width it is projected into.

    This is the piece that makes a resize re-flow prose, lists, tables and code instead of just
    re-wrapping the bytes Rich produced at some earlier width. It keeps what the message *is* --
    its text, who said it, its indent -- and runs the same render the live path runs, so a replay
    at the emit width is byte-identical to what was printed and a replay at another width is what
    Rich would have produced there.
    """

    printer: UiPrinter
    text: str
    role: str
    indent: int

    def ansi(self, width: int) -> str:
        # Rich loads on first render, not at import: the first frame needs no Markdown console.
        from wizolt.ui.markdown import markdown_console

        printer = self.printer
        console = markdown_console(width)
        with console.capture() as capture:
            printer.render_message(console, self.text, self.role, False, self.indent)
        cleaned = printer.strip_unknown_escapes(printer.strip_trailing_pad(capture.get()))
        # A block owns its inside, never its outside: the gap above it is `separate`'s to open and
        # the one below belongs to whatever comes next, so a document that opens on a heading or
        # closes on a list cannot smuggle in blank rows of its own.
        return cleaned.strip("\n") + "\n"

    def fragments(self, width: int) -> StyleAndTextTuples:
        return to_formatted_text(ANSI(self.ansi(width)))


@dataclass(frozen=True)
class LogBlockCell(WidthDependent):
    """A completed log block -- tool output, code, a diff -- laid out for the projected width.

    Diffs are the visible case: the gutter and the changed-text column are sized from the pane, so
    replaying rows built for a wider one leaves the gutter stranded mid-row once the pane narrows.
    Keeping the block means the diff is cut for the width it lands in.
    """

    printer: UiPrinter
    block: LogBlock

    def fragments(self, width: int) -> StyleAndTextTuples:
        return list(self.printer.log_segments(self.block, width))


@dataclass(frozen=True)
class HorizontalRule(WidthDependent):
    """A completed rule keeps its label and colors, but takes its width from the projection."""

    rule_style: str
    label: str = ""
    label_style: str = ""
    blank_after: bool = False

    def fragments(self, width: int) -> StyleAndTextTuples:
        width = max(1, width)
        parts: StyleAndTextTuples
        if not self.label:
            parts = [(self.rule_style, "─" * width)]
        else:
            lead = "── "[:width]
            limit = max(0, width - len(lead) - 1)
            label = self.label
            if get_cwidth(label) > limit:
                clipped = ""
                for char in label:
                    if get_cwidth(clipped + char) > max(0, limit - 1):
                        break
                    clipped += char
                label = clipped + "…" if limit else ""
            trail = max(0, width - len(lead) - get_cwidth(label))
            parts = [(self.rule_style, lead), (self.label_style, label), (self.rule_style, (" " + "─" * (trail - 1)) if trail else "")]
        parts.append(("", "\n\n" if self.blank_after else "\n"))
        return parts


class RecordedOutput(str):
    """Completed output that does not depend on width: the captured bytes, until the theme changes.

    It is the capture itself, so whatever reads the transcript as text still can. Calling it, as a
    replay does, draws it in the active theme: the capture until `/theme` switches, then its kept
    fragments re-rendered in the new colors. Until then a replay costs no more than the bytes.
    """

    parts: list[FormattedText | ANSI | WidthDependent]
    color_depth: ColorDepth
    theme: tuple[str, int]
    drawn: str

    def __new__(cls, ansi: str, parts: list[FormattedText | ANSI | WidthDependent], color_depth: ColorDepth) -> Self:
        recorded = super().__new__(cls, ansi)
        recorded.parts, recorded.color_depth = parts, color_depth
        recorded.theme, recorded.drawn = Theme.key(), ansi
        return recorded

    def __call__(self, columns: int) -> str:
        if self.theme != Theme.key():
            self.drawn = UiPrinter.render_to_ansi(self.parts, color_depth=self.color_depth)
            self.theme = Theme.key()
        return self.drawn


def markdown_table(headers: list[str], rows: list[tuple]) -> str:
    def cell(value: object) -> str:
        return Text.clean(str(value)).replace("\n", " ").replace("|", "\\|")

    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(cell(value) for value in row) + " |" for row in rows),
        ]
    )


MAX_RENDERED_SOURCES = 10


def search_sources_footer(sources: list[Json]) -> str:
    """A markdown source list for the provider-side searches a turn performed, or "" for none.

    This is presentation only. The sources stay on the messages that carry them, so the answer
    reaching history is exactly what the model wrote, and nothing new replays to the provider on
    the next turn."""
    seen = dict.fromkeys(url for source in sources if isinstance(source, dict) and (url := str(source.get("url") or "")))
    if not seen:
        return ""
    shown = list(seen)[:MAX_RENDERED_SOURCES]
    # Strip scheme and trailing slash for a compact one-line display.
    lines = [f"{index}. {url.split('://', 1)[-1].rstrip('/')}" for index, url in enumerate(shown, start=1)]
    if len(seen) > len(shown):
        lines.append(f"…and {len(seen) - len(shown)} more")
    return "\n".join(["", "**Sources**", "", *lines])


class _MemoizedStyle(BaseStyle):
    """A fixed style that resolves each style string once.

    prompt-toolkit caches resolved attributes only inside one live render; a transcript row is
    rendered on its own, so without this every fragment of every row paid the full rule scan.
    """

    def __init__(self, style: BaseStyle) -> None:
        self._style = style
        self._attrs: SimpleCache[tuple[str, Attrs], Attrs] = SimpleCache(maxsize=1024)

    def get_attrs_for_style_str(self, style_str: str, default: Attrs = DEFAULT_ATTRS) -> Attrs:
        return self._attrs.get((style_str, default), lambda: self._style.get_attrs_for_style_str(style_str, default))

    @property
    def style_rules(self) -> list[tuple[str, str]]:
        return self._style.style_rules

    def invalidation_hash(self) -> Hashable:
        return self._style.invalidation_hash()


class Theme:
    """The active semantic palette and its small Rich/prompt-toolkit adapters.

    `dark` and `light` are the defaults and follow the terminal's ANSI colors; the named schemes
    and the user's theme files are in `wizolt.ui.themes`.
    """

    DARK: ClassVar[dict[str, str]] = {
        "text": "default",
        "muted": "ansibrightblack",
        "subtle": "ansibrightblack",
        "accent": "ansicyan",
        "accent_secondary": "ansimagenta",
        "info": "ansiblue",
        "user": "#e0a96d",
        "user_bg": "#303238",
        "tool": "ansigreen",
        "success": "ansigreen",
        "warning": "ansiyellow",
        "error": "ansired",
        "rule": "ansibrightblack",
        "syntax_assign": "#79c0ff",
        "syntax_string": "#a5d6ff",
        "syntax_number": "#d2a8ff",
        "syntax_ident": "#a5d6ff",
        "syntax_builtin": "#79c0ff",
        "syntax_default": "#e6edf3",
        "status_base": "#cbd5e1",
        "status_provider": "#67d4e8",
        "status_model": "#d7b0ff",
        "status_reason": "#f0c77f",
        "status_mcp": "#93a4b7",
        "status_context": "#8dd6a1",
        "status_cache": "#80b8ef",
        "status_yolo": "#ff8f9c",
        "status_agent": "#e7ac80",
        "status_provider_bg": "#2f4e58",
        "status_model_bg": "#665378",
        "status_reason_bg": "#615641",
        "status_context_bg": "#3c5645",
        "status_cache_bg": "#384a60",
        "status_agent_bg": "#4b3f38",
        "status_yolo_bg": "#603d47",
        "divider_glow": "#67e8f9",
        "divider_rule": "#4b5563",
        "divider_label": "ansimagenta",
        "status_bg": "#2b2f36",
        "selection_bg": "#0077a8",
        "selection_fg": "#ffffff",
        "menu_bg": "#2b2f36",
        "menu_muted": "ansibrightblack",
        "pygments": "github-dark",
    }
    LIGHT: ClassVar[dict[str, str]] = {
        "text": "default",
        "muted": "ansibrightblack",
        "subtle": "ansibrightblack",
        "accent": "ansicyan",
        "accent_secondary": "ansimagenta",
        "info": "ansiblue",
        "user": "#9a5b2e",
        "user_bg": "#eeeeee",
        "tool": "ansigreen",
        "success": "ansigreen",
        "warning": "ansiyellow",
        "error": "ansired",
        "rule": "ansibrightblack",
        "syntax_assign": "#005cc5",
        "syntax_string": "#032f62",
        "syntax_number": "#6f42c1",
        "syntax_ident": "#032f62",
        "syntax_builtin": "#005cc5",
        "syntax_default": "#24292e",
        "status_base": "#4b5563",
        "status_provider": "#006d77",
        "status_model": "#7547a3",
        "status_reason": "#855d12",
        "status_mcp": "#596879",
        "status_context": "#236d48",
        "status_cache": "#285da6",
        "status_yolo": "#b63f58",
        "status_agent": "#a64b27",
        "status_provider_bg": "#b7d2d6",
        "status_model_bg": "#bca5d1",
        "status_reason_bg": "#dcd1a6",
        "status_context_bg": "#bcd2bf",
        "status_cache_bg": "#c1cfe1",
        "status_agent_bg": "#e0cfc4",
        "status_yolo_bg": "#e2bec9",
        "divider_glow": "#0e7490",
        "divider_rule": "#9ca3af",
        "divider_label": "ansimagenta",
        "status_bg": "#e8ebef",
        "selection_bg": "#0077a8",
        "selection_fg": "#ffffff",
        "menu_bg": "#e8ebef",
        "menu_muted": "ansibrightblack",
        # No GitHub light style ships with Pygments. Visual Studio's is as plain, and its blue
        # keywords sit with the GitHub-blue builtins above; `default` set bold green beside them.
        "pygments": "vs",
    }
    ROLES: ClassVar[tuple[str, ...]] = tuple(key for key in DARK if key != "pygments")

    # Diff colors are pinned, not derived from the palette: a palette reshuffle must never move
    # them. `dark` and `light` draw the `classic` style, tuned against real diffs in both
    # appearances; the foregrounds below apply under every style. `emph` is the heavier band under
    # the words a modified line actually changed; the line keeps its `bg` everywhere else.
    # The chrome around the bands -- the gutter rail and numbers, file and hunk heads, the +/-
    # signs -- is pinned the same way in both appearances; a theme file recolors any of it through
    # the `[diff]` keys `gutter`, `header`, `hunk`, `added_sign`, `removed_sign`.
    DIFF_DARK: ClassVar[dict[str, str]] = {
        **{key: "bg:" + color for key, color in (DIFF_STYLES["classic"].dark or {}).items()},
        "diff.added.fg": "fg:default",
        "diff.removed.fg": "fg:default",
        "diff.gutter": "fg:ansibrightblack",
        "diff.header": "fg:ansibrightblack",
        "diff.hunk": "fg:ansicyan",
        "diff.added.sign": "fg:ansigreen",
        "diff.removed.sign": "fg:ansired",
    }
    DIFF_LIGHT: ClassVar[dict[str, str]] = {
        **{key: "bg:" + color for key, color in (DIFF_STYLES["classic"].light or {}).items()},
        "diff.added.fg": "fg:#003b00",
        "diff.removed.fg": "fg:#520000",
        "diff.gutter": "fg:ansibrightblack",
        "diff.header": "fg:ansibrightblack",
        "diff.hunk": "fg:ansicyan",
        "diff.added.sign": "fg:ansigreen",
        "diff.removed.sign": "fg:ansired",
    }

    BUILTIN: ClassVar[dict[str, Palette]] = {"dark": Palette("dark", DARK), "light": Palette("light", LIGHT), **NAMED_THEMES}
    # The one name that is no theme: draw `dark` or `light` by the terminal's background.
    AUTO: ClassVar[str] = "auto"

    _mode: ClassVar[str] = "dark"  # the active theme's name
    _diff_style: ClassVar[str] = "auto"  # `ui.diff.style`: a DIFF_STYLES name, or `auto` for the theme's pairing
    _bar_themes: ClassVar[dict[str, str]] = {"statusbar": "inherit", "divider": "inherit"}
    _custom: ClassVar[dict[str, Palette]] = {}
    # Bumped whenever a theme's colors may have changed, including a theme file reloaded under the
    # same name, so anything rendered from the palette knows its colors are stale.
    _generation: ClassVar[int] = 0
    _transcript_style: ClassVar[tuple[tuple[str, int], BaseStyle] | None] = None
    _pygments_cache: ClassVar[dict[str, type[PygmentsStyle] | None]] = {}

    @classmethod
    def fzf_colors(cls) -> str:
        """The file picker shares the completion menu's surface and selection colors."""
        if cls.monochrome():
            return "bw"
        colors = cls.palette()
        accent = colors["accent"]
        if cls.active().background:
            accent = lift(accent, colors["syntax_default"], colors["menu_bg"], 4.5)
        values = {
            "fg": colors["syntax_default"],
            "bg": colors["menu_bg"],
            "fg+": colors["selection_fg"],
            "bg+": colors["selection_bg"],
            "hl": accent,
            "hl+": colors["selection_fg"],
            "query": colors["syntax_default"],
            "prompt": accent,
            "info": colors["menu_muted"],
            "header": colors["menu_muted"],
            "border": colors["menu_muted"],
            "separator": colors["menu_muted"],
            "pointer": colors["selection_fg"],
            "marker": accent,
            "spinner": accent,
            "gutter": colors["menu_bg"],
        }
        ansi = {name: str(index - 1) for index, name in enumerate(ANSI_COLOR_NAMES)}
        ansi["default"] = "-1"
        return ",".join([cls.appearance(), *(f"{key}:{ansi.get(value, value)}" for key, value in values.items())])

    @classmethod
    def bar_styles(cls, specs: set[str], kind: str = "statusbar") -> dict[str, str]:
        """Resolve template styles from semantic roles and theme highlight groups.

        Only color values and named text attributes cross this boundary, never raw toolkit
        style directives from configuration or interpolated model/provider names.
        """
        palette = cls.bar_palette(kind)
        colors = palette.colors
        background = palette.background or ("#ffffff" if palette.appearance == "light" else "#000000")
        # Solid segments use their own surface; a color readable on the transcript can disappear
        # on the popup grey or the badge. User highlight overrides still take precedence below.
        detail = colors["status_base"]
        context = colors["status_context"]
        badge = colors["divider_label"]
        warning, error = colors["warning"], colors["error"]
        if palette.background:
            detail = lift(detail, detail, colors["menu_bg"], 5.5)
            context = lift(context, detail, colors["status_bg"], 4.5)
            badge = lift(badge, detail, colors["menu_bg"], 4.5)
            # Pressure text needs to read on a band, while transcript warnings retain their hue.
            toward = "#000000" if palette.appearance == "light" else "#ffffff"
            warning = lift(warning, toward, colors["status_bg"], 4.5)
            error = lift(error, toward, colors["status_bg"], 4.5)

        def solid(color: str, *, bold: bool = False) -> str:
            # A foreground readable on the terminal can disappear on a colored segment.
            # Prefer the theme's own ink; use black/white only when neither native ink reads.
            ink = background
            if color.startswith("#"):
                candidates = [value for value in (colors["status_base"], background) if value.startswith("#")]
                ink = max(candidates, key=lambda value: contrast(value, color))
                if contrast(ink, color) < 4.5:
                    ink = max(("#000000", "#ffffff"), key=lambda value: contrast(value, color))
            return f"fg:{ink} bg:{color}" + (" bold" if bold else "")

        def tinted(role: str) -> str:
            base, accent = colors["status_bg"], colors[role]
            surface = blend(base, accent, 0.2) if base.startswith("#") and accent.startswith("#") else base
            # A tinted row carries all the semantic inks, including pressure and YOLO.
            # Move the surface toward the appearance's edge until each remains readable.
            toward = "#000000" if palette.appearance == "dark" else "#ffffff"
            inks = [color for name, color in colors.items() if name.startswith("status_") and not name.endswith("_bg")]
            for ink in (*inks, warning, error):
                surface = lift(surface, toward, ink, 4.5)
            ink = lift(colors["status_base"], colors["status_base"], surface, 5.5)
            return f"fg:{ink} bg:{surface}"

        # Bars are live rows. Inline colors must not be overridden by transcript role classes.
        groups = {role: f"fg:{colors[role]}" for role in cls.ROLES}
        groups.update(
            {
                "status.warning": f"fg:{warning}",
                "status.error": f"fg:{error}",
                "status.context": f"fg:{context} bg:{colors['status_bg']}",
                "status.agent": solid(colors["status_agent_bg"], bold=True),
                "status.provider": solid(colors["status_provider_bg"]),
                "status.model": solid(colors["status_model_bg"], bold=True),
                "status.reason": solid(colors["status_reason_bg"]),
                "status.yolo": solid(colors["status_yolo_bg"], bold=True),
                "status.detail": f"fg:{detail} bg:{colors['menu_bg']}",
                "status.cache": f"fg:{colors['status_cache']} bg:{colors['menu_bg']}",
                "status.cache.segment": solid(colors["status_cache_bg"]),
                "status.usage": solid(colors["status_context_bg"]),
                "status.usage.warning": solid(warning, bold=True),
                "status.usage.error": solid(error, bold=True),
                # A status line's own band, which a preset lays the whole row on.
                "status.band": f"fg:{colors['status_base']} bg:{colors['status_bg']}",
                "status.attention": solid(warning, bold=True),
                "divider.activity": f"fg:{background} bg:{colors['accent_secondary']} bold",
                "divider.metrics": f"fg:{detail} bg:{colors['menu_bg']}",
                "divider.badge": f"fg:{badge} bg:{colors['menu_bg']} bold",
                "divider.label": "class:divider.working",
                "spinner": f"fg:{colors['success']}",
            }
        )
        # Only these two layouts need a derived row surface. Ordinary statusbar and divider
        # refreshes must not compute contrast for unused tinted surfaces.
        for group, role in (("status.split", "status_provider"), ("status.monitor", "status_context")):
            if any(group in spec.split() for spec in specs):
                groups[group] = tinted(role)
        groups.update(palette.highlights)
        detected = terminal.background()
        result = {"__background": "#" + "".join(f"{value:02x}" for value in detected) if detected else background}
        for spec in specs:
            parts = []
            for token in spec.split():
                if token in groups:
                    parts.append(groups[token])
                elif token in ("bold", "italic", "underline", "reverse", "nobold", "noitalic", "nounderline", "noreverse"):
                    parts.append(token)
                elif token.startswith(("fg=", "bg=")):
                    value = token[3:]
                    color = normalize_color(colors.get(value, value))
                    if color is None:
                        raise ValueError(f"invalid color {value!r}")
                    parts.append(token[:2] + ":" + color)
                else:
                    raise ValueError(f"unknown highlight or style {token!r}")
            if not parts:
                raise ValueError("empty highlight")
            result[spec] = " ".join(parts)
        return result

    @classmethod
    def themes(cls) -> dict[str, Palette]:
        return {**cls.BUILTIN, **cls._plugins, **cls._custom}

    _plugins: ClassVar[dict[str, Palette]] = {}

    @classmethod
    def project_plugins(cls, themes: dict[str, Palette]) -> None:
        """Project the focused agent's catalog, never accumulate other agents' contributions."""
        if cls._plugins != themes:
            cls._plugins = dict(themes)
            cls._generation += 1

    @classmethod
    def name(cls) -> str:
        return cls._mode

    @classmethod
    def key(cls) -> tuple[str, int]:
        """Equal for two renders exactly when they drew in the same colors."""
        return cls._mode, cls._generation

    @classmethod
    def active(cls) -> Palette:
        """The active theme, or `dark` if a reload took the one in use away."""
        return cls.themes().get(cls._mode, cls.BUILTIN["dark"])

    @classmethod
    def bar_palette(cls, kind: str) -> Palette:
        selected = cls._bar_themes[kind]
        if selected == "inherit" or cls.canonical(selected) is None:
            return cls.active()
        return cls.themes()[cls.resolve(selected)]

    @classmethod
    def selected_bar_theme(cls, kind: str) -> str:
        return cls._bar_themes[kind]

    @classmethod
    def set_bar_theme(cls, kind: str, name: str) -> None:
        selected = "inherit" if name.strip().lower() == "inherit" else cls.canonical(name)
        if selected is None:
            raise ValueError(f"unknown ui.{kind}.theme {name!r}")
        cls._bar_themes = {**cls._bar_themes, kind: selected}
        cls._generation += 1

    @classmethod
    def configure_bar_themes(cls, ui: dict) -> list[str]:
        problems = []
        for kind in ("statusbar", "divider"):
            table = ui.get(kind, {})
            name = table.get("theme", "inherit") if isinstance(table, dict) else "inherit"
            try:
                if not isinstance(name, str):
                    raise TypeError(f"ui.{kind}.theme must be a string")
                cls.set_bar_theme(kind, name)
            except (TypeError, ValueError) as error:
                cls.set_bar_theme(kind, "inherit")
                problems.append(f"{error}; using inherit")
        return problems

    @classmethod
    def set_mode(cls, name: str) -> None:
        cls._mode = name if name in cls.themes() else "dark"
        cls._generation += 1

    @classmethod
    def load_custom(cls, directory: str, inline: object = None) -> list[str]:
        """Read theme files and configured definitions; return a line for each problem."""
        cls._custom, problems = load_custom(directory, cls.BUILTIN, cls.ROLES, inline)
        # Built-ins, `auto` and light/dark pairs already resolve case-insensitively; a custom
        # name differing only in case could otherwise be listed but never chosen.
        reserved = {name.lower() for name in (cls.AUTO, *cls.BUILTIN, *cls.pairs())}
        for name in [name for name in cls._custom if name.lower() in reserved]:
            del cls._custom[name]
            source = f"ui.themes.{name}" if isinstance(inline, dict) and name in inline else os.path.join(directory, name + ".toml")
            problems.append(f"theme {source}: `{name}` already means something to runtime.theme; choose another name")
        for kind, selected in cls._bar_themes.items():
            if selected != "inherit" and cls.canonical(selected) is None:
                cls.set_bar_theme(kind, "inherit")
                problems.append(f"unknown ui.{kind}.theme {selected!r}; using inherit")
        cls._generation += 1
        return problems

    @classmethod
    def configure(cls, configured: str, directory: str, inline: object = None) -> list[str]:
        """Load custom themes and activate the configured one, reporting what could not be used."""
        problems = cls.load_custom(directory, inline)
        name = cls.resolve(configured)
        if configured.strip() and cls.canonical(configured) is None:
            problems.append(f"unknown theme `{configured}`; using {name}. Available: {', '.join(cls.choices())}")
        cls.set_mode(name)
        return problems

    @classmethod
    def color_depth(cls, selected: ColorDepth) -> ColorDepth:
        """The depth to draw in, given the one the output selected for the terminal.

        The environment decides first -- `NO_COLOR` turns color off, and an explicit
        `PROMPT_TOOLKIT_COLOR_DEPTH` is obeyed -- for the transcript as it already is for the live
        app. Exact colors (`wants_true_color`) are drawn on a terminal that advertises true color;
        prompt-toolkit never reads `COLORTERM` itself and would round them to 256 colors. Anything
        else keeps the output's choice.
        """
        chosen = ColorDepth.from_env()
        if chosen is not None:
            return chosen
        if cls.wants_true_color() and cls.terminal_has_true_color():
            return ColorDepth.DEPTH_24_BIT
        return selected

    @classmethod
    def wants_true_color(cls) -> bool:
        """Whether the colors in use are exact values that rounding would spoil: a named scheme's,
        or a diff style's other than `classic`, the one tuned to survive 256 colors."""
        return cls.active().true_color or any(cls.bar_palette(kind).true_color for kind in cls._bar_themes) or cls.diff_style_name() != "classic"

    @staticmethod
    def terminal_has_true_color() -> bool:
        return os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")

    @classmethod
    def true_color_warning(cls) -> str | None:
        """What to tell the user when exact colors will be rounded to 256: a dark diff band can
        round to black. Silent when the environment chose the depth itself (`NO_COLOR`,
        `PROMPT_TOOLKIT_COLOR_DEPTH`)."""
        if not cls.wants_true_color() or cls.terminal_has_true_color() or ColorDepth.from_env() is not None:
            return None
        what = f"theme {cls.name()}" if cls.active().true_color else f"diff style {cls.diff_style_name()}"
        if not cls.active().true_color and cls.diff_style_name() == "classic":
            what = next(f"{kind} theme {cls.selected_bar_theme(kind)}" for kind in cls._bar_themes if cls.bar_palette(kind).true_color)
        return (
            f"The {what} is drawn in exact colors, but COLORTERM is not `truecolor`, so they are rounded "
            "to the nearest of 256 colors and dark diff lines can look black. If your terminal supports "
            "true color, add `export COLORTERM=truecolor` to your shell profile."
        )

    @classmethod
    def monochrome(cls) -> bool:
        return ColorDepth.from_env() is ColorDepth.DEPTH_1_BIT

    @classmethod
    def palette(cls) -> dict[str, str]:
        return cls.active().colors

    @classmethod
    def appearance(cls) -> str:
        return cls.active().appearance

    # prompt-toolkit spells a terminal color `ansibrightblack`; Rich spells the same one
    # `bright_black`. The palette speaks prompt-toolkit's dialect, since that is what most of the UI
    # is drawn in, and this is where the other one is translated. Hex passes through untouched.
    RICH_COLOR_NAMES: ClassVar[dict[str, str]] = {
        f"ansi{prefix}{name}": f"{'bright_' if prefix else ''}{name}"
        for prefix in ("", "bright")
        for name in ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
    }

    @classmethod
    def color(cls, role: str) -> str:
        """The active palette's color for one semantic role: a terminal color name or `#rrggbb`."""
        return cls.palette()[role]

    @classmethod
    def rich_color(cls, role: str) -> str:
        """One role in Rich's spelling of the same color."""
        color = cls.color(role)
        return cls.RICH_COLOR_NAMES.get(color, color)

    @classmethod
    def diff_style(cls, key: str) -> str:
        """A diff band: the selected diff style's, unless a theme file recolors that band.
        Foregrounds stay the appearance's pinned ones."""
        recolored = cls.active().diff.get(key)
        if recolored:
            return recolored
        band = (cls.diff_bands(cls.diff_style_name()) or {}).get(key)
        return "bg:" + band if band else (cls.DIFF_LIGHT if cls.appearance() == "light" else cls.DIFF_DARK)[key]

    @classmethod
    def diff_bands(cls, name: str) -> dict[str, str] | None:
        """A diff style's bands for the active appearance; None if it was not made for it."""
        style = DIFF_STYLES[name]
        return style.light if cls.appearance() == "light" else style.dark

    @classmethod
    def diff_style_name(cls) -> str:
        """The diff style in use: the one selected, or the active theme's pairing under `auto` and
        when the selected one was made only for the other appearance."""
        selected = cls._diff_style
        return selected if selected != cls.AUTO and cls.diff_bands(selected) else cls.active().diff_style

    @classmethod
    def diff_styles(cls) -> tuple[str, ...]:
        """What the picker offers: `auto`, then every style made for the active appearance."""
        return (cls.AUTO, *(name for name in DIFF_STYLES if cls.diff_bands(name)))

    @classmethod
    def selected_diff_style(cls) -> str:
        """The `ui.diff.style` setting as selected, `auto` included."""
        return cls._diff_style

    @classmethod
    def set_diff_style(cls, name: str) -> None:
        cls._diff_style = name if name in DIFF_STYLES else cls.AUTO
        cls._generation += 1

    @classmethod
    def configure_diff_style(cls, ui: dict) -> list[str]:
        """Select `ui.diff.style`, reporting a value that could not be used."""
        table = ui.get("diff", {})
        if not isinstance(table, dict):
            cls.set_diff_style(cls.AUTO)
            return ["ui.diff must be a table; using the theme's diff style"]
        name = table.get("style", cls.AUTO)
        cls.set_diff_style(name if isinstance(name, str) else cls.AUTO)
        if not isinstance(name, str) or (name != cls.AUTO and name not in DIFF_STYLES):
            return [f"unknown ui.diff.style {name!r}; using auto. Available: {', '.join((cls.AUTO, *DIFF_STYLES))}"]
        return []

    @classmethod
    def fg(cls, role: str, *attributes: str) -> str:
        """One role as a prompt-toolkit inline style, e.g. `fg:#8b949e class:role.muted bold`.

        For fragments built outside the style map — wrapped rows, ramps, anything assembled from a
        computed color. Fragments that can name a class should use one instead.

        The inline color is what any style draws; the `role.<name>` class after it wins only where
        a style defines it. `transcript_style` does, which is how a transcript row recorded under one
        theme re-renders in the next.
        """
        return cls.inline(role, f"class:role.{role}", *attributes)

    @classmethod
    def inline(cls, role: str, *attributes: str) -> str:
        """One role as a bare inline style, e.g. `fg:#8b949e bold`: what a style map's values take,
        since they may not name a class. Fragments use `fg`."""
        return " ".join((f"fg:{cls.color(role)}", *attributes))

    @classmethod
    def transcript_style(cls) -> BaseStyle:
        """The style transcript rows render in: the bare-print style plus every `role.<name>` class
        `fg` tags fragments with, in the active theme.

        One object per theme, not one per row: a merged style resolves attributes through its own
        cache, so a fresh merge per row paid the full lookup on every line -- ten times the cost
        of rendering the row without it. It also remembers each style string it has resolved: a
        `class:` name makes prompt-toolkit scan every rule for it, and the same few strings
        (`Theme.fg` of a handful of roles) recur on every emitted line.
        """
        cached = cls._transcript_style
        if cached is None or cached[0] != cls.key():
            roles = Style.from_dict({f"role.{role}": f"fg:{cls.color(role)}" for role in cls.ROLES})
            cached = cls.key(), _MemoizedStyle(merge_styles([_SCROLLBACK_STYLE, roles]))
            cls._transcript_style = cached
        return cached[1]

    @classmethod
    def selection(cls, *attributes: str) -> str:
        """The one band that says "this row is selected", for every list that has a cursor.

        It is a fixed pair rather than `reverse` on purpose: reverse inverts whatever color the row
        is already drawn in, so the band changed color from row to row -- green over a tool name,
        gray over a `tr.N` key. One band is one meaning, and it is the same band in the completion
        menu, the pickers, the browsers, and the approval actions.

        Without color (`NO_COLOR`) a background draws nothing, so there the band is `reverse`,
        the one mark a monochrome terminal still shows.
        """
        if cls.monochrome():
            return " ".join(("reverse", *attributes))
        return " ".join((f"fg:{cls.color('selection_fg')}", f"bg:{cls.color('selection_bg')}", *attributes))

    @classmethod
    def tui_styles(cls) -> dict[str, str]:
        """Semantic classes referenced directly by prompt-toolkit fragments."""
        return {role: f"fg:{cls.color(role)}" for role in ("text", "muted", "subtle", "accent", "rule", "success", "error")}

    @classmethod
    def rich_theme(cls) -> RichTheme:
        """Every role as a Rich style named `wizolt.<role>`, plus the Markdown element styles.

        Rich resolves an unknown style name by raising, so a console that renders our markup must
        be built with this theme; `markdown_console` is the only place that happens.
        """
        # Imported here, not at module scope: the first frame needs this module's palette but no
        # Rich, and Rich is only worth loading once Markdown is actually rendered.
        from rich.theme import Theme as RichTheme

        styles = {f"wizolt.{role}": cls.rich_color(role) for role in ("user", "error", "muted", "rule")}
        styles["wizolt.user"] += " on " + cls.rich_color("user_bg")
        return RichTheme({**styles, **cls.markdown_styles()}, inherit=True)

    @classmethod
    def markdown_styles(cls) -> dict[str, str]:
        """Restrained Markdown styles: hierarchy comes from shape and weight, not color."""
        muted, subtle = cls.rich_color("muted"), cls.rich_color("subtle")
        return {
            "markdown.h1": "bold",
            "markdown.h2": "bold",
            "markdown.h3": "bold",
            "markdown.h4": "italic",
            "markdown.h5": "italic",
            "markdown.h6": "italic",
            "markdown.h7": "italic",
            "markdown.paragraph": "none",
            "markdown.text": "none",
            "markdown.item": "none",
            # Inline code is the one colored span in prose; links and emphasis use shape only.
            "markdown.code": cls.rich_color("accent"),
            "markdown.code_block": "none",
            "markdown.block_quote": muted,
            "markdown.hr": cls.rich_color("rule"),
            "markdown.item.bullet": "bold",
            "markdown.item.number": "bold",
            "markdown.list": "none",
            "markdown.em": "italic",
            "markdown.emph": "italic",
            "markdown.strong": "bold",
            "markdown.s": "strike",
            "markdown.link": "underline",
            "markdown.link_url": muted,
            "markdown.table.border": subtle,
            "markdown.table.header": "bold",
        }

    @classmethod
    def ramp(cls, start_role: str, end_role: str, steps: int, *, kind: str | None = None) -> list[str]:
        """Interpolate `steps` hex colors from one role to another."""
        colors = cls.bar_palette(kind).colors if kind else cls.palette()
        start, end = cls.rgb(colors[start_role]), cls.rgb(colors[end_role])
        span = max(1, steps - 1)
        return [cls.mix(start, end, index / span) for index in range(steps)]

    @staticmethod
    def mix(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> str:
        return "#" + "".join(f"{round(channel + (channel_end - channel) * ratio):02x}" for channel, channel_end in zip(start, end, strict=True))

    @staticmethod
    def rgb(color: str) -> tuple[int, int, int]:
        value = color.rpartition(":")[2].lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    # A categorical series (`series`): OKLCH lightness and chroma per background, and the hue the
    # first color starts from. The golden angle moves each next hue as far from all earlier ones
    # as a sequence can, so consecutive colors never share a family however many there are.
    SERIES_TONE: ClassVar[dict[str, tuple[float, float]]] = {"dark": (0.78, 0.14), "light": (0.55, 0.15)}
    SERIES_START, GOLDEN_ANGLE = 250.0, 137.50776

    @classmethod
    def series(cls, count: int) -> list[str]:
        """`count` colors for things that must be told apart, e.g. the parts of a stacked bar.

        Generated rather than taken from the palette: themes derive their roles from a few hues,
        so roles alone cannot keep five neighbors apart in every theme. Equal OKLCH lightness and
        chroma keep the colors equally prominent and readable on the theme's background."""
        lightness, chroma = cls.SERIES_TONE["light" if cls.appearance() == "light" else "dark"]
        return [cls.oklch(lightness, chroma, cls.SERIES_START + index * cls.GOLDEN_ANGLE) for index in range(count)]

    @staticmethod
    def oklch(lightness: float, chroma: float, hue: float) -> str:
        """An OKLCH color as `#rrggbb`, its chroma reduced until it fits the sRGB gamut."""
        while True:
            a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
            l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
            m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
            s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
            linear = (
                4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
                -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
                -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
            )
            if all(0 <= channel <= 1 for channel in linear) or chroma <= 0:
                break
            chroma = max(0.0, chroma - 0.005)
        encoded = (12.92 * c if c <= 0.0031308 else 1.055 * max(0.0, c) ** (1 / 2.4) - 0.055 for c in linear)
        return "#" + "".join(f"{round(min(1.0, max(0.0, channel)) * 255):02x}" for channel in encoded)

    @classmethod
    def detect(cls) -> str:
        # The terminal's own answer first: most terminals report their background, while few
        # set COLORFGBG (Ghostty, kitty, WezTerm, Alacritty and tmux do not).
        reported = terminal.background()
        if reported is not None:
            return "light" if terminal.is_light(reported) else "dark"
        # COLORFGBG is "fg;bg" (rxvt/urxvt/Konsole) or "fg;;bg" (iTerm2). Only the standard
        # white entries are reliably light; index 8 is bright black and must remain dark.
        fgbg = os.environ.get("COLORFGBG", "")
        if ";" in fgbg:
            with contextlib.suppress(ValueError):
                bg = int(fgbg.rsplit(";", 1)[1])
                return "light" if bg in {7, 15} else "dark"
        return "dark"

    @classmethod
    def pairs(cls) -> list[str]:
        """Names with both a `-dark` and a `-light` theme, which follow the terminal like `auto`.
        `auto-dark` and `auto-light` stay two themes: their pair would be `auto` itself."""
        known = cls.themes()
        pairs = (name.removesuffix("-dark") for name in known if name.endswith("-dark") and name.removesuffix("-dark") + "-light" in known)
        return [pair for pair in pairs if pair.lower() != cls.AUTO]

    @classmethod
    def choices(cls) -> tuple[str, ...]:
        """What the picker lists: `auto`, then every theme. A pair is not listed beside its two
        themes, which crowded the menu, but `runtime.theme` still accepts one."""
        return (cls.AUTO, *cls.themes())

    @classmethod
    def canonical(cls, name: str) -> str | None:
        """`name` as `runtime.theme` spells it -- `auto`, a pair or a theme -- or None if it is none
        of them. Matched exactly, then ignoring case."""
        name, known = name.strip(), (*cls.choices(), *cls.pairs())
        if name in known:
            return name
        return next((choice for choice in known if choice.lower() == name.lower()), None)

    @classmethod
    def resolve(cls, configured: str) -> str:
        """Resolve explicit auto/pairs against the terminal; unknown names fall back to dark."""
        name = cls.canonical(configured or "") or "dark"
        if name == cls.AUTO:
            return cls.detect()
        if name in cls.pairs():
            return f"{name}-{cls.detect()}"
        return name

    @classmethod
    def pygments_style(cls) -> type[PygmentsStyle] | None:
        if pygments is None or get_style_by_name is None:
            return None
        name = cls.palette()["pygments"]
        if name not in cls._pygments_cache:
            try:
                cls._pygments_cache[name] = generated_pygments_style(name) or get_style_by_name(name)
            except Exception:  # noqa: BLE001 - optional Pygments styles must degrade to plain rendering.
                cls._pygments_cache[name] = None
        return cls._pygments_cache[name]


# The style `print_formatted_text` builds for a bare call, resolved once. Scrollback fragments
# carry inline colors from the theme, which every one of these resolves identically; keeping the
# same style here is what makes the recorded bytes equal to the ones the direct path writes.
_SCROLLBACK_STYLE = merge_styles([default_ui_style(), default_pygments_style()])


class UiPrinter:
    """Render completed output into native terminal scrollback.

    The durable half of the terminal boundary: what it prints survives the session and stays
    searchable with the terminal's own tools, so nothing here clears the screen. Live previews and
    status belong to the prompt-toolkit application instead.

    Because the output is permanent it is sanitized rather than passed through. Rich pads every line
    to the console width, which bakes trailing whitespace into scrollback and becomes wrap artifacts
    when the terminal is later narrowed, so padding is stripped unless it carries a background color
    and is part of a visible band. Terminal control strings prompt-toolkit cannot parse are stripped
    up front, since it drops their framing but leaks the payload as visible garbage.

    Color is decided once, from whether output is a real terminal.
    """

    PROMPT_PREFIX: ClassVar[str] = "> "
    USER_LOG_PREFIX: ClassVar[str] = "• "
    # How long scrollback emits wait before printing as one batch. Each print suspends the live
    # application (erase + repaint), so batching a burst of tool-result lines into one suspend
    # keeps the animated divider on screen; 30ms is well below human perception.
    SCROLLBACK_BATCH_WINDOW: ClassVar[float] = 0.03
    MCP_STATUS_RE: ClassVar[re.Pattern[str]] = re.compile(r"● (connected|connecting|disconnected|disconnecting|error|skipped)")
    MCP_STATUS_ANSI: ClassVar[dict[str, str]] = {
        "connected": "\x1b[32m",
        "connecting": "\x1b[32m",
        "disconnected": "\x1b[33m",
        "disconnecting": "\x1b[33m",
        "error": "\x1b[31m",
        "skipped": "\x1b[90m",
    }

    @classmethod
    def user_log_style(cls) -> str:
        return Theme.fg("user")

    TOOL_ARG_TOKEN: ClassVar[re.Pattern] = re.compile(
        r"""\s+|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_][\w.-]*=|(?:tr|job)\.\d+|\d+(?::\d+)?|[;,]|[^\s;,]+"""
    )

    def __init__(self, output_fn=print):
        self.output_fn = output_fn
        self.color = output_fn is print and sys.stdout.isatty()
        # Where rendered scrollback goes while something else owns the terminal. The TUI sets this
        # to its ScrollbackRegion, which needs every printed row -- not just the ones routed through
        # the ordered writer -- because a width change rebuilds the terminal from what it recorded,
        # and a row nobody recorded is a row the rebuild cannot put back. Startup banners, restored
        # transcript and the unwinding fallback path all print through here too.
        self.transcript_sink: Callable[[ScrollbackText], None] | None = None
        # Batch mode: while active, every emit appends its styled fragments instead of printing,
        # so a burst of output (the restored-transcript replay) is printed by a single
        # print_formatted_text call and flushes once. Under the TUI each call coordinates with the
        # renderer, so batching turns a hundred of those into one. Only the colored path batches.
        self._batch_parts: list[FormattedText | ANSI | WidthDependent] | None = None
        # Scrollback window: while a prompt_toolkit application is live, each print_formatted_text
        # suspends it (erase the whole rendered output, print above it, repaint), which makes the
        # animated divider visibly blink on every emit. A short window batches a burst of emits
        # (a tool result's lines) into one suspend; outside a live application nothing changes.
        self._scrollback_parts: list[FormattedText | ANSI | WidthDependent] = []
        self._scrollback_scheduled = False
        self._scrollback_generation = 0
        # Parts/scheduling state are touched from the app loop and late synchronous fallback
        # callers during shutdown, so all access goes through this lock.
        self._scrollback_lock = threading.Lock()
        # Rendered rows since the last full-width rule or user-turn boundary. Read by the loop
        # to keep rules away from another rule or the start of a user turn.
        self.rows_since_rule = 0
        # Blank rows currently sitting at the end of what has been printed. This is what makes the
        # gaps between blocks stable: a caller says "part this from what came before" through
        # `separate`, and saying it twice, or saying it under a rule that already left a gap, still
        # leaves one blank row. Starts at one, so the first block of a session does not open with a
        # blank row it has nothing to be parted from.
        self.trailing_blanks = 1
        # Whether the last thing printed was a call that fit on one line with nothing hanging off
        # it. A run of those reads as a list, so the callers that part blocks with a blank row skip
        # it between two of them; anything else printed clears the flag and the gap comes back.
        self.emitted_single_line = False

    @staticmethod
    def single_line_block(text: str | LogBlock) -> bool:
        """Whether a block is one log line: a call with no output, no children, no wrapped text."""
        if not isinstance(text, LogBlock) or len(text.items) != 1:
            return False
        line = text.items[0]
        return isinstance(line, LogLine) and "\n" not in (line.text + line.meta)

    def track_layout(self, text: str) -> None:
        """Record what one emit left on screen: rows drawn, and blank rows left at the end.

        Everything printed goes through here, so the two spacing questions -- how far the last rule
        is, and whether a gap is already there -- are answered from what actually reached the
        terminal rather than from each caller's guess about it.
        """
        self.rows_since_rule += text.count("\n")
        rows = text.split("\n")
        if rows and rows[-1] == "":
            rows.pop()  # the newline that ended the last row, not a row of its own
        blanks = 0
        for row in reversed(rows):
            if self.SGR_RE.sub("", row).strip():
                break
            blanks += 1
        # A wholly blank emit extends the run above it; anything with content restarts the count.
        self.trailing_blanks = self.trailing_blanks + blanks if blanks == len(rows) else blanks
        # Cleared here rather than in `emit`, so output that never goes through it -- an answer, a
        # rule, a direct write -- also ends a run of one-line calls. `emit` sets it again after.
        self.emitted_single_line = False

    def separate(self, rows: int = 1) -> None:
        """Ensure `rows` blank rows part what comes next from what is already on screen.

        The one way a gap is opened. Callers state the separation they want rather than counting
        newlines, so a block cannot arrive glued to the one above it, and two callers each asking
        for room cannot stack two blank rows between the same pair of blocks.

        Uncolored output is left alone: it is a transcript for another program to read, and its
        spacing is the caller's exact text.
        """
        if not self.color:
            return
        for _ in range(max(0, rows - self.trailing_blanks)):
            self.emit()

    def write_direct(self, callback: Callable[[], None]) -> None:
        """Run one write with no application to print above, draining anything queued first.

        The fallback for a write the ordered queue can no longer accept -- the runtime is unwinding
        and the application may already have stopped. Ordering still holds: whatever was batched
        goes out before this does."""

        self.drain_scrollback()
        callback()

    def drain_scrollback(self) -> None:
        """Synchronously print anything still queued in the batching window.

        Called before any direct print (so a later line never lands ahead of queued ones) and
        at application shutdown (so a turn's last lines are not lost to an un-fired timer).
        """
        with self._scrollback_lock:
            parts = self._scrollback_parts
            self._scrollback_parts = []
            self._scrollback_scheduled = False
            self._scrollback_generation += 1
        if parts:
            self.print_parts(parts)

    def print_parts(self, parts: list[FormattedText | ANSI | WidthDependent]) -> None:
        """Send completed output, retaining width-dependent rules for terminal replay.

        Every printed row passes through here, which is what lets the TUI rebuild the terminal
        after a width change: it records what it is handed, and a row that never reached the
        recorder is a row no rebuild can restore. When nothing has claimed the sink -- headless
        runs, startup before the app exists, the unwinding fallback -- this prints as before.
        """
        sink = self.transcript_sink
        if sink is None:
            depth = Theme.color_depth(get_app_session().output.get_default_color_depth())
            print_formatted_text(*parts, sep="", end="", flush=True, color_depth=depth)
            return
        # Snapshot the batch and the completed labels; replay must never consult live turn state.
        color_depth = get_app_session().output.get_default_color_depth()
        if any(isinstance(part, WidthDependent) for part in parts):
            sink(partial(self.render_to_ansi, list(parts), color_depth=color_depth))
        else:
            sink(RecordedOutput(self.render_to_ansi(parts, color_depth=color_depth), list(parts), color_depth))

    @staticmethod
    def render_to_ansi(parts: list[FormattedText | ANSI | WidthDependent], columns: int | None = None, *, color_depth: ColorDepth | None = None) -> str:
        """Render completed fragments and rules to ANSI at the requested terminal width.

        Rendering through a real `Vt100_Output` rather than reimplementing the styling keeps
        themes, ANSI passthrough and wrapping identical to the direct path; only the destination
        differs.
        """
        buffer = io.StringIO()
        size = Size(rows=24, columns=columns or shutil.get_terminal_size((80, 24)).columns)
        # Match ordinary print_formatted_text and the live viewer. Forcing true color here
        # bypasses the terminal's palette conversion and makes recorded diff bands darker. The
        # output's choice is frozen at capture; the theme's say over it is applied here, so a
        # row redrawn after `/theme` takes the depth the new theme draws in.
        depth = Theme.color_depth(color_depth or get_app_session().output.get_default_color_depth())
        output = Vt100_Output(buffer, lambda: size, default_color_depth=depth)
        fragments = [fragment for part in parts for fragment in (part.fragments(size.columns) if isinstance(part, WidthDependent) else to_formatted_text(part))]
        # The role classes resolve to the colors the fragments already carry inline, so this adds
        # nothing until the theme changes -- then a replay draws the new theme's colors.
        render_fragments_to_output(output, fragments, Theme.transcript_style())
        return buffer.getvalue()

    def _scrollback_print_parts(self, parts: list[FormattedText | ANSI | WidthDependent]) -> None:
        """Write one block, which is usually one part but not always.

        An answer is a rule plus its body: two parts, because the rule has to stay a
        `HorizontalRule` for a replay to redraw it, but one write, because it is one block on
        screen and the scrollback queue orders writes rather than rows. A single part still goes
        through `_scrollback_print`, which is the funnel everything else uses.
        """
        if len(parts) == 1:
            self._scrollback_print(parts[0])
            return
        self._write_scrollback(parts)

    def _scrollback_print(self, fragment: FormattedText | ANSI | WidthDependent) -> None:
        self._write_scrollback([fragment])

    def _write_scrollback(self, parts: list[FormattedText | ANSI | WidthDependent]) -> None:
        """Print above a live application, batching a burst of emits into one suspend.

        Outside a live application this drains any queued batch first and then prints
        immediately, exactly as before, so headless and pre-TUI output is unchanged and
        nothing queued is reordered or lost.

        The batching path depends on the live application being reachable from this thread:
        TuiApp.run never wraps create_app_session, so the app registers on the shared default
        AppSession (prompt_toolkit's `_current_app_session` ContextVar default) and every
        thread's get_app_or_none() sees it. Should a future caller wrap app.run in a private
        create_app_session instead, this degrades gracefully to per-emit direct prints -- the
        pre-batch behavior, correct but with the divider blink back.
        """
        app = get_app_or_none()
        # `_running_in_terminal` has no public accessor; it is the only way to see "a suspend is
        # already in progress" so a nested emit prints inside it instead of rejoining the window
        # (which would delay it past the suspend's wait-then-return contract).
        if app is None or not app.is_running or app._running_in_terminal:
            self.drain_scrollback()
            self.print_parts(parts)
            return
        loop = app.loop
        assert loop is not None  # a running application always has one; the checker cannot see it
        self._queue_scrollback(app, parts)

    def _queue_scrollback(self, app: Any, parts: list[FormattedText | ANSI | WidthDependent]) -> None:
        with self._scrollback_lock:
            self._scrollback_parts.extend(parts)
            if self._scrollback_scheduled:
                return
            self._scrollback_scheduled = True
            generation = self._scrollback_generation
        app.loop.call_soon_threadsafe(self._schedule_scrollback, app, generation)

    def _schedule_scrollback(self, app: Any, generation: int) -> None:
        with self._scrollback_lock:
            if not self._scrollback_scheduled or generation != self._scrollback_generation:
                return
        app.loop.call_later(self.SCROLLBACK_BATCH_WINDOW, self._flush_scrollback, generation)

    def _flush_scrollback(self, generation: int | None = None) -> None:
        with self._scrollback_lock:
            if generation is not None and generation != self._scrollback_generation:
                return
            self._scrollback_scheduled = False
            parts = self._scrollback_parts
            self._scrollback_parts = []
        if parts:
            self.print_parts(parts)

    @contextlib.contextmanager
    def batched(self):
        """Collect every emit into one scrollback flush instead of one suspend per emit."""
        if not self.color or self._batch_parts is not None:
            yield
            return
        self._batch_parts = []
        try:
            yield
        finally:
            parts = self._batch_parts
            self._batch_parts = None
            if parts:
                self._flush_batch(parts)

    def _flush_batch(self, parts: list[FormattedText | ANSI | WidthDependent]) -> None:
        """Flush a collected batch through the scrollback path, keeping the batch a single print.

        Inside a live application the parts join the same queue as ordinary emits -- landing after
        anything already queued and flushing as one print; outside one they print directly, in
        order, as one call."""
        app = get_app_or_none()
        if app is None or not app.is_running or app._running_in_terminal:
            self.drain_scrollback()
            self.print_parts(parts)
            return
        loop = app.loop
        assert loop is not None  # a running application always has one; the checker cannot see it
        self._queue_scrollback(app, parts)

    def emit(self, text: str | LogBlock = "", indent: int = 0) -> None:
        """Print one line or log block. `indent` moves plain text into a column; a LogBlock is
        left alone, since it already carries its own margins and rails.

        The margin is applied after styling, not before: `segments` dispatches on what the text
        starts with (`• `, `Error:`, `+ `), so prefixing spaces first would strip every line of
        its color."""
        if isinstance(text, LogBlock):
            indent = 0
        if not self.color:
            self.output_fn(self.indent_message(str(text), "", indent) if indent else str(text))
            return
        segments = self.log_segments(text) if isinstance(text, LogBlock) else self.segments(text)
        if indent:
            segments = self.indent_segments(segments, LogBlock.margin(indent))
        # Counted in rendered rows, not in blocks or calls: what decides whether two rules sit too
        # close is how far apart they are on screen, and one Bash call with its output goes further
        # than four Reads. A block that wrapped counts the rows it actually took.
        self.track_layout("".join(fragment for _, fragment in segments))
        self.emitted_single_line = self.single_line_block(text)
        # A log block sizes its diff gutter and wrapping from the pane, so it is recorded as itself
        # and laid out again on replay. Plain text needs no such treatment: `segments` never wraps,
        # so the terminal re-flows it for free.
        # Snapshot nested mutable items now, before batching or later replay can observe edits.
        part: FormattedText | WidthDependent = LogBlockCell(self, deepcopy(text)) if isinstance(text, LogBlock) else FormattedText(segments)
        if self._batch_parts is not None:
            self._batch_parts.append(part)
            return
        self._scrollback_print(part)

    def emit_block(self, block: WidthDependent) -> None:
        """Print a block that lays itself out, parted from what is above it. Uncolored output gets
        the same rows as plain text, at the terminal's width."""
        width = shutil.get_terminal_size().columns
        text = "".join(fragment[1] for fragment in block.fragments(width))
        if not self.color:
            self.output_fn(text.rstrip("\n"))
            return
        self.separate()
        self.track_layout(text)
        if self._batch_parts is not None:
            self._batch_parts.append(block)
            return
        self._scrollback_print(block)

    @staticmethod
    def indent_segments(segments: list[tuple[str, str]], margin: str) -> list[tuple[str, str]]:
        """Open every rendered line with `margin`, carrying the style of the fragment it opens.

        A trailing newline ends the last line rather than starting another, so the margin that
        would follow it is dropped; otherwise an empty emit would print a row of spaces."""
        indented: list[tuple[str, str]] = []
        at_line_start = True
        for style, value in segments:
            for index, piece in enumerate(value.split("\n")):
                if index:
                    indented.append((style, "\n"))
                    at_line_start = True
                if at_line_start and piece:
                    indented.append((style, margin))
                    at_line_start = False
                if piece:
                    indented.append((style, piece))
        return indented

    # Rich right-pads every rendered line with spaces up to the console width so backgrounds and
    # padding can fill the row. Uncolored padding gets baked into scrollback and turns into wrap
    # zigzags on a narrower terminal, so we strip it — but padding that carries a background color
    # (syntax-highlighted code blocks, edit previews) must be preserved so the block still reads
    # as a solid band. We track the SGR bg state per token and only strip whitespace rendered with
    # bg off.
    SGR_RE: ClassVar[re.Pattern[str]] = re.compile(r"\x1b\[([0-9;]*)m")
    RECORD_TOKEN_RE: ClassVar[re.Pattern[str]] = re.compile(r"(?:tr|job)\.\d+|\d+(?::\d+)?")
    # OSC / APC / DCS / SOS / PM sequences are terminal control strings that prompt_toolkit's ANSI
    # parser doesn't recognize. When they slip through Rich's output (OSC 8 hyperlinks were the
    # historical culprit, iTerm image escapes / Kitty graphics / shell-integration marks are
    # potential future ones), pt eats the ESC framing but leaks the payload as visible garbage
    # (e.g. `8;id=…;https://…;;` for OSC 8). Strip these up front so pt only ever sees CSI escapes.
    # The trade is that any legitimate uses of these (clickable hyperlinks, inline images) never
    # reach the terminal — but they weren't working through pt anyway; better clean than garbled.
    NON_CSI_ESCAPE_RE: ClassVar[re.Pattern[str]] = re.compile(r"\x1b[\]_PX^][^\x07\x1b]*(?:\x07|\x1b\\)")

    @classmethod
    def strip_unknown_escapes(cls, text: str) -> str:
        return cls.NON_CSI_ESCAPE_RE.sub("", text)

    @classmethod
    def strip_trailing_pad(cls, text: str) -> str:
        return "\n".join(cls._strip_line_pad(line) for line in text.split("\n"))

    @classmethod
    def _strip_line_pad(cls, line: str) -> str:
        tokens: list[tuple[str, str]] = []  # ("sgr"|"text", payload)
        bg_states: list[bool] = []  # bg active while each token renders
        bg, idx = False, 0
        for m in cls.SGR_RE.finditer(line):
            if m.start() > idx:
                tokens.append(("text", line[idx : m.start()]))
                bg_states.append(bg)
            tokens.append(("sgr", m.group(0)))
            bg_states.append(bg)
            params = [int(param) if param else 0 for param in (m.group(1) or "0").split(";")]
            position = 0
            while position < len(params):
                n = params[position]
                # Extended colors carry RGB channels or a palette index. Those numbers are
                # color data, not independent SGR commands (0 and 49 do not reset the band).
                if n in {38, 48, 58} and position + 1 < len(params) and params[position + 1] in {2, 5}:
                    if n == 48:
                        bg = True
                    position += 5 if params[position + 1] == 2 else 3
                    continue
                if n == 0 or n == 49:
                    bg = False
                elif 40 <= n <= 47 or 100 <= n <= 107 or n == 48:
                    bg = True
                position += 1
            idx = m.end()
        if idx < len(line):
            tokens.append(("text", line[idx:]))
            bg_states.append(bg)
        seen_content = False
        for i in range(len(tokens) - 1, -1, -1):
            kind, payload = tokens[i]
            if kind == "sgr" or seen_content:
                continue
            if bg_states[i]:
                if payload.strip():
                    seen_content = True
                continue
            stripped = payload.rstrip()
            if stripped != payload:
                tokens[i] = ("text", stripped)
            if stripped:
                seen_content = True
        return "".join(payload for _, payload in tokens)

    def emit_answer(self, text: str, *, role: str = "", rule: bool = True, indent: int = 0) -> None:
        if not self.color:
            if role == "user":
                text, role = "\n" + self.USER_LOG_PREFIX + text, ""
            elif role == "assistant":
                role = ""
            self.output_fn(self.indent_message(text, role, indent))
            return
        if role == "user":
            # The user's message opens a turn, so it is parted from whatever the last one left
            # behind -- through the printer, which knows whether a gap is already there, rather
            # than by a blank row Rich prints whether one is needed or not.
            self.separate()
        # The rule above an answer is drawn as a HorizontalRule rather than by Rich, so that a
        # projection can redraw it at the width it is replayed into. Rich bakes the width into the
        # captured ANSI, and a 100-column rule replayed into a 60-column pane wraps onto a second
        # row -- the other three rules already avoid this the same way.
        drew_rule = rule and not self.is_error(text)
        block = MessageBlock(self, text, role, indent)
        # Count the rule's row as Rich's own row was counted, so spacing decisions taken from
        # `rows_since_rule` and `trailing_blanks` are unchanged by where the rule is drawn.
        self.track_layout(("─\n" if drew_rule else "") + block.ansi(shutil.get_terminal_size().columns))
        parts: list[FormattedText | ANSI | WidthDependent] = [block]
        if drew_rule:
            parts.insert(0, HorizontalRule(Theme.fg("rule")))
        if self._batch_parts is not None:
            self._batch_parts.extend(parts)
            return
        self._scrollback_print_parts(parts)

    def emit_phase_rule(self) -> None:
        """Close a stretch of the turn with the same quiet full-width rule the turn ends with,
        minus the label: the agent's own words -- or, in a long run of silent calls, their
        absence -- are the label, so the rule carries none. It is drawn below an interim
        narration the way the turn-end rule is drawn below the answer, and again at a batch
        boundary when the agent has been working in silence long enough.

        The caller decides whether it would land too close to the rule above it (rule_due);
        this method only draws what it is told to.
        """
        if not self.color:
            return
        # The rule owns both of its seams: a blank row above parts it from the block it closes, and
        # one below keeps whatever follows off it. Neither is the caller's to draw, and neither
        # doubles up when the block above already ended in a gap.
        self.separate()
        fragments = HorizontalRule(Theme.fg("rule"), blank_after=True)
        if self._batch_parts is not None:
            self._batch_parts.append(fragments)
        else:
            self._scrollback_print(fragments)
        self.track_layout("─\n\n")
        # Distance to the next rule is measured from here, so the rule's own rows do not count.
        self.rows_since_rule = 0

    def rule_due(self, min_rows: int) -> bool:
        """Whether a phase rule would land at least `min_rows` rendered rows below the last one
        drawn, so it is far enough to be worth drawing. Color is part of the answer: without it
        there are no rules to be close to."""
        return self.color and self.rows_since_rule >= min_rows

    def emit_turn_end(self, started_at: float) -> None:
        """Close the turn with a quiet full-width gray rule carrying its total duration.

        The durable counterpart to the animated working divider: the divider counts up while the
        turn runs and is torn down when it ends, so the final elapsed value is frozen here. It
        reuses `elapsed_since` so the rule reads like the divider's last frame (`5s`, `1m05s`)
        instead of the old `0m5s` / `1m5s`. The label is left-biased (a short lead of dashes, then
        the label, then a long trail to the full width) and a blank line lifts the rule off the
        answer above it.
        """
        label = f"done in {Text.elapsed_since(started_at)}"
        if not self.color:
            self.output_fn(label)
            return
        self.separate()
        fragments = HorizontalRule(Theme.fg("rule"), label, Theme.fg("text"))
        if self._batch_parts is not None:
            self._batch_parts.append(fragments)
        else:
            self._scrollback_print(fragments)
        self.track_layout(label + "\n")
        # Distance to the next rule is measured from here, so the rule's own row does not count.
        self.rows_since_rule = 0

    @staticmethod
    def indent_message(text: str, role: str = "", indent: int = 0) -> str:
        body = "\n".join(LogBlock.margin(indent) + line for line in text.splitlines() or [""])
        return f"{LogBlock.margin(indent)}{role}:\n{body}" if role else body

    @classmethod
    def colorize_mcp_status(cls, text: str) -> str:
        return cls.MCP_STATUS_RE.sub(lambda match: cls.MCP_STATUS_ANSI[match.group(1)] + "●\x1b[39m " + match.group(1), text)

    @staticmethod
    def is_error(text: str) -> bool:
        """Whether a message reads as an error, which is drawn without a rule above it."""
        return text.startswith(("Error:", "ConfigError:", "Unknown command:"))

    def render_message(self, console: Console, text: str, role: str, rule: bool, indent: int) -> None:
        # Rich renders completed output only, so it loads here rather than at module scope and the
        # first frame does not pay for it. See `wizolt.ui.markdown`.
        from rich.padding import Padding
        from rich.rule import Rule
        from rich.text import Text as RichText

        from wizolt.ui.markdown import WizoltMarkdown

        error = self.is_error(text)
        styled_text = self.colorize_mcp_status(text) if role != "user" else text
        if rule and not error:
            console.print(Rule(style="wizolt.rule", characters="─"))
        margin = LogBlock.margin(indent)
        if role == "user":
            console.print(Padding(RichText(UiPrinter.USER_LOG_PREFIX + text, style="wizolt.user"), (0, 0, 0, len(margin)), style="wizolt.user"))
        elif role == "assistant":
            content = RichText(styled_text, style="wizolt.error") if error else WizoltMarkdown(styled_text)
            console.print(Padding(content, (0, 0, 0, len(margin))))
        else:
            if role:
                label = RichText(role + ":", style="wizolt.muted")
                console.print(Padding(label, (0, 0, 0, len(margin))))
            content = RichText(styled_text, style="wizolt.error") if error else WizoltMarkdown(styled_text)
            console.print(Padding(content, (0, 0, 0, len(margin))))

    @staticmethod
    def tab_segments(titles: tuple[str, ...], active: int) -> list[tuple[str, str]]:
        parts: list[tuple[str, str]] = []
        for index, title in enumerate(titles):
            parts.append(("class:tab.active" if index == active else "class:tab.inactive", f" {title} "))
            if index < len(titles) - 1:
                parts.append(("class:choice.disabled", " │ "))
        return parts

    def segments(self, text: str) -> list[tuple[str, str]]:
        if text.startswith(MEMORY_PREFIXES):
            return self.memory_segments(text)
        if text.startswith(self.USER_LOG_PREFIX):
            prefix, content = self.USER_LOG_PREFIX, text[len(self.USER_LOG_PREFIX) :]
            return [(self.user_log_style(), prefix + content + "\n")]
        if text.startswith("+ "):
            return [(Theme.fg("muted"), "+ "), (Theme.fg("text"), text[2:] + "\n")]
        if text.startswith("done in "):
            return [(Theme.fg("muted"), text + "\n")]
        if text.startswith("wizolt "):
            return [(Theme.fg("accent"), text + "\n")]
        if text.startswith("Resume "):
            return [(Theme.fg("success"), line + "\n") for line in text.splitlines()]
        if text.startswith(("Error:", "ConfigError:", "Unknown command:")):
            return [(Theme.fg("error"), text + "\n")]
        return [(Theme.fg("text"), line + "\n") for line in text.splitlines() or [""]]

    # A log line's label and its text, as palette roles. The label carries the identity (which tool,
    # which worker, an error) and the text is body copy, so most rows pair a colored label with
    # ordinary text; the rows that are themselves supporting detail go muted on both.
    LOG_ROLES: ClassVar[dict[LogRole, tuple[str, str]]] = {
        LogRole.TOOL: ("tool", "text"),
        LogRole.AUTO: ("info", "text"),
        LogRole.META: ("muted", "muted"),
        LogRole.FIELD: ("accent", "text"),
        LogRole.OUTPUT: ("muted", "muted"),
        LogRole.ERROR: ("error", "text"),
        LogRole.WARNING: ("warning", "text"),
        LogRole.SUCCESS: ("success", "text"),
        LogRole.MUTED: ("muted", "muted"),
        LogRole.DIFF: ("text", "text"),
        LogRole.CODE: ("text", "text"),
    }

    @classmethod
    def log_styles(cls, role: LogRole) -> tuple[str, str]:
        """One log role's (label, text) styles under the active theme."""
        label, text = cls.LOG_ROLES[role]
        return Theme.fg(label) + (" bold" if role in {LogRole.WARNING, LogRole.SUCCESS} else ""), Theme.fg(text)

    def log_segments(self, block: LogBlock, columns: int | None = None) -> list[tuple[str, str]]:
        """Lay a log block out for `columns`, defaulting to the terminal's current width.

        The width reaches the diff gutter and every wrapped row, so passing it explicitly is what
        lets a projection lay the block out again for the pane it lands in rather than replaying
        the rows produced for some earlier one. See `LogBlockCell`.
        """
        segments: list[tuple[str, str]] = []
        width = max(1, (columns if columns is not None else shutil.get_terminal_size((120, 20)).columns) - 1)
        entries = [(line, level, self.margin_segments(level, rails)) for line, level, rails in block.walk_rows()]
        index = 0
        while index < len(entries):
            line, level, margin = entries[index]
            if line.role is LogRole.DIFF:
                end = index + 1
                while end < len(entries) and entries[end][0].role is LogRole.DIFF and entries[end][1] == level:
                    end += 1
                diff_lines = [entry[0] for entry in entries[index:end]]
                diff_text = "\n".join(item.text for item in diff_lines)
                highlighted = self.segment_lines(self.diff_segments(diff_text))
                for item, rendered in zip(diff_lines, highlighted):
                    prefix = [*margin, *self.edge_segments(item.edge)]
                    rendered = self.remove_line_ending(rendered)
                    for row in Text.wrap_styled(prefix, prefix, rendered, width):
                        background = self.diff_background(item.text)
                        if background:
                            used = sum(get_cwidth(fragment[1]) for fragment in row)
                            row.append((background, " " * max(0, width - used)))
                        segments.extend([*row, ("", "\n")])
                index = end
                continue
            if line.role is LogRole.CODE:
                end = index + 1
                while end < len(entries) and entries[end][0].role is LogRole.CODE and entries[end][1] == level:
                    end += 1
                code = [entry[0] for entry in entries[index:end]]
                # Lexed as one block, for the same reason a diff is: a per-line lexer loses every
                # construct that spans lines. The numbers are the excerpt's own 1..N, which is what
                # a ToolScript traceback (`File "<toolscript>", line N`) counts in.
                highlighted = self.code_lines("\n".join(item.text for item in code), code[0].syntax)
                number_width = len(str(len(code)))
                for number, item in enumerate(code, 1):
                    rendered = highlighted[number - 1] if highlighted is not None and number - 1 < len(highlighted) else [(Theme.fg("text"), item.text)]
                    prefix = [*margin, *self.edge_segments(item.edge), (Theme.fg("subtle"), f"{number:>{number_width}}  ")]
                    # A wrapped code row keeps the margin itself (rails included) and blanks only
                    # the edge and gutter, so a long line cannot punch a hole in the rail.
                    continuation = [*margin, ("", " " * sum(get_cwidth(fragment[1]) for fragment in prefix[len(margin) :]))]
                    for row in Text.wrap_styled(prefix, continuation, rendered, width):
                        segments.extend([*row, ("", "\n")])
                index = end
                continue
            label_style, text_style = self.log_styles(line.role)
            prefix = [*margin, *self.edge_segments(line.edge)]
            if line.label:
                prefix.append((label_style, line.label))
            content: list[tuple[str, str]] = []
            if line.text:
                separator = "  " if line.edge is LogEdge.NONE and line.label else " " if line.label else ""
                prefix.append((text_style, separator))
                content.extend(self.syntax_segments(line.text, line.syntax, text_style))
            if line.meta:
                content.append((Theme.fg("error" if line.role is LogRole.ERROR else "muted"), line.meta))
            continuation = [*margin, ("", " " * get_cwidth(line.text_prefix()))]
            for row in Text.wrap_styled(prefix, continuation, content, width):
                segments.extend([*row, ("", "\n")])
            index += 1
        return segments

    @staticmethod
    def margin_segments(level: int, rails: tuple[int, ...]) -> list[tuple[str, str]]:
        """A line's indent as styled segments: rail units in the gray the tree edges use, plain
        spacing everywhere else. Runs of one style are merged, so an indent with no rails in it
        stays the single blank segment it has always been."""
        segments: list[tuple[str, str]] = []
        for rail, text in LogBlock.margin_units(level, rails):
            style = Theme.fg("subtle") if rail else ""
            if segments and segments[-1][0] == style:
                segments[-1] = (style, segments[-1][1] + text)
            else:
                segments.append((style, text))
        return segments

    @staticmethod
    def edge_segments(edge: LogEdge) -> list[tuple[str, str]]:
        return [] if edge is LogEdge.NONE else [(Theme.fg("subtle"), edge.value + " ")]

    @staticmethod
    def remove_line_ending(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
        result = list(segments)
        if result and result[-1][1].endswith("\n"):
            style, text = result[-1]
            result[-1] = (style, text[:-1])
            if not result[-1][1]:
                result.pop()
        return result

    @classmethod
    def syntax_segments(cls, text: str, lexer_name: str, fallback_style: str) -> list[tuple[str, str]]:
        if lexer_name == "tool-args":
            return cls.tool_arg_segments(text, fallback_style)
        if pygments is None or get_lexer_by_name is None or not lexer_name:
            return [(fallback_style, text)]
        try:
            lexer = get_lexer_by_name(lexer_name, stripnl=False, ensurenl=False)
            return [(cls.pygments_style(token_type), value) for token_type, value in lexer.get_tokens(text) if value]
        except Exception:  # noqa: BLE001 - third-party lexers must degrade to plain rendering.
            return [(fallback_style, text)]

    @classmethod
    def tool_arg_segments(cls, text: str, fallback_style: str) -> list[tuple[str, str]]:
        segments = []
        for match in cls.TOOL_ARG_TOKEN.finditer(text):
            token = match.group(0)
            if token.isspace():
                style = fallback_style
            elif token.endswith("="):
                style = Theme.fg("syntax_assign")
            elif token.startswith(('"', "'")):
                style = Theme.fg("syntax_string")
            elif UiPrinter.RECORD_TOKEN_RE.fullmatch(token):
                style = Theme.fg("syntax_number")
            elif token in {";", ","}:
                style = Theme.fg("muted")
            else:
                style = Theme.fg("syntax_ident")
            segments.append((style, token))
        return segments or [(fallback_style, text)]

    def memory_segments(self, text: str) -> list[tuple[str, str]]:
        segments = []
        for line in text.splitlines() or [""]:
            # Deliberately narrower than MEMORY_PREFIXES: these two carry their value on the same
            # line, so the whole line is the headline; `plan:`/`known:` head a list below them and
            # are matched exactly, one case down.
            if line.startswith(("goal:", "check:")):
                segments.append((Theme.fg("accent_secondary"), line))
            elif line in {"summary:", "plan:", "known:"}:
                segments.append((Theme.fg("accent"), line))
            elif line.lstrip().startswith("- [x]"):
                segments.append((Theme.fg("success"), line))
            elif line.lstrip().startswith("- [~]"):
                segments.append((Theme.fg("warning"), line))
            elif line.lstrip().startswith("- [-]"):
                segments.append((Theme.fg("error"), line))
            elif line.lstrip().startswith("+ "):
                segments.append((Theme.fg("success"), line))
            else:
                segments.append((Theme.fg("text"), line))
            segments.append(("", "\n"))
        return segments

    @staticmethod
    def token_definition(style: Any, token_type: Any) -> dict | None:
        """The style entry for a token, falling back to its ancestors, or None if nothing matches.

        `style_for_token` raises KeyError when a style defines nothing anywhere in a token's
        subtree, and a style only has to cover the tokens its own authors thought about: the YAML
        lexer emits `Token.Literal.Scalar.Plain` and `Token.Punctuation.Indicator`, which
        github-dark never mentions, so highlighting any `.yaml` used to abort mid-render and take
        the whole Edit down with it.

        Walking up to the parent is what Pygments' own token hierarchy is for: a plain scalar
        renders like the Literal it is, rather than dropping the whole file to unstyled text.
        """
        while token_type is not None:
            try:
                return style.style_for_token(token_type)
            except KeyError:
                token_type = getattr(token_type, "parent", None)
        return None

    @classmethod
    def pygments_style(cls, token_type: Any) -> str:
        style = Theme.pygments_style()
        if style is None or Token is None:
            return "fg:default"
        if token_type in Token.Text.Whitespace:
            return "fg:default"
        if token_type in Token.Name.Builtin:
            return Theme.fg("syntax_builtin")
        definition = cls.token_definition(style, token_type)
        if definition is None:
            return "fg:default"
        color = definition.get("color")
        default_hex = Theme.color("syntax_default").lstrip("#")
        parts = ["fg:default" if not color or color.lower() == default_hex else f"fg:#{color}"]
        parts.extend(attribute for attribute in ("bold", "italic", "underline") if definition.get(attribute))
        return " ".join(parts)

    def _diff_tokenize_lines(self, code_text: str, path: str | None) -> list[list[tuple[str, str]]] | None:
        """Tokenize a whole block of code and return highlighted segments per line.

        Pygments lexers are designed to work on whole files; splitting by diff
        lines and lexing each one independently breaks multiline strings and
        indentation-sensitive languages.  We therefore lex the assembled code
        block once and split the resulting token stream back into lines.
        """
        if pygments is None or get_lexer_for_filename is None or not path:
            return None
        try:
            lexer = get_lexer_for_filename(path, stripnl=False)
        except Exception:  # noqa: BLE001 - third-party lexer lookup must degrade to plain rendering.
            return None
        return self._tokenized_lines(lexer, code_text)

    @classmethod
    def code_lines(cls, code_text: str, lexer_name: str) -> list[list[tuple[str, str]]] | None:
        """Highlighted segments per source line for a standalone block of code, by lexer name.

        The same whole-block lexing _diff_tokenize_lines relies on, for code that arrives without a
        filename to infer a lexer from: a ToolScript body, anything a viewer shows. Returns None
        when pygments is missing or the name is unknown, leaving the caller to render plain text."""
        if pygments is None or get_lexer_by_name is None or not lexer_name:
            return None
        try:
            lexer = get_lexer_by_name(lexer_name, stripnl=False)
        except Exception:  # noqa: BLE001 - third-party lexer lookup must degrade to plain rendering.
            return None
        return cls._tokenized_lines(lexer, code_text)

    @classmethod
    def _tokenized_lines(cls, lexer: Any, code_text: str) -> list[list[tuple[str, str]]] | None:
        # The whole walk is guarded, not just the call that starts it: get_tokens is a generator,
        # so a lexer that fails does it while this loop pulls from it, not here. Highlighting is
        # decoration -- a lexer that cannot cope must cost the color, never the edit it was
        # previewing.
        try:
            lines: list[list[tuple[str, str]]] = [[]]
            for token_type, value in lexer.get_tokens(code_text):
                style = cls.pygments_style(token_type)
                parts = value.split("\n")
                for i, part in enumerate(parts):
                    if i > 0:
                        lines.append([])
                    if part:
                        lines[-1].append((style, part))
            return lines
        except Exception:  # noqa: BLE001 - third-party lexer execution must degrade to plain rendering.
            return None

    # A unified-diff file header, told apart from a removed or added line whose own content starts
    # with "---" or "+++" by the space that follows the marker in a header and nowhere else (both
    # git and difflib write `--- <path>`, and `--- ` even when the path is empty). Matching the
    # marker alone drew a removed markdown rule (`---`, so the diff line is `----`) as a dim
    # header: no red band, and the row it then never counted shifted every old line number
    # under it in that hunk.
    DIFF_HEADER_PREFIXES: ClassVar[tuple[str, ...]] = ("--- ", "+++ ")
    # Width of the line-number gutter `diff_segments` writes (`NNNN NNNN │ `). A caller wrapping a
    # diff row indents its continuation by this much, so the wrapped text stays in the body column
    # instead of falling back to the left margin.
    DIFF_GUTTER_WIDTH: ClassVar[int] = 12
    # Word-level emphasis compares a removed line with the added line that replaces it. Lines this
    # long are skipped (a minified blob gains nothing from it), and a pair sharing less than this
    # much is a rewrite, where marking nearly every word would only restate the whole-line band.
    DIFF_EMPHASIS_MAX_CHARS: ClassVar[int] = 400
    DIFF_EMPHASIS_MIN_RATIO: ClassVar[float] = 0.5
    DIFF_WORD_RE: ClassVar[re.Pattern] = re.compile(r"\w+|\s+|[^\w\s]")

    @classmethod
    def changed_spans(cls, old: str, new: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """The character ranges of `old` and `new` that differ, compared word by word.

        Empty on both sides when the pair is too long or too different to be a modified line."""
        if max(len(old), len(new)) > cls.DIFF_EMPHASIS_MAX_CHARS:
            return [], []
        old_words = cls.DIFF_WORD_RE.findall(old)
        new_words = cls.DIFF_WORD_RE.findall(new)
        matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
        if matcher.ratio() < cls.DIFF_EMPHASIS_MIN_RATIO:
            return [], []
        old_offsets = [0, *accumulate(len(word) for word in old_words)]
        new_offsets = [0, *accumulate(len(word) for word in new_words)]
        old_spans: list[tuple[int, int]] = []
        new_spans: list[tuple[int, int]] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if i1 < i2:
                old_spans.append((old_offsets[i1], old_offsets[i2]))
            if j1 < j2:
                new_spans.append((new_offsets[j1], new_offsets[j2]))
        return old_spans, new_spans

    @staticmethod
    def split_at_spans(content: list[tuple[str, str]], spans: list[tuple[int, int]]) -> list[tuple[str, str, bool]]:
        """Split styled pieces at the span edges, flagging each piece that falls inside a span."""
        result: list[tuple[str, str, bool]] = []
        offset = 0
        for style, piece in content:
            cuts = sorted({0, len(piece), *(min(max(edge - offset, 0), len(piece)) for span in spans for edge in span)})
            for start, end in pairwise(cuts):
                inside = any(low <= offset + start and offset + end <= high for low, high in spans)
                result.append((style, piece[start:end], inside))
            offset += len(piece)
        return result

    def diff_background(self, line: str) -> str:
        """The band a diff line is painted with: the added or removed color, or none.

        One rule, so the renderer that draws a band and any caller padding it out to the pane edge
        cannot disagree about which lines have one. A file header is told apart from a changed line
        whose own content opens with the marker by the space after it, which is how `diff_segments`
        reads them too -- matching the marker alone would band a `----` horizontal rule."""
        if line.startswith(self.DIFF_HEADER_PREFIXES):
            return ""
        if line.startswith("+"):
            return Theme.diff_style("diff.added.bg")
        if line.startswith("-"):
            return Theme.diff_style("diff.removed.bg")
        return ""

    def diff_segments(self, text: str) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        old_line: int | None = None
        new_line: int | None = None
        lines = text.splitlines()
        # Bands are padded only after wrapping in log_segments; padding a logical line here would
        # either be discarded at a word boundary or create an extra visual row.
        # Determine the target file path from the diff header.  The `+++` line
        # names the resulting file; for created files `---` is /dev/null.
        file_path: str | None = None
        for header in lines:
            if header.startswith("+++"):
                candidate = header[4:].strip()
                if candidate != "/dev/null":
                    file_path = candidate
                break

        # Collect lines that belong to the new file version: context lines and
        # added lines.  These are lexed together so the highlighted diff is
        # syntactically coherent. Removed lines stay neutral on a red background so
        # the "before" state does not interfere with lexing the "after" state.
        new_code_lines: list[str] = []
        new_code_indices: list[int] = []
        for i, line in enumerate(lines):
            # Skip the unified-diff file headers / hunk markers; feed only actual code to the lexer.
            if line.startswith((*self.DIFF_HEADER_PREFIXES, "@@ ")):
                continue
            if line.startswith(("+", " ")):
                new_code_lines.append(line[1:])
                new_code_indices.append(i)

        highlighted: list[list[tuple[str, str]]] | None = None
        if new_code_lines:
            highlighted = self._diff_tokenize_lines("\n".join(new_code_lines), file_path)

        hl_by_index: dict[int, list[tuple[str, str]]] = {}
        if highlighted is not None:
            for hl_index, line_index in enumerate(new_code_indices):
                if hl_index < len(highlighted):
                    hl_by_index[line_index] = highlighted[hl_index]

        # A run of removed lines followed by an added run of the same length is a modification: its
        # lines are paired in order and each pair marks the words it changed. Runs of different
        # lengths are left alone, as diff-highlight does -- pairing them by position would match a
        # line against whatever happens to sit at the same offset.
        def changed(line: str, sign: str) -> bool:
            return line.startswith(sign) and not line.startswith(self.DIFF_HEADER_PREFIXES)

        spans_by_index: dict[int, list[tuple[int, int]]] = {}
        run = 0
        while run < len(lines):
            if not changed(lines[run], "-"):
                run += 1
                continue
            removed_end = run
            while removed_end < len(lines) and changed(lines[removed_end], "-"):
                removed_end += 1
            added_end = removed_end
            while added_end < len(lines) and changed(lines[added_end], "+"):
                added_end += 1
            if removed_end - run == added_end - removed_end:
                for old_index, new_index in zip(range(run, removed_end), range(removed_end, added_end), strict=True):
                    old_spans, new_spans = self.changed_spans(lines[old_index][1:], lines[new_index][1:])
                    spans_by_index[old_index] = old_spans
                    spans_by_index[new_index] = new_spans
            run = added_end

        def hunk_start(part: str, prefix: str) -> int | None:
            if not part.startswith(prefix):
                return None
            try:
                return int(part[1:].split(",", 1)[0])
            except ValueError:
                return None

        # The bands are the diff style's; the chrome around them -- the gutter, the file and hunk
        # heads, the signs -- takes the diff family's own foregrounds (see `DIFF_DARK`), so a
        # theme file recolors the whole diff through one `[diff]` table instead of half of it.
        def number(old: int | None, new: int | None, background: str = "") -> None:
            old_text = "" if old is None else str(old)
            new_text = "" if new is None else str(new)
            # The same box-drawing stroke as the tree rail beside it, so the two verticals match.
            segments.append(((Theme.diff_style("diff.gutter") + " " + background).strip(), f"{old_text:>4} {new_text:>4} │ "))

        def append_hl(
            prefix: str,
            prefix_style: str,
            content_hl: list[tuple[str, str]],
            suffix: str,
            background: str = "",
            spans: list[tuple[int, int]] | None = None,
            emphasis: str = "",
        ) -> None:
            def styled(style: str, band: str = background) -> str:
                return (style + " " + band).strip()

            segments.append((styled(prefix_style), prefix))
            for style, piece, inside in self.split_at_spans(content_hl, spans or []):
                segments.append((styled(style, emphasis if inside else background), piece))
            segments.append(("", suffix))

        for index, line in enumerate(lines):
            suffix = "\n" if index < len(lines) - 1 else ""
            if line.startswith("@@"):
                parts = line.split()
                if len(parts) >= 3:
                    old_line = hunk_start(parts[1], "-")
                    new_line = hunk_start(parts[2], "+")
                # A file header or hunk head has no line to place, so it steps out of the gutter:
                # the rail is for the lines the numbers sit beside.
                segments.append((Theme.diff_style("diff.hunk"), line + suffix))
            elif line.startswith(self.DIFF_HEADER_PREFIXES):
                segments.append((Theme.diff_style("diff.header"), line + suffix))
            elif line.startswith("+"):
                background = self.diff_background(line)
                number(None, new_line, background)
                content_hl = hl_by_index.get(index) or [(Theme.diff_style("diff.added.fg"), line[1:])]
                append_hl(
                    "+", Theme.diff_style("diff.added.sign"), content_hl, suffix, background, spans_by_index.get(index), Theme.diff_style("diff.added.emph")
                )
                new_line = None if new_line is None else new_line + 1
            elif line.startswith("-"):
                background = self.diff_background(line)
                number(old_line, None, background)
                content_hl = [(Theme.diff_style("diff.removed.fg"), line[1:])]
                append_hl(
                    "-", Theme.diff_style("diff.removed.sign"), content_hl, suffix, background, spans_by_index.get(index), Theme.diff_style("diff.removed.emph")
                )
                old_line = None if old_line is None else old_line + 1
            elif line.startswith(" "):
                number(old_line, new_line)
                content_hl = hl_by_index.get(index) or [("fg:default", line[1:])]
                append_hl(" ", "fg:default", content_hl, suffix)
                old_line = None if old_line is None else old_line + 1
                new_line = None if new_line is None else new_line + 1
            else:
                number(None, None)
                segments.append(("fg:default", line + suffix))
        return segments

    @staticmethod
    def segment_lines(segments: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = [[]]
        for style, text in segments:
            parts = text.split("\n")
            for index, part in enumerate(parts):
                if index > 0:
                    lines[-1].append((style, "\n"))
                    lines.append([])
                if part:
                    lines[-1].append((style, part))
        if lines and not lines[-1]:
            lines.pop()
        return lines


class ActivityPulse:
    """One green liveness signal shared by model activity and simulated divider previews.

    Deliberately independent of theme accents: this marks an in-flight request, not the rule.
    Callers decide whether activity exists; the renderer only maps a clock to its breath.
    """

    STYLES: ClassVar[tuple[str, ...]] = (
        "fg:#0a3d0a",
        "fg:#146114",
        "fg:#1f8a1f",
        "fg:#2dbf2d bold",
        "fg:#43e043 bold",
        "fg:#7bff7b bold",
    )
    PERIOD: ClassVar[float] = 1.6

    @classmethod
    def fragments(cls, now: float) -> StyleAndTextTuples:
        phase = (now % cls.PERIOD) / cls.PERIOD
        intensity = 1.0 - abs(2.0 * phase - 1.0)
        index = min(len(cls.STYLES) - 1, int(intensity * len(cls.STYLES)))
        return [(cls.STYLES[index], "● ")]


class LiveSpark:
    """The mark that says a live region is still alive, for regions with nothing else moving.

    A streaming response and a running command both spend long stretches with only a clock
    changing, and a still block does not say it is still running. Both cap their rail with this
    instead of another `│`, and share one definition so the two regions never drift apart.

    Deliberately wordless: whatever the region is doing is already named on the divider under it,
    and saying it twice on one screen is worse than not saying it here at all.
    """

    # Exactly the width of a rail, so it sits in the rail's column and the rows keep their own.
    # A text glyph rather than an emoji: terminals draw emoji in their own colors, so a breath put
    # on one lands on whatever is beside it instead, and emoji are two cells before the space.
    GLYPH: ClassVar[str] = "✦ "
    # Two weights of the star, one per breath, swapped at the crest so every star is seen at full
    # brightness: the opening breath is the fine four-point star, the next breath the heavy
    # six-point one, then back. GLYPH is the phase-zero entry.
    GLYPHS: ClassVar[tuple[str, ...]] = ("✦ ", "✶ ")
    # Twice the divider pulse's period: that dot marks a request in flight and should read as a
    # heartbeat, while this one sits over a wall of text and would nag at that rate.
    PERIOD: ClassVar[float] = 3.2
    STEPS: ClassVar[int] = 12
    # The divider's own glow, so the live rule and the live region it caps read as one thing.
    ROLE: ClassVar[str] = "divider_glow"
    # How far the breath reaches past that color, as a fraction of the way to black at the trough
    # and to white at the crest. Wide on purpose: a shallow fade reads as the terminal mis-drawing
    # a cell rather than as a breath, and the crest has to clear the gray rows beside it. The
    # crest stays shy of pure white so a light terminal does not lose the mark against its own
    # background, but is close enough to read as white on a dark one.
    # The star is thin in its own shape and the terminal has no font size, so it is bold for the
    # whole ramp: that is the only way the mark reads heavier than the rows beside it. The breath
    # survives as the color ramp alone.
    FLOOR: ClassVar[float] = 0.78
    CEILING: ClassVar[float] = 0.92

    @classmethod
    def ramp(cls) -> list[str]:
        """The spark's shades, darkest to brightest, around the divider's accent.

        Derived from the palette rather than hard-coded like the divider's pulse: that dot is green
        in both themes and answers to nothing, while this spark caps the divider's own rule and has
        to keep sharing its color when a theme changes it.
        """
        hue = Theme.rgb(Theme.color(cls.ROLE))
        low = Theme.rgb(Theme.mix(hue, (0, 0, 0), cls.FLOOR))
        high = Theme.rgb(Theme.mix(hue, (255, 255, 255), cls.CEILING))
        span = max(1, cls.STEPS - 1)
        return ["fg:" + Theme.mix(low, high, step / span) + " bold" for step in range(cls.STEPS)]

    @classmethod
    def style(cls, started_at: float = 0.0) -> str:
        """Where on the ramp the spark sits now: a triangular breath measured from `started_at`,
        opening at the crest and falling from there.

        Both halves of that matter. Timed off the wall clock instead, a region catches the cycle
        wherever it happens to be, so one appearing near the trough opens near-black and stays
        unreadable for over a second — precisely the moment it exists to announce. And starting
        bright is what makes the arrival the loudest frame; the breath afterwards is what says it
        is still going.

        A falsy `started_at` falls back to the wall clock, which is the old arbitrary phase but
        never a crash: a caller with no anchor still gets a breathing spark.
        """
        elapsed = (time.monotonic() - started_at) if started_at else time.monotonic()
        phase = (elapsed % cls.PERIOD) / cls.PERIOD
        intensity = abs(2.0 * phase - 1.0)  # 1 at the start, 0 at the half-period, 1 again
        ramp = cls.ramp()
        return ramp[min(len(ramp) - 1, int(intensity * len(ramp)))]

    @classmethod
    def glyph(cls, started_at: float = 0.0) -> str:
        """The spark's mark at this phase: one star per breath, the fine four-point star on the
        opening breath and the heavy six-point star on the next, so each is seen at full
        brightness; `GLYPH` is the opening entry. Shares the phase clock with `style`."""
        elapsed = (time.monotonic() - started_at) if started_at else time.monotonic()
        breath = int(elapsed // cls.PERIOD)
        return cls.GLYPHS[breath % 2]


class BashLivePreview:
    HEIGHT: ClassVar[int] = 5
    MAX_CHARS: ClassVar[int] = 8000
    # Heartbeat tick so the elapsed timer advances even while a command produces no output
    # (e.g. quiet long-runners or `... | tail` that buffers until EOF), so the terminal never
    # looks frozen during a blocking command.
    TICK: ClassVar[float] = 0.3

    def __init__(self):
        self.output = create_output(sys.stderr)
        self.active = False
        self.rendered_lines = 0
        self.rendered_rows: list[list[tuple[str, str]]] = []
        self.text = ""
        self.started_at = 0.0
        # Monotonic absolute end of the current wait budget, for the `· Ns left` countdown; None
        # for Bash, which has no budget to count down.
        self.deadline: float | None = None
        self.lock = threading.Lock()
        self.timer: threading.Thread | None = None

    def start(self) -> None:
        if not sys.stderr.isatty():
            return
        with self.lock:
            self.active, self.rendered_lines, self.rendered_rows, self.text = True, 0, [], ""
            self.started_at = time.monotonic()
            self.deadline = None
            self.render()
        self.timer = threading.Thread(target=self.tick, daemon=True)
        self.timer.start()

    def tick(self) -> None:
        while True:
            time.sleep(self.TICK)
            with self.lock:
                if not self.active:
                    return
                self.render()

    def update(self, text: str) -> None:
        with self.lock:
            if not self.active:
                return
            self.text = (self.text + text)[-self.MAX_CHARS :]
            self.render()

    def finish(self) -> None:
        with self.lock:
            if not self.active:
                return
            self.active = False
        timer = self.timer
        if timer is not None:
            timer.join()
        with self.lock:
            self.rendered_lines, self.rendered_rows, self.text = 0, [], ""
            self.deadline = None

    def render(self) -> None:
        if not self.active:
            return
        rows = self.frame_rows()
        if rows == self.rendered_rows:
            return
        previous = self.rendered_lines
        if self.rendered_lines:
            self.output.write_raw(f"\x1b[{self.rendered_lines}A")
        for row in rows:
            self.output.write_raw("\r")
            self.output.erase_end_of_line()
            print_formatted_text(FormattedText(row), output=self.output, end="", flush=True)
            self.output.write_raw("\n")
        for _ in range(max(0, previous - len(rows))):
            self.output.write_raw("\r")
            self.output.erase_end_of_line()
            self.output.write_raw("\n")
        if previous > len(rows):
            self.output.write_raw(f"\x1b[{previous - len(rows)}A")
        self.output.flush()
        self.rendered_lines = len(rows)
        self.rendered_rows = rows

    def frame_rows(self) -> list[list[tuple[str, str]]]:
        """The frame as styled rows: a breathing spark capping the rail, then the output under it.

        The status row used to carry a BRANCH edge under a `hierarchy(None, ...)` — a `├` with no
        root above it at all, claiming a line joining from a place there was never anything. The
        spark takes that cell instead and is the same width, so the rows keep their column. It is
        also the only thing that moves: a command that goes quiet for minutes leaves nothing else
        on screen changing but the clock, which is what the tick loop was already there for.
        """
        width = max(20, shutil.get_terminal_size((120, 20)).columns)
        body = [line.expandtabs(4) for line in self.text.replace("\r", "\n").splitlines()[-self.HEIGHT :]]
        label = Text.elapsed_since(self.started_at, precise=True)
        remaining = f" · {math.ceil(max(0.0, self.deadline - time.monotonic()))}s left" if self.deadline is not None else ""
        # `limit` leaves a column of slack so a full-width line cannot auto-wrap and desync the
        # cursor-up math in render().
        rail = LogBlock.prefix(2, LogEdge.CONTINUE)
        limit = max(1, width - get_cwidth(rail) - 1)
        # Always emit a status row so the frame is visible even before any output arrives.
        status = f"output · {label}{remaining}" if body else f"running… {label}{remaining}"
        rows = [[(LiveSpark.style(self.started_at), LogBlock.margin(2) + LiveSpark.glyph(self.started_at)), (Theme.fg("muted"), status)]]
        if body:
            # A blank row keeps the spark off the rail: the star caps the region, it does not sit on it.
            rows.append([("", "")])
            rows.extend([(Theme.fg("muted"), rail + Text.clip_width(line, limit))] for line in body)
        return rows


class StatusBar:
    """A quiet, colored summary of the session, owning none of the state it displays.

    Every value displayed is read from session state the engine already maintains. It is a view: it
    never blocks a turn, and must never become the reason a piece of state exists.

    The TUI reads the fragments whenever its ordinary events redraw the screen. The simple frontend
    writes the same static row to stderr once at turn start and erases it on stop, keeping it out of
    piped transcripts without a timer or repaint thread.
    """

    RETRY_NOTICE_DURATION: ClassVar[float] = 2.0
    ROLE_KEYS: ClassVar[tuple[str, ...]] = ("provider", "reason", "mcp", "context", "yolo", "agent")
    SPINNER_FRAMES: ClassVar[str] = "⠋⠙⠹⠸⠼⠴⠦⠧"

    def __init__(self, session: Session):
        self.session = session
        self.layout = BarLayout()
        self.started_at = 0.0
        self.running = False
        self.rendered = False
        self.output = create_output(sys.stderr)
        self.seen_retry_count = session.state.model_retry_count
        self.retry_notice_until = 0.0

    def start(self, *, reset: bool = True) -> None:
        if self.running or not sys.stderr.isatty():
            return
        self.begin(reset=reset)
        self.output.write_raw("\r")
        self.output.erase_end_of_line()
        print_formatted_text(FormattedText(self.fragments()), output=self.output, end="", flush=True)
        self.rendered = True
        self.running = True

    def begin(self, *, reset: bool = True) -> None:
        if reset or not self.started_at:
            self.started_at = time.monotonic()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.clear()

    def is_running(self) -> bool:
        return self.running

    def clear(self) -> None:
        if self.rendered:
            self.output.write_raw("\r")
            self.output.erase_end_of_line()
            self.output.flush()
            self.rendered = False

    def retry_notice_active(self) -> bool:
        now = time.monotonic()
        count = self.session.state.model_retry_count
        if count != self.seen_retry_count:
            self.seen_retry_count = count
            self.retry_notice_until = now + self.RETRY_NOTICE_DURATION
        return self.retry_notice_until > now

    def output_rate(self) -> str:
        """The model's live output speed while a response streams, as `↓ 48 tok/s`, or "" when
        nothing is streaming.

        Estimated from streamed characters at four per token, because token deltas are not on the
        wire: providers report usage once, when the request is over, which is exactly too late for
        the line the reader is watching. The `↓` marks the incoming stream, not a measured token
        rate. Suppressed for the first second, where a couple of chunks over a near-zero elapsed
        reads as a wild number.

        Read off the in-flight worker when there is one, like every other value on this row.
        """
        rate = self.session.state.estimated_output_rate(time.monotonic())
        return "" if rate is None else f"↓ {round(rate)} tok/s"

    def model_attempt_status(self) -> str:
        attempt = self.session.state.current_model_attempt
        return f"attempt {attempt}/{MODEL_REQUEST_RETRIES + 1}" if attempt > 1 else ""

    def retry_status(self) -> str:
        # The two-second notice window covers the brief aftermath after a wait ends. While the wait
        # itself is in progress (its monotonic deadline is still in the future) the full text
        # (attempt, reason, countdown) must keep showing for its whole duration, which can far
        # outlast that window.
        waiting = self.session.state.model_retry_until > time.monotonic()
        if not waiting and not self.retry_notice_active():
            return ""
        attempt = self.session.state.current_model_attempt
        text = f"retrying {attempt}/{MODEL_REQUEST_RETRIES + 1}" if attempt > 1 else "retrying"
        state = self.session.state
        reason = state.model_retry_reason
        if reason:
            text += " · " + reason
        # The model publishes the wait deadline as a fact; the renderer formats the countdown.
        remaining = max(0, math.ceil(state.model_retry_until - time.monotonic()))
        if remaining:
            text += f" · {remaining}s"
        return text

    def values(self) -> dict[str, Value]:
        """Singular agent/usage fields are local; plural agents fields describe the shared group.

        Counts are derived at render time and never cached on the selected Session. Switching
        projection therefore preserves the same group counts while all usage stays agent-local.
        """
        source = self.session
        provider = source.config.provider
        usage = source.usage
        counts = source.subagents.counts if source.subagents else None
        # A displayed session outside the group's live entries is an archived, read-only view.
        entry = source.subagents.entries.get(source.uid) if source.subagents else None
        return {
            "agent.name": source.agent_name,
            "agent.id": source.uid,
            "agent.state": entry.status if entry else "archived" if source.subagents else source.state.last_turn_status,
            "agents.count": counts.total if counts else 1,
            "agents.running": counts.running if counts else int(self.running and not source.state.awaiting_input),
            "agents.waiting": counts.waiting if counts else int(source.state.awaiting_input),
            "provider": source.config.active_provider,
            "model": provider.model.rsplit("/", 1)[-1] or "(no model)",
            "reasoning": provider.reasoning,
            "yolo": self.session.settings.yolo,
            "context.percent": usage.context_percent(source.state.context_percent),
            "cache.percent": usage.last_cached_prompt_tokens * 100 // usage.last_prompt_tokens if usage.last_prompt_tokens else 0,
            "mcp.label": self.mcp_label(),
            "mcp.count": sum(self.session.mcp.connected(item.name) for item in self.session.mcp.parse_configs()) if self.session.mcp else 0,
            "skills.count": len(self.session.skills.skills) if self.session.skills else 0,
            "running": self.running,
            "elapsed": max(0.0, time.monotonic() - self.started_at) if self.started_at else 0.0,
            "activity": "working" if self.running else "",
            "rate": self.output_rate(),
            "spinner": "",
            "label": "",
            "queue.total": 0,
            "queue.followup": 0,
            "queue.next_turn": 0,
            "reset_pending": self.session.context_reset_requested,
            **(source.plugins.fields() if source.plugins is not None else {}),
        }

    def fragments(self) -> StyleAndTextTuples:
        columns = shutil.get_terminal_size((120, 20)).columns
        return list(self.layout.render("statusbar", self.values(), max(1, columns - 1), Theme.bar_styles))

    def mcp_label(self) -> str:
        """The MCP group's text: `mcp N`, with a spinner frame in front of the count while
        discovery is still in flight.

        The count rises server by server as each one finishes, and the idle screen already redraws
        at the 0.2s idle refresh, so those steps show on their own. The spinner covers the stretch
        before the first server lands, where a bare `mcp 0` would read as "nothing connected"
        when the truth is "not yet known". Frames advance at the same five per second as that
        refresh, so every redraw shows the next frame; a plain count returns once discovery
        settles, including the honest `mcp 0` of a session where nothing connected.
        """

        mcp = self.session.mcp
        count = sum(mcp.connected(item.name) for item in mcp.parse_configs()) if mcp is not None else 0
        if mcp is not None and mcp.discovery_status == "discovering":
            frame = self.SPINNER_FRAMES[int(time.monotonic() * 5) % len(self.SPINNER_FRAMES)]
            return f"mcp {frame}{count}"
        return f"mcp {count}"

    @staticmethod
    def clip_fragments(fragments: StyleAndTextTuples, width: int) -> StyleAndTextTuples:
        """Clip styled fragments to a display width while keeping each segment's style, mirroring
        Text.clip_width's trailing ellipsis. Lets the idle status bar keep its role colors when the
        line is wider than the terminal instead of collapsing to a single tone."""
        width = max(0, width)
        if width == 0:
            return [("", "")]
        if sum(get_cwidth(fragment[1]) for fragment in fragments) <= width:
            return list(fragments)
        ellipsis = "." * min(3, width)
        available = width - get_cwidth(ellipsis)
        clipped: StyleAndTextTuples = []
        used = 0
        for style, text, *_ in fragments:
            for char in text:
                char_width = max(0, get_cwidth(char))
                if used + char_width > available:
                    clipped.append((style, ellipsis))
                    return clipped
                clipped.append((style, char))
                used += char_width
        return clipped or [("", "")]
