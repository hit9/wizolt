"""Wizolt's named palettes and user-defined themes.

The default dark/light palettes follow terminal ANSI colors. Named palettes use distinct
accent families across messages, menus, status bars, and code. The terminal retains its own
background. Custom themes extend a built-in palette through TOML colors and highlights.
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
    # A port's code colors, which become its Pygments style when Pygments has none of its own.
    syntax: Syntax | None = None


@dataclass(frozen=True)
class Syntax:
    """A port's syntax highlight colors, mapped to Pygments token groups."""

    comment: str
    keyword: str
    function: str
    string: str
    number: str
    constant: str
    type: str
    operator: str
    preproc: str  # decorators and preprocessor lines
    builtin: str  # built-in functions and types
    variable_builtin: str  # self, cls
    property: str  # attributes and keyword arguments
    punctuation: str
    italic_comments: bool = True
    italic_keywords: bool = False


# `[diff]` keys in a theme file: the diff key each one recolors and how its color is used -- "bg:"
# for a band a line is painted with, "fg:" for the chrome drawn on one.
DIFF_KEYS = {
    "added": ("diff.added.bg", "bg:"),
    "added_word": ("diff.added.emph", "bg:"),
    "removed": ("diff.removed.bg", "bg:"),
    "removed_word": ("diff.removed.emph", "bg:"),
    "gutter": ("diff.gutter", "fg:"),
    "header": ("diff.header", "fg:"),
    "hunk": ("diff.hunk", "fg:"),
    "added_sign": ("diff.added.sign", "fg:"),
    "removed_sign": ("diff.removed.sign", "fg:"),
}


def bands(added: str, added_word: str, removed: str, removed_word: str) -> dict[str, str]:
    """A diff style's four bands for one appearance: green under added lines and the words they
    changed, red under removed ones."""
    return {"diff.added.bg": added, "diff.added.emph": added_word, "diff.removed.bg": removed, "diff.removed.emph": removed_word}


def blend(color: str, toward: str, ratio: float) -> str:
    """`color` moved `ratio` of the way to `toward`; both `#rrggbb`. Painting `toward` at opacity
    `ratio` over `color` gives the same result, which is how Neovim schemes blend."""
    start = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    end = [int(toward[index : index + 2], 16) for index in (1, 3, 5)]
    return "#" + "".join(f"{round(a + (b - a) * ratio):02x}" for a, b in zip(start, end, strict=True))


@dataclass(frozen=True)
class DiffStyle:
    """The red and green a diff is drawn on, for the appearances the style was made for."""

    note: str  # what the picker says about it
    dark: dict[str, str] | None = None
    light: dict[str, str] | None = None


# Independent diff styles: preserve classic's established red and green bands.
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
}
# fmt: on


# `Theme.ramp` interpolates between these two, so they must be hex rather than a terminal color.
HEX_ROLES = ("divider_glow", "divider_rule")
# WCAG contrast floors for a scheme's grey text: secondary text on the background, and the menu's
# descriptions on its raised surface, which is where a comment grey reads worst.
MUTED_CONTRAST = 5.5
MENU_TEXT_CONTRAST = 5.5


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
    # ANSI names and `default` belong to the user's terminal palette. Their RGB values
    # are unknown, so keep explicit overrides rather than guessing or parsing names as hex.
    if not all(value.startswith("#") for value in (color, toward, against)):
        return color
    if contrast(toward, against) < minimum:
        toward = max(("#000000", "#ffffff"), key=lambda value: contrast(value, against))
    for step in range(11):
        mixed = blend(color, toward, step / 10)
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
    status: str,
    pygments: str,
    diff_style: str,
    syntax: Syntax | None = None,
) -> Palette:
    """Map a palette onto UI roles, lifting text contrast on its actual surfaces."""
    muted = lift(comment, fg, background, MUTED_CONTRAST)
    provider, model = aqua, purple
    success = "#98c78d" if appearance == "dark" else "#327244"
    # Tool arguments are colored like code: keys as properties, values as strings and numbers.
    code = {"assign": blue, "string": green, "number": purple, "ident": aqua, "builtin": yellow}
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
            # Keep success green even when a theme uses cyan for tools. Custom colors can override it.
            "success": success,
            "warning": yellow,
            "error": red,
            "rule": comment,
            "syntax_assign": code["assign"],
            "syntax_string": code["string"],
            "syntax_number": code["number"],
            "syntax_ident": code["ident"],
            "syntax_builtin": code["builtin"],
            # Code tokens in the scheme's foreground render as the terminal's default.
            "syntax_default": fg,
            "status_base": fg,
            "status_provider": provider,
            "status_model": model,
            "status_reason": yellow,
            "status_mcp": comment,
            # The service counts' own hue: magenta, the one classic accent no other status
            # role takes, so the counts never share a color with a neighbor or a separator.
            "status_services": blend(purple, red, 0.5),
            "status_context": success,
            "status_cache": blue,
            "status_yolo": red,
            "status_agent": orange,
            # Mix raw accents with the theme's background, before text contrast is lifted.
            # Lifted ink reused as a surface makes all segments pale; saturated accent fills
            # overpower the theme. Only the model gets a stronger, still blended surface.
            "status_provider_bg": blend(background, provider, 0.28),
            "status_model_bg": blend(background, model, 0.5),
            "status_reason_bg": blend(background, yellow, 0.35),
            "status_context_bg": blend(background, success, 0.27),
            "status_cache_bg": blend(background, blue, 0.21),
            "status_agent_bg": blend(background, orange, 0.17),
            "status_yolo_bg": blend(background, red, 0.27),
            "status_bg": status,
            "user_bg": blend(background, "#ffffff" if appearance == "dark" else "#000000", 0.04),
            "divider_glow": aqua,
            "divider_rule": rule,
            "divider_label": aqua,
            "selection_bg": lift(blue, fg, background, 4.5),
            "selection_fg": background,
            "menu_bg": surface,
            "menu_muted": lift(comment, fg, surface, MENU_TEXT_CONTRAST),
            "pygments": pygments,
        },
        background,
        true_color=True,
        diff_style=diff_style,
        syntax=syntax,
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
        "divider_label",
    ):
        palette.colors[role] = lift(palette.colors[role], fg, background, 4.5)
    palette.colors["user"] = lift(palette.colors["user"], fg, palette.colors["user_bg"], 4.5)
    for role in (
        "status_base",
        "status_provider",
        "status_model",
        "status_reason",
        "status_mcp",
        "status_services",
        "status_context",
        "status_cache",
        "status_yolo",
        "status_agent",
    ):
        edge = "#ffffff" if appearance == "dark" else "#000000"
        palette.colors[role] = lift(lift(palette.colors[role], edge, status, MUTED_CONTRAST), edge, background, MUTED_CONTRAST)
    return palette


# Each palette has a distinct accent family. Code uses Pygments' established styles;
# diffs remain independently selectable and default to classic in every palette.
# fmt: off
BUILTIN: dict[str, Palette] = {
    "slate": scheme(
        "dark", fg="#d7e5f1", comment="#91a5b7", surface="#293d52", rule="#4a6884", background="#242b35",
        red="#f08c91", green="#82d6bd", yellow="#e8ca83", blue="#79b9f2", purple="#c4acf0", aqua="#6bb9ff", orange="#e7bf86",
        status="#1f3042", pygments="nord", diff_style="classic",
    ),
    "forest": scheme(
        "dark", fg="#dde8cf", comment="#9caa91", surface="#30422d", rule="#566a44", background="#252d26",
        red="#ed9691", green="#9cd59b", yellow="#e1cf7d", blue="#83c6d1", purple="#d5b1a0", aqua="#b1d878", orange="#e4bd7c",
        status="#243525", pygments="zenburn", diff_style="classic",
    ),
    "sand": scheme(
        "dark", fg="#f0dfc0", comment="#b2a087", surface="#493723", rule="#7b603c", background="#30291f",
        red="#f3977e", green="#b9ce8a", yellow="#f2cc72", blue="#83bec3", purple="#d7a8bf", aqua="#ffc15e", orange="#ffc46d",
        status="#3a2d1e", pygments="gruvbox-dark", diff_style="classic",
    ),
    "plum": scheme(
        "dark", fg="#ece0f5", comment="#b19bbb", surface="#432f50", rule="#695281", background="#292330",
        red="#f394b0", green="#87d4ca", yellow="#e6c986", blue="#a2bdf0", purple="#cca7ee", aqua="#d1a0ff", orange="#eda6c3",
        status="#33223f", pygments="dracula", diff_style="classic",
    ),
    "paper": scheme(
        "light", fg="#243b4a", comment="#617482", surface="#e0e9ef", rule="#a4b8c6", background="#f4f6f8",
        red="#a63d49", green="#297c71", yellow="#886323", blue="#286496", purple="#805795", aqua="#286496", orange="#8b5942",
        status="#e0e9ef", pygments="friendly", diff_style="classic",
    ),
    # https://github.com/morhetz/gruvbox — retain its warm, multicolor identity.
    "gruvbox-dark": scheme(
        "dark", fg="#ebdbb2", comment="#928374", surface="#3c3836", rule="#665c54", background="#282828",
        red="#fb4934", green="#b8bb26", yellow="#fabd2f", blue="#83a598", purple="#d3869b", aqua="#8ec07c", orange="#fe8019",
        status="#504945", pygments="gruvbox-dark", diff_style="classic",
    ),
    # https://ethanschoonover.com/solarized/ — contrast is lifted only where UI text needs it.
    "solarized-dark": scheme(
        "dark", fg="#839496", comment="#586e75", surface="#073642", rule="#586e75", background="#002b36",
        red="#dc322f", green="#859900", yellow="#b58900", blue="#268bd2", purple="#6c71c4", aqua="#2aa198", orange="#cb4b16",
        status="#073642", pygments="solarized-dark", diff_style="classic",
    ),
    # https://spec.draculatheme.com/ — pink, purple, cyan and bright green.
    "dracula": scheme(
        "dark", fg="#f8f8f2", comment="#6272a4", surface="#44475a", rule="#6272a4", background="#282a36",
        red="#ff5555", green="#50fa7b", yellow="#f1fa8c", blue="#8be9fd", purple="#bd93f9", aqua="#ff79c6", orange="#ffb86c",
        status="#44475a", pygments="dracula", diff_style="classic",
    ),
    # https://github.com/NLKNguyen/papercolor-theme — Type uses pink; pythonOperator uses purple.
    # These share Python Statement colors by design, not positional field order.
    "papercolor-light": scheme(
        "light", fg="#444444", comment="#878787", surface="#d0d0d0", rule="#bcbcbc", background="#eeeeee",
        red="#af0000", green="#008700", yellow="#5f8700", blue="#0087af", purple="#8700af", aqua="#005f87", orange="#d75f00",
        status="#d0d0d0", pygments="papercolor-light", diff_style="classic",
        syntax=Syntax(
            comment="#878787", keyword="#d70087", function="#0087af", string="#5f8700", number="#d75f00", constant="#008700",
            type="#d70087", operator="#8700af", preproc="#d75f00", builtin="#444444", variable_builtin="#444444", property="#005faf", punctuation="#444444",
        ),
    ),
    "papercolor-dark": scheme(
        "dark", fg="#d0d0d0", comment="#808080", surface="#303030", rule="#585858", background="#1c1c1c",
        red="#af005f", green="#5faf00", yellow="#d7af5f", blue="#5fafd7", purple="#af87d7", aqua="#d7875f", orange="#ffaf00",
        status="#3a3a3a", pygments="papercolor-dark", diff_style="classic",
        syntax=Syntax(
            comment="#808080", keyword="#afd700", function="#5fafd7", string="#d7af5f", number="#ff5faf", constant="#5faf00",
            type="#afd700", operator="#af87d7", preproc="#ff5faf", builtin="#d0d0d0", variable_builtin="#d0d0d0", property="#00afaf", punctuation="#d0d0d0",
        ),
    ),
}
# fmt: on


def generated_pygments_style(name: str) -> type | None:
    """The Pygments style for a port's `syntax`, built on first use; None for any other name."""
    palette = next((palette for palette in BUILTIN.values() if palette.syntax and palette.colors["pygments"] == name), None)
    if palette is None or palette.syntax is None:
        return None
    from pygments.style import Style
    from pygments.token import Comment, Keyword, Name, Number, Operator, Punctuation, String, Token

    syntax = palette.syntax
    comment = ("italic " if syntax.italic_comments else "") + syntax.comment
    keyword = ("italic " if syntax.italic_keywords else "") + syntax.keyword
    styles = {
        # Rich draws a token without a color in black, so plain text needs the scheme's foreground.
        Token: palette.colors["syntax_default"],
        Comment: comment,
        Comment.Preproc: syntax.preproc,
        Keyword: keyword,
        Keyword.Constant: syntax.constant,
        Keyword.Type: syntax.type,
        Name.Function: syntax.function,
        Name.Function.Magic: syntax.function,
        Name.Class: syntax.type,
        Name.Builtin: syntax.builtin,
        Name.Builtin.Pseudo: syntax.variable_builtin,
        Name.Decorator: syntax.preproc,
        Name.Constant: syntax.constant,
        Name.Attribute: syntax.property,
        Name.Tag: syntax.keyword,
        String: syntax.string,
        Number: syntax.number,
        Operator: syntax.operator,
        Operator.Word: keyword,
        Punctuation: syntax.punctuation,
    }
    return type(f"{name}Style", (Style,), {"name": name, "background_color": palette.background, "styles": styles})


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
    if any(palette.syntax and palette.colors["pygments"] == name for palette in BUILTIN.values()):
        return True
    try:
        from pygments.styles import get_style_by_name

        get_style_by_name(name)
    except Exception:  # noqa: BLE001 - a missing or broken style is reported, not raised.
        return False
    return True


def load_custom(directory: str, builtins: dict[str, Palette], roles: tuple[str, ...], inline: object = None) -> tuple[dict[str, Palette], list[str]]:
    """Load theme files and inline definitions through the same validation.

    A file that cannot be used at all is skipped; a bad entry inside a usable file is dropped and
    the rest of the file still applies.
    """
    try:
        entries = sorted(entry for entry in os.listdir(directory) if entry.endswith(".toml"))
    except OSError:
        entries = []
    problems: list[str] = []
    sources: list[tuple[str, str, dict]] = []
    for entry in entries:
        name, path = entry.removesuffix(".toml"), os.path.join(directory, entry)
        try:
            with open(path, "rb") as file:
                sources.append((name, path, tomllib.load(file)))
        except (OSError, tomllib.TOMLDecodeError) as error:
            problems.append(f"theme {path}: {error}")
    if inline is not None:
        if not isinstance(inline, dict):
            problems.append("[ui.themes] must be a table")
        else:
            for name, data in inline.items():
                if not isinstance(data, dict):
                    problems.append(f"theme ui.themes.{name}: must be a table")
                else:
                    sources.append((name, f"ui.themes.{name}", data))
    # Process inline definitions last. A usable definition replaces the same-named file;
    # an unusable one leaves that file available, just as invalid entries keep base colors.
    accepted = []
    for name, path, data in sources:
        if name.startswith("plugins."):
            problems.append(f"theme {path}: the plugins. namespace is reserved for plugin themes")
        else:
            accepted.append((name, path, data))
    themes, validation = compile_themes(accepted, builtins, roles)
    return themes, [*problems, *validation]


def compile_themes(sources: list[tuple[str, str, dict]], builtins: dict[str, Palette], roles: tuple[str, ...]) -> tuple[dict[str, Palette], list[str]]:
    """Validate theme values independently of their source: files, config or plugin metadata."""
    themes: dict[str, Palette] = {}
    problems: list[str] = []
    for name, path, data in sources:
        if not THEME_NAME.match(name):
            problems.append(f"theme {path}: the name must be letters, digits, '.', '_' or '-'")
            continue
        if name in builtins:
            problems.append(f"theme {path}: `{name}` is a built-in theme; choose another name")
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
                band, prefix = DIFF_KEYS[key]
                diff[band] = prefix + color
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
