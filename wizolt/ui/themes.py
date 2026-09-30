"""Named color themes: ports of Neovim colorschemes, and the user's theme files.

The default `dark` and `light` palettes live on `render.Theme` and follow the terminal's own ANSI
colors. A named scheme is opt-in and pins every role to the scheme's hex values instead, so the
status footer, the menu, the selection band, diffs and highlighted code agree with a terminal
already set to that scheme. Each is taken from the scheme's own Neovim source; where Pygments has
no style for it, one is generated from the scheme's syntax groups. None of them paints the
terminal background: the transcript is native scrollback, which wizolt does not own.

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
    # A port's code colors, which become its Pygments style when Pygments has none of its own.
    syntax: Syntax | None = None


@dataclass(frozen=True)
class Syntax:
    """A scheme's code colors, from the Neovim highlight groups (tree-sitter's where it defines
    them) that each Pygments token reads as."""

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


def blend(color: str, toward: str, ratio: float) -> str:
    """`color` moved `ratio` of the way to `toward`; both `#rrggbb`. Painting `toward` at opacity
    `ratio` over `color` gives the same result, which is how Neovim schemes blend."""
    start = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    end = [int(toward[index : index + 2], 16) for index in (1, 3, 5)]
    return "#" + "".join(f"{round(a + (b - a) * ratio):02x}" for a, b in zip(start, end, strict=True))


def own_bands(added: str, removed: str, green: str, red: str) -> dict[str, str]:
    """A scheme's own diff: the line bands its Neovim port draws DiffAdd and DiffDelete on. Those
    ports mark changed words in a separate change color, so the word bands are derived instead:
    each line band pushed a third of the way to the scheme's own green or red."""
    return bands(added, blend(added, green, 0.35), removed, blend(removed, red, 0.35))


@dataclass(frozen=True)
class DiffStyle:
    """The red and green a diff is drawn on, for the appearances the style was made for."""

    note: str  # what the picker says about it
    dark: dict[str, str] | None = None
    light: dict[str, str] | None = None


# Solid red and green bands. Each named scheme's own come first, from its Neovim source, with
# the changed-word bands derived by own_bands(). Then wizolt's, delta's defaults, and sets from
# delta's theme collection (https://github.com/dandavison/delta/blob/main/themes.gitconfig),
# tuned by their authors against popular syntax themes; those are taken as published.
# fmt: off
DIFF_STYLES: dict[str, DiffStyle] = {
    # extras/lua/tokyonight_night.lua: DiffAdd and DiffDelete, blended from green2 and red1.
    "tokyonight": DiffStyle("tokyonight's own", dark=own_bands("#243e4a", "#4a272f", "#41a6b5", "#db4b4b")),
    # groups/syntax.lua: DiffAdd = darken(green, 0.18, base), DiffDelete = darken(red, 0.18, base).
    "catppuccin": DiffStyle(
        "catppuccin's own",
        dark=own_bands(blend("#1e1e2e", "#a6e3a1", 0.18), blend("#1e1e2e", "#f38ba8", 0.18), "#a6e3a1", "#f38ba8"),
        light=own_bands(blend("#eff1f5", "#40a02b", 0.18), blend("#eff1f5", "#d20f39", 0.18), "#40a02b", "#d20f39"),
    ),
    # themes.lua (wave): diff.add = winterGreen, diff.delete = winterRed; vcs autumnGreen, autumnRed.
    "kanagawa": DiffStyle("kanagawa's own", dark=own_bands("#2b3328", "#43242b", "#76946a", "#c34043")),
    # rose-pine.lua: DiffAdd is git_add (foam) and DiffDelete git_delete (love), blended 20%.
    "rose-pine": DiffStyle(
        "rose-pine's own",
        dark=own_bands(blend("#191724", "#9ccfd8", 0.2), blend("#191724", "#eb6f92", 0.2), "#9ccfd8", "#eb6f92"),
        light=own_bands(blend("#faf4ed", "#56949f", 0.2), blend("#faf4ed", "#b4637a", 0.2), "#56949f", "#b4637a"),
    ),
    # everforest.vim (dark, medium): DiffAdd on bg_green, DiffDelete on bg_red.
    "everforest": DiffStyle("everforest's own", dark=own_bands("#425047", "#514045", "#a7c080", "#e67e80")),
    # gruvbox.nvim: DiffAdd on dark_green or light_green, DiffDelete on dark_red or light_red.
    "gruvbox": DiffStyle(
        "gruvbox's own",
        dark=own_bands("#62693e", "#722529", "#b8bb26", "#fb4934"),
        light=own_bands("#d5d39b", "#fc9487", "#79740e", "#9d0006"),
    ),
    # onedark.nvim palette (dark): diff_add, diff_delete.
    "one-dark": DiffStyle("one-dark's own", dark=own_bands("#31392b", "#382b2c", "#98c379", "#e86671")),
    # jellybeans.vim: DiffAdd on #437019, DiffDelete on #700009.
    "jellybeans": DiffStyle("jellybeans' own", dark=own_bands("#437019", "#700009", "#99ad6a", "#cf6a4c")),
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
    "gruvmax-fang": DiffStyle("delta's, made for gruvbox-dark; very dark", dark=bands("#001a00", "#003300", "#330011", "#80002a")),
    "platypus": DiffStyle("delta's, made for Solarized dark", dark=bands("#2a5e37", "#1a861a", "#5d001e", "#b80000")),
    "calochortus-lyallii": DiffStyle("delta's, made for Nord", dark=bands("#004000", "#007800", "#400000", "#780000")),
    "colibri": DiffStyle("delta's, made for One Half Dark", dark=bands("#003500", "#007e5e", "#5e0000", "#80002a")),
    "mantis-shrimp": DiffStyle("delta's, made for Monokai", dark=bands("#004433", "#007800", "#5d001e", "#b80000")),
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
    """Map a scheme's named colors onto wizolt's roles.

    `status` is the panel a status line lies on in the scheme. `syntax`, for a scheme Pygments
    has no style of its own for, colors code as the scheme does; `pygments` then names the style
    generated from it.

    Text stays the terminal's own foreground: prose is printed unstyled, and a pinned foreground
    would disagree with it. The selection band is the scheme's blue under its background color,
    the way these schemes draw their own popup-menu selection. The comment grey is meant for code
    comments and is dim by design; readable UI text is lifted toward the foreground while
    decorative separators retain a quieter grey. Syntax colors retain the published palette.

    The status footer colors what each field is: the model in the accent, reasoning in the second
    accent, usage in the informational blue, tools in grey. `status` is the band a preset may lay
    it on, so its text is lifted to read both there and on the background. The working divider
    keeps to one hue, its label the accent its glow is drawn in.
    """
    muted = lift(comment, fg, background, MUTED_CONTRAST)
    # Tool arguments are colored like code: keys as properties, values as strings and numbers.
    code = {"assign": blue, "string": green, "number": purple, "ident": aqua, "builtin": yellow}
    if syntax is not None:
        code = {"assign": syntax.property, "string": syntax.string, "number": syntax.number, "ident": syntax.type, "builtin": syntax.builtin}
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
            "syntax_assign": code["assign"],
            "syntax_string": code["string"],
            "syntax_number": code["number"],
            "syntax_ident": code["ident"],
            "syntax_builtin": code["builtin"],
            # Code tokens in the scheme's foreground render as the terminal's default.
            "syntax_default": fg,
            "status_base": fg,
            "status_provider": aqua,
            "status_reason": purple,
            "status_mcp": comment,
            "status_context": blue,
            "status_yolo": red,
            "status_worker": orange,
            "status_bg": status,
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
    for role in ("status_base", "status_provider", "status_reason", "status_mcp", "status_context", "status_yolo", "status_worker"):
        palette.colors[role] = lift(lift(palette.colors[role], fg, status, MUTED_CONTRAST), fg, background, MUTED_CONTRAST)
    return palette


# Ports of Neovim colorschemes, from each one's own source: its palette, the background its
# StatusLine and Pmenu are drawn on, its DiffAdd and DiffDelete, and, where Pygments has no style
# for it, the colors its highlight groups give code. UI contrast is adjusted by scheme(). A pair
# named `-dark` and `-light` follows the terminal's background, as `auto` does.
# fmt: off
BUILTIN: dict[str, Palette] = {
    # https://github.com/ellisonleao/gruvbox.nvim; code in Pygments' gruvbox style.
    "gruvbox-dark": scheme(
        "dark", fg="#ebdbb2", comment="#928374", surface="#3c3836", rule="#665c54", background="#282828",
        red="#fb4934", green="#b8bb26", yellow="#fabd2f", blue="#83a598", purple="#d3869b", aqua="#8ec07c", orange="#fe8019",
        status="#504945", pygments="gruvbox-dark", diff_style="gruvbox",
    ),
    "gruvbox-light": scheme(
        "light", fg="#3c3836", comment="#928374", surface="#ebdbb2", rule="#bdae93", background="#fbf1c7",
        red="#9d0006", green="#79740e", yellow="#b57614", blue="#076678", purple="#8f3f71", aqua="#427b58", orange="#af3a03",
        status="#d5c4a1", pygments="gruvbox-light", diff_style="gruvbox",
    ),
    # https://github.com/navarasu/onedark.nvim (dark); code in Pygments' one-dark style.
    "one-dark": scheme(
        "dark", fg="#abb2bf", comment="#5c6370", surface="#3e4452", rule="#4b5263", background="#282c34",
        red="#e06c75", green="#98c379", yellow="#e5c07b", blue="#61afef", purple="#c678dd", aqua="#56b6c2", orange="#d19a66",
        status="#393f4a", pygments="one-dark", diff_style="one-dark",
    ),
    # https://github.com/folke/tokyonight.nvim, night: extras/lua/tokyonight_night.lua.
    "tokyonight": scheme(
        "dark", fg="#c0caf5", comment="#565f89", surface="#16161e", rule="#3b4261", background="#1a1b26",
        red="#f7768e", green="#9ece6a", yellow="#e0af68", blue="#7aa2f7", purple="#bb9af7", aqua="#7dcfff", orange="#ff9e64",
        status="#16161e", pygments="tokyonight", diff_style="tokyonight",
        syntax=Syntax(
            comment="#565f89", keyword="#9d7cd8", function="#7aa2f7", string="#9ece6a", number="#ff9e64", constant="#ff9e64",
            type="#2ac3de", operator="#89ddff", preproc="#7dcfff", builtin="#2ac3de", variable_builtin="#f7768e",
            property="#73daca", punctuation="#a9b1d6", italic_keywords=True,
        ),
    ),
    # https://github.com/catppuccin/nvim, mocha and latte: palettes/, groups/syntax.lua, editor.lua.
    "catppuccin-dark": scheme(
        "dark", fg="#cdd6f4", comment="#9399b2", surface="#181825", rule="#45475a", background="#1e1e2e",
        red="#f38ba8", green="#a6e3a1", yellow="#f9e2af", blue="#89b4fa", purple="#cba6f7", aqua="#94e2d5", orange="#fab387",
        status="#181825", pygments="catppuccin-dark", diff_style="catppuccin",
        syntax=Syntax(
            comment="#9399b2", keyword="#cba6f7", function="#89b4fa", string="#a6e3a1", number="#fab387", constant="#fab387",
            type="#f9e2af", operator="#89dceb", preproc="#f5c2e7", builtin="#fab387", variable_builtin="#f38ba8",
            property="#b4befe", punctuation="#9399b2",
        ),
    ),
    "catppuccin-light": scheme(
        "light", fg="#4c4f69", comment="#7c7f93", surface="#e6e9ef", rule="#bcc0cc", background="#eff1f5",
        red="#d20f39", green="#40a02b", yellow="#df8e1d", blue="#1e66f5", purple="#8839ef", aqua="#179299", orange="#fe640b",
        status="#e6e9ef", pygments="catppuccin-light", diff_style="catppuccin",
        syntax=Syntax(
            comment="#7c7f93", keyword="#8839ef", function="#1e66f5", string="#40a02b", number="#fe640b", constant="#fe640b",
            type="#df8e1d", operator="#04a5e5", preproc="#ea76cb", builtin="#fe640b", variable_builtin="#d20f39",
            property="#7287fd", punctuation="#7c7f93",
        ),
    ),
    # https://github.com/rebelot/kanagawa.nvim, wave: colors.lua, themes.lua, highlights/editor.lua.
    "kanagawa": scheme(
        "dark", fg="#dcd7ba", comment="#727169", surface="#223249", rule="#54546d", background="#1f1f28",
        red="#e46876", green="#98bb6c", yellow="#e6c384", blue="#7e9cd8", purple="#957fb8", aqua="#7fb4ca", orange="#ffa066",
        status="#16161d", pygments="kanagawa", diff_style="kanagawa",
        syntax=Syntax(
            comment="#727169", keyword="#957fb8", function="#7e9cd8", string="#98bb6c", number="#d27e99", constant="#ffa066",
            type="#7aa89f", operator="#c0a36e", preproc="#e46876", builtin="#7fb4ca", variable_builtin="#e46876",
            property="#e6c384", punctuation="#9cabca", italic_keywords=True,
        ),
    ),
    # https://github.com/rose-pine/neovim, main and dawn: palette.lua, rose-pine.lua, config.lua.
    # Rosé Pine has no green; its own `leaf` marks success.
    "rose-pine-dark": scheme(
        "dark", fg="#e0def4", comment="#908caa", surface="#1f1d2e", rule="#6e6a86", background="#191724",
        red="#eb6f92", green="#95b1ac", yellow="#f6c177", blue="#31748f", purple="#c4a7e7", aqua="#9ccfd8", orange="#ebbcba",
        status="#1f1d2e", pygments="rose-pine-dark", diff_style="rose-pine",
        syntax=Syntax(
            comment="#908caa", keyword="#31748f", function="#ebbcba", string="#f6c177", number="#f6c177", constant="#f6c177",
            type="#9ccfd8", operator="#908caa", preproc="#c4a7e7", builtin="#ebbcba", variable_builtin="#eb6f92",
            property="#9ccfd8", punctuation="#908caa",
        ),
    ),
    "rose-pine-light": scheme(
        "light", fg="#464261", comment="#797593", surface="#fffaf3", rule="#9893a5", background="#faf4ed",
        red="#b4637a", green="#6d8f89", yellow="#ea9d34", blue="#286983", purple="#907aa9", aqua="#56949f", orange="#d7827e",
        status="#fffaf3", pygments="rose-pine-light", diff_style="rose-pine",
        syntax=Syntax(
            comment="#797593", keyword="#286983", function="#d7827e", string="#ea9d34", number="#ea9d34", constant="#ea9d34",
            type="#56949f", operator="#797593", preproc="#907aa9", builtin="#d7827e", variable_builtin="#b4637a",
            property="#56949f", punctuation="#797593",
        ),
    ),
    # https://github.com/sainnhe/everforest, dark with medium contrast: autoload/everforest.vim.
    "everforest": scheme(
        "dark", fg="#d3c6aa", comment="#859289", surface="#3d484d", rule="#4f585e", background="#2d353b",
        red="#e67e80", green="#a7c080", yellow="#dbbc7f", blue="#7fbbb3", purple="#d699b6", aqua="#83c092", orange="#e69875",
        status="#3d484d", pygments="everforest", diff_style="everforest",
        syntax=Syntax(
            comment="#859289", keyword="#e67e80", function="#a7c080", string="#a7c080", number="#d699b6", constant="#83c092",
            type="#dbbc7f", operator="#e69875", preproc="#d699b6", builtin="#a7c080", variable_builtin="#d699b6",
            property="#7fbbb3", punctuation="#d3c6aa", italic_keywords=True,
        ),
    ),
    # The classics draw their status line in reverse video, light under dark text; their bands
    # here are the grey each draws its cursor line or inactive status line in instead, so the
    # footer's colors read on the band and without it alike.
    # Vim's own desert (runtime/colors/desert.vim). It marks deletions magenta, so diffs are classic's.
    "desert": scheme(
        "dark", fg="#ffffff", comment="#7f7f8c", surface="#666666", rule="#666666", background="#333333",
        red="#cd5c5c", green="#9acd32", yellow="#f0e68c", blue="#75a0ff", purple="#ffa0a0", aqua="#6dceeb", orange="#cd853f",
        status="#666666", pygments="desert", diff_style="classic",
        syntax=Syntax(
            comment="#6dceeb", keyword="#f0e68c", function="#89fb98", string="#ffa0a0", number="#ffa0a0", constant="#ffa0a0",
            type="#bdb76b", operator="#f0e68c", preproc="#cd5c5c", builtin="#89fb98", variable_builtin="#89fb98",
            property="#89fb98", punctuation="#ffde9b", italic_comments=False,
        ),
    ),
    # https://github.com/jnurmine/Zenburn; code in Pygments' zenburn style. It marks deletions
    # grey, so diffs are delta's.
    "zenburn": scheme(
        "dark", fg="#dcdccc", comment="#7f9f7f", surface="#2c2e2e", rule="#688060", background="#3f3f3f",
        red="#e37170", green="#7f9f7f", yellow="#f0dfaf", blue="#8cd0d3", purple="#dca3a3", aqua="#8cd0d3", orange="#ffcfaf",
        status="#434443", pygments="zenburn", diff_style="delta",
    ),
    # https://github.com/nanotech/jellybeans.vim.
    "jellybeans": scheme(
        "dark", fg="#e8e8d3", comment="#888888", surface="#606060", rule="#777777", background="#151515",
        red="#cf6a4c", green="#99ad6a", yellow="#fad07a", blue="#8197bf", purple="#c6b6ee", aqua="#8fbfdc", orange="#ffb964",
        status="#403c41", pygments="jellybeans", diff_style="jellybeans",
        syntax=Syntax(
            comment="#888888", keyword="#8197bf", function="#fad07a", string="#99ad6a", number="#cf6a4c", constant="#cf6a4c",
            type="#ffb964", operator="#8197bf", preproc="#8fbfdc", builtin="#fad07a", variable_builtin="#c6b6ee",
            property="#c6b6ee", punctuation="#668799",
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
