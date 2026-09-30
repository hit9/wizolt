# Appearance

Customize the terminal's colors, bottom statusbar and divider above the input area.
Start with `/theme`, `/statusbar` or `/divider` to preview the built-in choices before saving.
For custom layouts and animations, edit your [config file](configuration.md) and restart the
session. Custom colors live in separate theme files.

## Color themes

Use `/theme` to preview and select a theme, or `/theme github-dark` to choose one directly.
The choice is saved as `runtime.theme` in your config file:

```toml
[runtime]
theme = "github-dark"
```

The default, `auto`, matches your terminal's background, falling back to `dark` if it cannot
detect it. `--theme NAME` overrides the configured theme for one launch.

`dark` and `light` draw in your terminal's own colors, so they follow whatever scheme your
terminal is set to. The named themes are ports of well-known Vim and Neovim colorschemes, classic
and current, taken from each one's own source: its code colors, its diff colors and its menus.

| Theme | Scheme |
| --- | --- |
| `gruvbox-dark`, `gruvbox-light` | gruvbox, warm and retro |
| `one-dark` | Atom's One Dark |
| `desert` | Vim's own desert, khaki and sky blue on grey |
| `zenburn` | Zenburn, low-contrast and easy on the eyes |
| `jellybeans` | Jellybeans, bright colors on near-black |
| `tokyonight` | Tokyo Night's night style, deep blue with bright accents |
| `catppuccin-dark`, `catppuccin-light` | Catppuccin mocha and latte, soft pastels |
| `kanagawa` | Kanagawa's wave style, muted colors after Hokusai's painting |
| `rose-pine-dark`, `rose-pine-light` | Rosé Pine main and dawn, rose and pine on violet |
| `everforest` | Everforest's dark style, green and earthy |

`runtime.theme` also accepts `gruvbox`, `catppuccin` or `rose-pine`, or any name you give both a
`-dark` and a `-light` theme file; it then picks the variant that matches your terminal, the way
`auto` does. In a named theme the working divider keeps to the scheme's accent.

A named theme does not change your terminal's background, so pick the one that matches it. Use
`/theme` to preview code, status colors, menu text and selected rows. Hints and menu descriptions
stay readable while separators remain quieter. `NO_COLOR=1` turns color off everywhere, keeping
bold and the selection bar.

Named themes, theme files based on one, and every diff style except `classic` are exact colors.
wizolt draws them exactly only when your terminal sets `COLORTERM=truecolor` (most modern terminals
support true color, but not every one sets the variable). Otherwise each color is rounded to the
nearest of 256, where a dark diff line can look black, and wizolt warns you at startup and after
`/theme`. If your terminal supports true color, add this to your shell profile:

```sh
export COLORTERM=truecolor
```

If it does not, use `dark` or `light` with the `classic` diff style, whose colors survive the
rounding.

### Diff colors

After you pick a theme, `/theme` offers the colors diffs are drawn in, previewed on a changed
line. The choice is saved as `ui.diff.style`:

```toml
[ui.diff]
style = "auto"
```

| Style | Colors |
| --- | --- |
| `auto` | The style the theme is paired with (the default) |
| `tokyonight`, `catppuccin`, `kanagawa`, `rose-pine`, `everforest`, `gruvbox`, `one-dark`, `jellybeans` | Each scheme's own diff colors; the theme of that name pairs with it |
| `classic` | Bright red and green; `dark`, `light` and `desert` pair with it |
| `delta` | Deeper red and green, the delta diff viewer's defaults; `zenburn` pairs with it |
| `zebra` | Soft red and green that stay behind the code |
| `gruvmax-fang`, `platypus`, `calochortus-lyallii`, `colibri`, `mantis-shrimp` | Sets from delta's theme collection, made for gruvbox, Solarized, Nord, One Half Dark and Monokai |

Every style marks removed lines in its red and added lines in its green, with the changed words a
shade stronger. A scheme's own colors are its own: Tokyo Night's added lines are teal, and Rosé
Pine, which has no green, marks them in blue. Some styles exist only for dark backgrounds, so the
menu offers them only with a dark theme; with a light one, they draw the theme's own pairing. If
red and green are hard to tell apart, a theme file's `[diff]` table can set other colors.

To make your own, add `<data_dir>/themes/<name>.toml` (`~/.wizolt/themes/` by default). It starts
from a built-in theme and changes only the colors you list:

```toml
base = "gruvbox-dark"         # any built-in theme; default "dark"
pygments = "monokai"          # optional: the Pygments style for code
[colors]
accent = "#83a598"            # "#rrggbb", "#rgb", "default", or a terminal color like "ansicyan"
user = "ansiyellow"
```

The roles you can set are `text`, `muted`, `subtle`, `accent`, `accent_secondary`, `info`,
`user`, `tool`, `success`, `warning`, `error`, `rule`, the code colors `syntax_assign`,
`syntax_string`, `syntax_number`, `syntax_ident`, `syntax_builtin` and `syntax_default`, the
status bar's `status_base`, `status_provider`, `status_reason`, `status_mcp`, `status_context`,
`status_yolo`, `status_worker` and `status_bg` (the band the `vim`, `split` and `monitor` layouts lie on), the working divider's `divider_glow` and `divider_rule` (these
two take `#rrggbb` only) and `divider_label`, and `selection_bg`, `selection_fg`, `menu_bg` and
`menu_muted` (the completion menu's descriptions).

A theme file pairs with its base theme's [diff colors](#diff-colors). A `[diff]` table sets
individual bands instead; they win over whichever diff style is selected:

```toml
[diff]
added = "#003a66"          # the added line
added_word = "#0061a8"     # the words it changed
removed = "#5c3300"
removed_word = "#a35a00"
```

The file name is the theme's name, so it cannot be a built-in theme, `auto`, or a pair's name
(`gruvbox`, or `mine` once `mine-dark` and `mine-light` exist). A mistake in a theme file is
reported at startup and when `/theme` opens; the rest of the file still applies. `/theme` re-reads the folder each time it opens, so edits show up without a restart.

## Statusbar and divider

Use `/statusbar` to preview presets directly in the bottom statusbar. `/divider` first selects a layout,
then a sweep. Each picker also offers your current custom value when you have one.
Moving the selection previews it; Enter saves it. Esc cancels the current picker, keeping
any choice you already confirmed. Divider previews include idle, running and queued examples,
so you can try animations without sending a model request.
The command reports whether the choice was saved or only applied to this session.

```toml
[ui.statusbar]
format = "preset:powerline"

[ui.divider]
format = "preset:comet"
sweep = "preset:comet"
```

Statusbar layouts:

| Preset | Appearance |
| --- | --- |
| `default` | Provider, model, reasoning and usage in one row |
| `minimal` | Model and a five-cell context meter together on the left |
| `split` | On a band: provider, model and reasoning on the left; tools, a ten-cell context meter and cache on the right |
| `compact` | Model, reasoning and context percentage joined by `›` in a short row |
| `brackets` | Model, reasoning and usage in dim square brackets, together on the left |
| `monitor` | On a band, dim labels beside bright values: model and reasoning on the left; tools, usage and worker summary on the right |
| `blocks` | Rectangular color segments: model and reasoning on the left, cache and context on the right; no special font needed |
| `powerline` | Color segments joined by arrow-shaped separators |
| `vim` | On a band like Vim's status line: model and reasoning on the left, MCP and context on the right |
| `lualine` | A highlighted session identity, model and reasoning on the left; tools, cache and context on the right |

Divider layouts in the picker:

| Preset | Appearance |
| --- | --- |
| `comet` | A quiet rule with a waiting dot and activity label |
| `capsule` | A centered, rounded activity badge with the waiting dot, and light rules on both sides |
| `frame` | Rounded top corners and a titled activity label frame the input area |
| `rail` | Activity on the left; elapsed time, speed and queue counts on the right |
| `powerline` | Activity and metrics in color segments joined to the rule by arrows |

The older divider presets `plain`, `minimal`, `dashed`, `dotted`, `double` and `right` still work
in config files. If you use one, the picker offers
`current (name)` so you can keep it; custom templates appear as `custom (current)`.

Sweeps in the picker:

| Preset | Motion |
| --- | --- |
| `none` | No animation |
| `comet` | A bright head and fading tail ease back and forth in a six-second cycle |
| `ripple` | Paired pulses spread outwards from the middle of the line |
| `aurora` | Broad, slow bands of light overlap |

These animations travel along the fill regions, skipping the labels, and keep their timing
when the terminal width changes.

In the statusbars other than `default`, `powerline` and `lualine`, the context figure turns
yellow from 70% and red from 90%. The older sweeps `scan`, `reverse`, `breathe`, `wave`, `twin`
and `pulse` still work in your config and can be kept
through `current (name)` in the picker.

The defaults are `preset:default`, `preset:comet` and `preset:comet`. Layout and sweep are
independent: a sweep colors the divider's fill region only while running. The `powerline` and `lualine` statusbars and `powerline` divider require a
font containing `` and ``. The `capsule` divider uses rounded Powerline glyphs `` and ``
(available in Nerd Fonts). The other presets need no special font.

`/statusbar powerline` and `/divider frame` select layouts directly. Edit your config file
for custom templates and formulas, then restart the session. Invalid settings at startup use
the defaults.

### Custom templates

Replace a `preset:name` value with a template. The statusbar and divider use the same syntax:

```toml
[ui.statusbar]
format = "[status_provider]{model}[/]{>}[muted]ctx {context.percent}%[/]"

[ui.divider]
format = "{% if running %}[accent bold]{activity}[/] · {elapsed:duration} {% endif %}[subtle]{fill:─}[/]"
sweep = "preset:none"
```

| Syntax | Result |
| --- | --- |
| `{model}` | Insert a field as plain text |
| `{elapsed:duration}` | Seconds followed by `s`; numbers also accept `:d` and `:.0f` through `:.6f` |
| `[accent bold]…[/]` | Theme color or highlight group, with optional text attributes; `[/]` restores the previous style |
| `[fg=#fff bg=#333 bold]…[/]` | Explicit foreground, background and attributes; colors can also name theme roles |
| `[status.band] …` | Lay the rest of the row on the theme's status band, as `vim` does; end it with ` [reset]` |
| `[reset]` | Restore terminal defaults |
| `{>}` | Push the remaining content to the right |
| `{fill:─}` / `{fill:─·}` | Fill space by repeating a character or short pattern || `{join:}` / `{join:}` | Join adjacent background colors automatically |
| `{% if worker.active %}…{% else %}…{% endif %}` | Conditional content; `else` is optional |
| `{% optional priority=10 %}…{% endoptional %}` | Omit the whole span when space is short; lower priorities go first |

Multiple `{>}` or `{fill:…}` regions share the free space equally. For example,
`{fill:─} {label} {fill:─}` centers a label between two rules. Patterns accept 1–32 printable
single-column characters. Include a separator inside its optional span so both disappear together.
Remaining text is clipped to one terminal row, preserving the group after the last fill where
possible. Close styles inside conditional and optional spans. Double
brackets to print them literally: `{{`, `}}`, `[[`, `]]`.

Conditions support comparisons, parentheses, `and`, `or` and `not`. For example:

```text
{% if context.percent >= 80 %}[warning bold]ctx {context.percent}%[/]{% else %}[muted]ctx {context.percent}%[/]{% endif %}
```

Available fields:

| Fields | Meaning |
| --- | --- |
| `provider`, `model`, `reasoning`, `context.percent`, `cache.percent` | Current agent; during delegation these follow the worker |
| `yolo`, `mcp.count`, `mcp.label`, `skills.count` | Session settings and connected services; `mcp.label` includes discovery activity |
| `worker.active`, `worker.model`, `worker.context`, `worker.summary` | Worker activity, model, context percentage, and the parked worker's context label |
| `running`, `elapsed`, `rate` | Whether the agent is running, seconds since the turn started, and the current output-rate label |
| `activity`, `spinner`, `label` | Divider activity, waiting dot, and the complete activity/elapsed/queue label used by default |
| `queue.total`, `queue.followup`, `queue.next_turn`, `reset_pending` | Divider queue counts and pending context reset |

The full `label`, `spinner` and queue counts are populated only in the divider; the statusbar
supplies empty strings or zero for those fields. Put fields such as `rate` inside conditions when you want to omit their surrounding text
while they are empty. Field contents never become template instructions.

### Powerline colors

Built-in Powerline templates use the highlight groups `status.model`, `status.detail`,
`status.usage`, `divider.activity` and `divider.metrics`. They follow the selected color theme.
Override them, or define your own groups, in a theme file:

```toml
base = "github-dark"

[highlights."status.model"]
fg = "#0d1117"
bg = "#79c0ff"
bold = true
```

Highlight groups accept `fg`, `bg`, `bold`, `italic` and `underline`. Reload theme files with
`/theme`. A custom Powerline template can then be one string:

```toml
[ui.statusbar]
format = "[status.model] {model} {join:}[status.detail] {reasoning} {join:}[reset]{>}{join:}[status.usage] ctx {context.percent}% [reset]"
```

### Sweep formulas

A sweep is a brightness formula: `x` is the zero-based terminal column, `t` is elapsed seconds, and
`w` is the divider width. `u` runs from `0` to `1` across the fill regions only, skipping fixed
labels. Use `u` for a width-independent animation that stays on the line, or `x` for motion in
terminal columns. The result is clamped to `0..1`, blending `divider_rule` with `divider_glow`.
Labels keep their own colors.

```toml
[ui.divider]
sweep = "exp(-((u - pingpong(t / 3, 1)) / 0.1) ** 2)"
```

This glow crosses the fill regions in three seconds and returns in three more. Change `3` for
the crossing time or `0.1` for the glow's width. For a fixed speed of 18 columns per second:

```toml
[ui.divider]
sweep = "exp(-((x - pingpong(t * 18, w)) / 8) ** 2)"
```

Here, `18` controls speed and `8` controls width in columns. `pingpong(value, width)` reflects
motion at the ends; `sin`, `cos`, `exp`,
`sqrt`, `abs`, `min` and `max` are also available, with arithmetic including `%` and `**`.
Use `"0"` or `"preset:none"` to stop the sweep.

Formulas are limited mathematical expressions, not Python scripts. Invalid formulas report an
error. If a formula becomes undefined while running, the affected light goes dark; the divider
preview displays the error. Templates are limited to 8,192 characters and 512 tokens, with at most 16 nested styles or
conditional blocks. Formulas are limited to 1,024 characters.

