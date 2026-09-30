# Custom appearance

Use this reference when the presets in [Appearance](appearance.md) are not enough.

## Theme files

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

A theme file pairs with its base theme's [diff colors](appearance.md#diff-colors). A `[diff]` table sets
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


## Layouts and animations

Set custom templates in your [config file](configuration.md) and restart wizolt.

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
| `{fill:─}` / `{fill:─·}` | Fill space by repeating a character or short pattern |
| `{join:}` / `{join:}` | Join adjacent background colors automatically |
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
base = "gruvbox-dark"

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

Invalid templates or formulas report an error. Keep the previous working value until your
new one previews correctly.
