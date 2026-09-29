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
from dataclasses import dataclass

from prompt_toolkit.styles import ANSI_COLOR_NAMES


@dataclass(frozen=True)
class Palette:
    """One theme: whether it is drawn for a light or dark background, and every role's color."""

    appearance: str  # "dark" | "light": picks the pinned diff colors
    colors: dict[str, str]  # every role, plus "pygments"


# `Theme.ramp` interpolates between these two, so they must be hex rather than a terminal color.
HEX_ROLES = ("divider_glow", "divider_rule")
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
    code_fg: str = "",
) -> Palette:
    """Map a scheme's named colors onto wizolt's roles.

    Text stays the terminal's own foreground: prose is printed unstyled, and a pinned foreground
    would disagree with it. The selection band is the scheme's blue under its background color,
    the way these schemes draw their own popup-menu selection.
    """
    return Palette(
        appearance,
        {
            "text": "default",
            "muted": comment,
            "subtle": comment,
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
            # Code tokens in the Pygments style's own foreground render as the terminal's default.
            "syntax_default": code_fg or fg,
            "status_base": fg,
            "status_provider": blue,
            "status_reason": purple,
            "status_mcp": aqua,
            "status_context": yellow,
            "status_yolo": red,
            "status_worker": orange,
            "divider_glow": aqua,
            "divider_rule": rule,
            "selection_bg": blue,
            "selection_fg": background,
            "menu_bg": surface,
            "pygments": pygments,
        },
    )


# Each scheme's colors are its published palette; the Pygments style is the one of the same name
# that Pygments ships, so fenced code matches the scheme too.
# fmt: off
BUILTIN: dict[str, Palette] = {
    "gruvbox-dark": scheme(
        "dark", fg="#ebdbb2", comment="#928374", surface="#3c3836", rule="#665c54", background="#282828",
        red="#fb4934", green="#b8bb26", yellow="#fabd2f", blue="#83a598", purple="#d3869b", aqua="#8ec07c", orange="#fe8019",
        pygments="gruvbox-dark", code_fg="#dddddd",
    ),
    "gruvbox-light": scheme(
        "light", fg="#3c3836", comment="#928374", surface="#ebdbb2", rule="#bdae93", background="#fbf1c7",
        red="#9d0006", green="#79740e", yellow="#b57614", blue="#076678", purple="#8f3f71", aqua="#427b58", orange="#af3a03",
        pygments="gruvbox-light",
    ),
    "solarized-dark": scheme(
        "dark", fg="#839496", comment="#586e75", surface="#073642", rule="#586e75", background="#002b36",
        red="#dc322f", green="#859900", yellow="#b58900", blue="#268bd2", purple="#d33682", aqua="#2aa198", orange="#cb4b16",
        pygments="solarized-dark",
    ),
    "solarized-light": scheme(
        "light", fg="#657b83", comment="#93a1a1", surface="#eee8d5", rule="#93a1a1", background="#fdf6e3",
        red="#dc322f", green="#859900", yellow="#b58900", blue="#268bd2", purple="#d33682", aqua="#2aa198", orange="#cb4b16",
        pygments="solarized-light",
    ),
    "nord": scheme(
        "dark", fg="#d8dee9", comment="#616e88", surface="#3b4252", rule="#4c566a", background="#2e3440",
        red="#bf616a", green="#a3be8c", yellow="#ebcb8b", blue="#81a1c1", purple="#b48ead", aqua="#88c0d0", orange="#d08770",
        pygments="nord",
    ),
    "dracula": scheme(
        "dark", fg="#f8f8f2", comment="#6272a4", surface="#44475a", rule="#44475a", background="#282a36",
        red="#ff5555", green="#50fa7b", yellow="#f1fa8c", blue="#bd93f9", purple="#ff79c6", aqua="#8be9fd", orange="#ffb86c",
        pygments="dracula",
    ),
    "one-dark": scheme(
        "dark", fg="#abb2bf", comment="#5c6370", surface="#3e4452", rule="#4b5263", background="#282c34",
        red="#e06c75", green="#98c379", yellow="#e5c07b", blue="#61afef", purple="#c678dd", aqua="#56b6c2", orange="#d19a66",
        pygments="one-dark",
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


def load_custom(directory: str, builtins: dict[str, Palette]) -> tuple[dict[str, Palette], list[str]]:
    """Every theme file in `directory`, and a line for each problem found in them.

    A file that cannot be used at all is skipped; a bad entry inside a usable file is dropped and
    the rest of the file still applies.
    """
    try:
        entries = sorted(entry for entry in os.listdir(directory) if entry.endswith(".toml"))
    except OSError:
        return {}, []
    roles = [role for role in builtins["dark"].colors if role != "pygments"]
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
        unknown = sorted(set(data) - {"base", "pygments", "colors"})
        if unknown:
            problems.append(f"theme {path}: unknown key{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}")
        themes[name] = Palette(base.appearance, colors)
    return themes, problems
