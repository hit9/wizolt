"""Named color themes: the built-in vim/neovim schemes and the user's theme files.

The default `dark` and `light` palettes live on `render.Theme` and follow the terminal's own ANSI
colors. A named scheme is opt-in and pins every role to the scheme's hex values instead, so the
status footer, the menu, the selection band and highlighted code agree with a terminal already set
to that scheme. None of them paints the terminal background: the transcript is native scrollback,
which wizolt does not own.

A theme file is `<data_dir>/themes/<name>.toml`: a built-in `base`, an optional `pygments` style,
and a `[colors]` table of role overrides. Mistakes in one are reported and skipped, never raised,
so a typo cannot stop wizolt from starting.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field

from prompt_toolkit.styles import ANSI_COLOR_NAMES


@dataclass(frozen=True)
class Palette:
    """One theme: whether it is drawn for a light or dark background, and every role's color."""

    appearance: str  # "dark" | "light": picks the pinned diff colors
    colors: dict[str, str]  # every role, plus "pygments"
    background: str = ""  # the terminal background a named scheme is drawn for; "" follows the terminal
    # Drawn in true color where the terminal offers it. A published scheme wants its exact colors;
    # `dark` and `light` keep the output's depth, because their fixed colors were tuned as it draws
    # them. A theme file inherits its base's choice.
    true_color: bool = False
    # Diff bands a theme file recolors, as `Theme.diff_style` spells them (`diff.added.bg` ->
    # `bg:#rrggbb`); every other band follows the selected diff style.
    diff: dict[str, str] = field(default_factory=dict)
    highlights: dict[str, str] = field(default_factory=dict)
    # The diff style this theme pairs with, drawn while `ui.diff.style` is `auto`.
    diff_style: str = "classic"


# `[diff]` keys in a theme file and the band each one recolors: the line, and the words it changed.
DIFF_KEYS = {
    "added": "diff.added.bg",
    "added_word": "diff.added.emph",
    "removed": "diff.removed.bg",
    "removed_word": "diff.removed.emph",
}


def bands(added: str, added_word: str, removed: str, removed_word: str) -> dict[str, str]:
    """A diff style's four bands for one appearance: green under added lines and the words they
    changed, red under removed ones."""
    return {"diff.added.bg": added, "diff.added.emph": added_word, "diff.removed.bg": removed, "diff.removed.emph": removed_word}


@dataclass(frozen=True)
class DiffStyle:
    """The red and green a diff is drawn on, for the appearances the style was made for."""

    note: str  # what the picker says about it
    dark: dict[str, str] | None = None
    light: dict[str, str] | None = None


# Solid red and green bands, each set taken as published. Beyond wizolt's own and delta's
# defaults, they come from delta's theme collection
# (https://github.com/dandavison/delta/blob/main/themes.gitconfig), most of them tuned by their
# authors against the same syntax themes wizolt ships.
# fmt: off
DIFF_STYLES: dict[str, DiffStyle] = {
    "classic": DiffStyle(
        "wizolt's bright red and green",
        dark=bands("#003b00", "#1c7a1c", "#520000", "#9c1c1c"),
        light=bands("#d1f0d1", "#8fd88f", "#f5c8c8", "#e88f8f"),
    ),
    # https://github.com/dandavison/delta/blob/main/src/color.rs
    "delta": DiffStyle(
        "delta's defaults: deeper, so code reads more easily",
        dark=bands("#002800", "#006000", "#3f0001", "#901011"),
        light=bands("#d0ffd0", "#a0efa0", "#ffe0e0", "#ffc0c0"),
    ),
    "zebra": DiffStyle(
        "soft bands that stay behind the code",
        dark=bands("#0e2f19", "#174525", "#330f0f", "#4f1917"),
        light=bands("#d6ffd6", "#adffad", "#fbdada", "#f6b6b6"),
    ),
    "gruvmax-fang": DiffStyle("made for gruvbox-dark", dark=bands("#001a00", "#003300", "#330011", "#80002a")),
    "platypus": DiffStyle("made for solarized-dark", dark=bands("#2a5e37", "#1a861a", "#5d001e", "#b80000")),
    "calochortus-lyallii": DiffStyle("made for nord", dark=bands("#004000", "#007800", "#400000", "#780000")),
    "colibri": DiffStyle("made for One Half Dark, one-dark's sibling", dark=bands("#003500", "#007e5e", "#5e0000", "#80002a")),
    "mantis-shrimp": DiffStyle("made for monokai", dark=bands("#004433", "#007800", "#5d001e", "#b80000")),
}
# fmt: on


# `Theme.ramp` interpolates between these two, so they must be hex rather than a terminal color.
HEX_ROLES = ("divider_glow", "divider_rule")
# WCAG contrast floors for a scheme's grey text: secondary text on the background, and the menu's
# descriptions on its raised surface, which is where a comment grey reads worst.
MUTED_CONTRAST = 4.5
MENU_TEXT_CONTRAST = 4.5


def luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    red, green, blue = (value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4 for value in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: str, second: str) -> float:
    """The WCAG contrast ratio of two `#rrggbb` colors, from 1 to 21."""
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def lift(color: str, toward: str, against: str, minimum: float) -> str:
    """`color`, moved toward `toward` in tenths until it reaches `minimum` contrast on `against`.

    Keeps the original wherever readable. If the preferred foreground cannot reach the floor
    (Solarized's menu text), continue toward black or white instead of silently falling short.
    """
    if contrast(toward, against) < minimum:
        toward = max(("#000000", "#ffffff"), key=lambda value: contrast(value, against))
    start = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    end = [int(toward[index : index + 2], 16) for index in (1, 3, 5)]
    for step in range(11):
        mixed = "#" + "".join(f"{round(a + (b - a) * step / 10):02x}" for a, b in zip(start, end, strict=True))
        if contrast(mixed, against) >= minimum:
            return mixed
    return toward


THEME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z")


def scheme(
    appearance: str,
    *,
    fg: str,
    comment: str,
    surface: str,
    rule: str,
    background: str,
    red: str,
    green: str,
    yellow: str,
    blue: str,
    purple: str,
    aqua: str,
    orange: str,
    pygments: str,
    diff_style: str,
) -> Palette:
    """Map a scheme's named colors onto wizolt's roles.

    Text stays the terminal's own foreground: prose is printed unstyled, and a pinned foreground
    would disagree with it. The selection band is the scheme's blue under its background color,
    the way these schemes draw their own popup-menu selection. The comment grey is meant for code
    comments and is dim by design; readable UI text is lifted toward the foreground while
    decorative separators retain a quieter grey. Syntax colors retain the published palette.
    """
    muted = lift(comment, fg, background, MUTED_CONTRAST)
    palette = Palette(
        appearance,
        {
            "text": "default",
            "muted": muted,
            "subtle": lift(comment, fg, background, 3.0),
            "accent": aqua,
            "accent_secondary": purple,
            "info": blue,
            "user": orange,
            "tool": green,
            "success": green,
            "warning": yellow,
            "error": red,
            "rule": comment,
            "syntax_assign": blue,
            "syntax_string": green,
            "syntax_number": purple,
            "syntax_ident": aqua,
            "syntax_builtin": yellow,
            # Code tokens in the scheme's foreground render as the terminal's default.
            "syntax_default": fg,
            "status_base": fg,
            "status_provider": blue,
            "status_reason": purple,
            "status_mcp": aqua,
            "status_context": yellow,
            "status_yolo": red,
            "status_worker": orange,
            "divider_glow": aqua,
            "divider_rule": rule,
            "selection_bg": lift(blue, fg, background, MENU_TEXT_CONTRAST),
            "selection_fg": background,
            "menu_bg": surface,
            "menu_muted": lift(comment, fg, surface, MENU_TEXT_CONTRAST),
            "pygments": pygments,
        },
        background,
        true_color=True,
        diff_style=diff_style,
    )
    # UI labels must remain readable even when a scheme's original accent is too faint.
    # Keep syntax tokens and the independent diff bands out of this adjustment.
    for role in (
        "accent",
        "accent_secondary",
        "info",
        "user",
        "tool",
        "success",
        "warning",
        "error",
        "status_base",
        "status_provider",
        "status_reason",
        "status_mcp",
        "status_context",
        "status_yolo",
        "status_worker",
    ):
        palette.colors[role] = lift(palette.colors[role], fg, background, MUTED_CONTRAST)
    return palette


# Published palettes, with UI contrast adjusted by scheme(); code uses Pygments' matching style.
# Each pairs with the diff style made for it where delta's collection has one; the rest with the
# style that suits its code: delta's deep bands under neon, zebra's soft ones elsewhere.
# fmt: off
BUILTIN: dict[str, Palette] = {
    "gruvbox-dark": scheme(
        "dark", fg="#ebdbb2", comment="#928374", surface="#3c3836", rule="#665c54", background="#282828",
        red="#fb4934", green="#b8bb26", yellow="#fabd2f", blue="#83a598", purple="#d3869b", aqua="#8ec07c", orange="#fe8019",
        pygments="gruvbox-dark", diff_style="gruvmax-fang",
    ),
    "gruvbox-light": scheme(
        "light", fg="#3c3836", comment="#928374", surface="#ebdbb2", rule="#bdae93", background="#fbf1c7",
        red="#9d0006", green="#79740e", yellow="#b57614", blue="#076678", purple="#8f3f71", aqua="#427b58", orange="#af3a03",
        pygments="gruvbox-light", diff_style="zebra",
    ),
    "solarized-dark": scheme(
        "dark", fg="#839496", comment="#586e75", surface="#073642", rule="#586e75", background="#002b36",
        red="#dc322f", green="#859900", yellow="#b58900", blue="#268bd2", purple="#d33682", aqua="#2aa198", orange="#cb4b16",
        pygments="solarized-dark", diff_style="platypus",
    ),
    "solarized-light": scheme(
        "light", fg="#657b83", comment="#93a1a1", surface="#eee8d5", rule="#93a1a1", background="#fdf6e3",
        red="#dc322f", green="#859900", yellow="#b58900", blue="#268bd2", purple="#d33682", aqua="#2aa198", orange="#cb4b16",
        pygments="solarized-light", diff_style="zebra",
    ),
    "nord": scheme(
        "dark", fg="#d8dee9", comment="#616e88", surface="#3b4252", rule="#4c566a", background="#2e3440",
        red="#bf616a", green="#a3be8c", yellow="#ebcb8b", blue="#81a1c1", purple="#b48ead", aqua="#88c0d0", orange="#d08770",
        pygments="nord", diff_style="calochortus-lyallii",
    ),
    "dracula": scheme(
        "dark", fg="#f8f8f2", comment="#6272a4", surface="#44475a", rule="#44475a", background="#282a36",
        red="#ff5555", green="#50fa7b", yellow="#f1fa8c", blue="#bd93f9", purple="#ff79c6", aqua="#8be9fd", orange="#ffb86c",
        pygments="dracula", diff_style="delta",
    ),
    "one-dark": scheme(
        "dark", fg="#abb2bf", comment="#5c6370", surface="#3e4452", rule="#4b5263", background="#282c34",
        red="#e06c75", green="#98c379", yellow="#e5c07b", blue="#61afef", purple="#c678dd", aqua="#56b6c2", orange="#d19a66",
        pygments="one-dark", diff_style="colibri",
    ),
    # https://github.com/sindresorhus/hyper-snazzy, with the comment and popup-menu greys from
    # https://github.com/connorholyday/vim-snazzy. Snazzy has no orange, so its yellow stands in,
    # and Pygments has no Snazzy style: Dracula shares its background and neon accents.
    "snazzy": scheme(
        "dark", fg="#eff0eb", comment="#606580", surface="#3a3d4d", rule="#606580", background="#282a36",
        red="#ff5c57", green="#5af78e", yellow="#f3f99d", blue="#57c7ff", purple="#ff6ac1", aqua="#9aedfe", orange="#f3f99d",
        pygments="dracula", diff_style="delta",
    ),
    # https://github.com/pygments/pygments/blob/master/pygments/styles/monokai.py
    "monokai": scheme(
        "dark", fg="#f8f8f2", comment="#959077", surface="#49483e", rule="#49483e", background="#272822",
        red="#ff4689", green="#a6e22e", yellow="#e6db74", blue="#66d9ef", purple="#ae81ff", aqua="#66d9ef", orange="#fd971f",
        pygments="monokai", diff_style="mantis-shrimp",
    ),
    # https://github.com/primer/github-vscode-theme (dark default), matching Pygments' gh_dark.py.
    "github-dark": scheme(
        "dark", fg="#e6edf3", comment="#8b949e", surface="#161b22", rule="#30363d", background="#0d1117",
        red="#f85149", green="#56d364", yellow="#d29922", blue="#79c0ff", purple="#d2a8ff", aqua="#a5d6ff", orange="#ffa657",
        pygments="github-dark", diff_style="zebra",
    ),
}
# fmt: on


def normalize_color(value: object) -> str | None:
    """A role's color in prompt-toolkit's spelling, or None if it is not one.

    `#rgb` widens to `#rrggbb` because Rich and `Theme.ramp` only read the long form.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if HEX_COLOR.match(value):
        digits = value[1:].lower()
        return "#" + ("".join(char * 2 for char in digits) if len(digits) == 3 else digits)
    if value in ("default", "ansidefault"):
        return "default"  # Rich has no name for `ansidefault`
    if value in ANSI_COLOR_NAMES:
        return value
    return None


def pygments_style_exists(name: str) -> bool:
    try:
        from pygments.styles import get_style_by_name

        get_style_by_name(name)
    except Exception:  # noqa: BLE001 - a missing or broken style is reported, not raised.
        return False
    return True


def load_custom(directory: str, builtins: dict[str, Palette], roles: tuple[str, ...]) -> tuple[dict[str, Palette], list[str]]:
    """Every theme file in `directory`, and a line for each problem found in them.

    A file that cannot be used at all is skipped; a bad entry inside a usable file is dropped and
    the rest of the file still applies.
    """
    try:
        entries = sorted(entry for entry in os.listdir(directory) if entry.endswith(".toml"))
    except OSError:
        return {}, []
    themes: dict[str, Palette] = {}
    problems: list[str] = []
    for entry in entries:
        name, path = entry.removesuffix(".toml"), os.path.join(directory, entry)
        if not THEME_NAME.match(name):
            problems.append(f"theme {path}: the file name must be letters, digits, '.', '_' or '-'")
            continue
        if name in builtins:
            problems.append(f"theme {path}: `{name}` is a built-in theme; rename the file")
            continue
        try:
            with open(path, "rb") as file:
                data = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as error:
            problems.append(f"theme {path}: {error}")
            continue
        base_name = data.get("base", "dark")
        base = builtins.get(base_name) if isinstance(base_name, str) else None
        if base is None:
            problems.append(f"theme {path}: base must be one of {', '.join(builtins)}")
            continue
        colors = dict(base.colors)
        pygments = data.get("pygments")
        if pygments is not None:
            if isinstance(pygments, str) and pygments_style_exists(pygments):
                colors["pygments"] = pygments
            else:
                problems.append(f"theme {path}: no Pygments style named {pygments!r}; keeping {colors['pygments']}")
        overrides = data.get("colors", {})
        if not isinstance(overrides, dict):
            problems.append(f"theme {path}: [colors] must be a table")
            overrides = {}
        for role, value in overrides.items():
            color = normalize_color(value)
            if role not in roles:
                problems.append(f"theme {path}: unknown role `{role}`")
            elif color is None or (role in HEX_ROLES and not color.startswith("#")):
                expected = "a #rrggbb color" if role in HEX_ROLES else "a #rrggbb color, default, or a terminal color like ansicyan"
                problems.append(f"theme {path}: {role} must be {expected}, not {value!r}")
            else:
                colors[role] = color
        diff = dict(base.diff)
        bands = data.get("diff", {})
        if not isinstance(bands, dict):
            problems.append(f"theme {path}: [diff] must be a table")
            bands = {}
        for key, value in bands.items():
            color = normalize_color(value)
            if key not in DIFF_KEYS:
                problems.append(f"theme {path}: unknown [diff] key `{key}`; use {', '.join(DIFF_KEYS)}")
            elif color is None or color == "default":
                problems.append(f"theme {path}: diff {key} must be a #rrggbb color or a terminal color like ansiblue, not {value!r}")
            else:
                diff[DIFF_KEYS[key]] = "bg:" + color
        highlights = dict(base.highlights)
        groups = data.get("highlights", {})
        if not isinstance(groups, dict):
            problems.append(f"theme {path}: [highlights] must be a table")
            groups = {}
        for group, spec in groups.items():
            if not THEME_NAME.fullmatch(group) or not isinstance(spec, dict) or set(spec) - {"fg", "bg", "bold", "italic", "underline"}:
                problems.append(f"theme {path}: invalid highlight group {group!r}")
                continue
            parts = []
            valid = True
            for key, value in spec.items():
                if key in ("fg", "bg"):
                    color = normalize_color(value)
                    if color is None:
                        valid = False
                    else:
                        parts.append(f"{key}:{color}")
                elif not isinstance(value, bool):
                    valid = False
                else:
                    parts.append(key if value else "no" + key)
            if valid:
                highlights[group] = " ".join(parts)
            else:
                problems.append(f"theme {path}: invalid highlight values for {group!r}")
        unknown = sorted(set(data) - {"base", "pygments", "colors", "diff", "highlights"})
        if unknown:
            problems.append(f"theme {path}: unknown key{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}")
        themes[name] = Palette(base.appearance, colors, base.background, base.true_color, diff, highlights, base.diff_style)
    return themes, problems
