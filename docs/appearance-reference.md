# Custom appearance

Start with a preset you like in [Appearance](appearance.md), then change just the parts you want.
The examples below are ready to copy; the syntax and field tables help when you want to go further.

| Change | Where to put it | Apply it |
| --- | --- | --- |
| Colors and highlight groups | `~/.wizolt/themes/my-theme.toml` | Run `/theme my-theme` |
| Input, statusbar, divider or animation | Your config file, shown in `/theme` | Restart wizolt |

## Theme files

### Change two colors

Create `~/.wizolt/themes/my-theme.toml` (or `themes/` inside your custom data directory):

<!-- figure: custom-theme -->
```toml
base = "one-dark"

[colors]
user = "#f2c97d"
tool = "#8bd5ca"
```

Run `/theme my-theme`. Your messages become gold and tool labels become teal; everything else
keeps One Dark's colors. Edit the file and run the same command again to try another version.

```{figure} _static/appearance-custom-theme.svg
:alt: The same user message and Edit call in One Dark and in my-theme, with gold messages and teal tool labels.

Two overrides; the rest of the theme stays familiar.
```

`base` accepts any built-in theme and defaults to `dark`. Colors accept `#rrggbb`, `#rgb`,
terminal names such as `ansicyan`, or `default` for your terminal's color. An optional
`pygments = "monokai"` before `[colors]` chooses another style for code.

### Find the color to change

A *role* names what a color is used for. Set only the roles you want to change:

| Area | Roles |
| --- | --- |
| Reading and navigation | `text`, `muted`, `subtle`, `accent`, `accent_secondary`, `info`, `rule` |
| Messages and results | `user`, `tool`, `success`, `warning`, `error` |
| Code | `syntax_assign`, `syntax_string`, `syntax_number`, `syntax_ident`, `syntax_builtin`, `syntax_default` |
| Statusbar fields | `status_base`, `status_provider`, `status_reason`, `status_mcp`, `status_context`, `status_yolo`, `status_worker` |
| Statusbar background | `status_bg` — the band in `vim`, `split` and `monitor` |
| Divider | `divider_glow`, `divider_rule`, `divider_label`; glow and rule require `#rrggbb` |
| Menus | `selection_bg`, `selection_fg`, `menu_bg`, `menu_muted` |

### Change diff backgrounds

A theme file pairs with its base theme's [diff colors](appearance.md#diff-colors). A `[diff]` table sets
individual bands instead; they win over whichever diff style is selected. For example, blue
additions and brown removals:

```toml
[diff]
added = "#003a66"          # the added line
added_word = "#0061a8"     # the words it changed
removed = "#5c3300"
removed_word = "#a35a00"
```

`added_word` and `removed_word` make changed words stand out inside each line.

The file name is the theme's name, so it cannot be a built-in theme, `auto`, or a pair's name
(`gruvbox`, or `mine` once `mine-dark` and `mine-light` exist). A mistake in a theme file is
reported at startup and when `/theme` opens; the rest of the file still applies. `/theme` re-reads the folder each time it opens, so edits show up without a restart.


## Layouts and animations

Put these examples in your [config file](configuration.md), then restart wizolt.
Replace existing values rather than adding a second table with the same name.

### Input prefixes

Choose or edit input prefixes in `/theme`'s **Input** tab, or set them in the config file:

```toml
[ui.input]
prompt = "preset:chevron"
```

Presets are `default` (`>`), `chevron` (`❯`), `arrow` (`→`), `lambda` (`λ`) and `bullet` (`•`).
Follow-up input adds `+` before the same prefix. To choose your own text for both:

```toml
[ui.input]
prompt = "❯ "
running = "→ "
```

Spaces are kept as written. Use `""` to hide a prefix. Each prefix accepts one line of printable
text, up to 32 terminal columns; colors follow the active theme. The transcript's message
markers and tool rows keep their existing layout.

### Custom templates

A template is ordinary text with fields in `{braces}` and colors in `[brackets]`.
Replace a `preset:name` value with the template you want.

#### Build a statusbar in three steps

Start with just the model:

<!-- figure: statusbar-model -->
```toml
[ui.statusbar]
format = "[status_provider]{model}[/]"
```

`{model}` becomes the model name. `[status_provider]` gives it the theme's model color;
`[/]` ends that color.

Add the context percentage:

<!-- figure: statusbar-context -->
```toml
[ui.statusbar]
format = "[status_provider]{model}[/] · [status_context]ctx {context.percent}%[/]"
```

Now put context on the right. **`{>}`** fills the space between the two parts:

<!-- figure: statusbar-aligned -->
```toml
[ui.statusbar]
format = "[status_provider]{model}[/]{>}[status_context]ctx {context.percent}%[/]"
```

```{figure} _static/appearance-custom-statusbar.svg
:alt: Three statusbars: model only, model followed by context usage, then model on the left with context on the right.

One extra piece at a time.
```

#### Make room in a small pane

An *optional* span disappears when space runs short. Here, reasoning disappears first, leaving
the model and context percentage:

<!-- figure: statusbar-optional -->
```toml
[ui.statusbar]
format = "[status_provider]{model}[/]{% optional priority=10 %} · [status_reason]{reasoning}[/]{% endoptional %}{>}[status_context]ctx {context.percent}%[/]"
```

```{figure} _static/appearance-custom-width.svg
:alt: The same template in 72 and 24 columns; the narrower version omits medium reasoning and keeps the model and context percentage.

The same template adapts to the available space.
```

Lower priorities disappear first. Keep the separator inside its span, as above, so both
disappear together.

#### Center a label in the divider

**`{fill:─}`** draws as much line as the row needs. Two fills share the space equally:

<!-- figure: divider-centered -->
```toml
[ui.divider]
format = "[divider_rule]{fill:─}[/]{% if running %} [divider_label]{activity} · {elapsed:duration}[/] {% endif %}[divider_rule]{fill:─}[/]"
sweep = "preset:none"
```

```{figure} _static/appearance-custom-divider.svg
:alt: An idle divider is a plain line; when running, it centers working and 12s between two lines.

The label appears only while the agent works.
```

`{% if running %}` includes the label only while a task runs. `:duration` prints seconds with
an `s`, such as `12s`. Use `{label}` instead of `{activity} · {elapsed:duration}` if you also
want the usual speed and queue details.

#### Syntax to keep nearby

Statusbars and dividers share this syntax:

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

Start with `preset:comet`, `preset:ripple` or `preset:aurora` in `[ui.divider]` `sweep`.
Use `preset:none` for a still line. A formula gives you finer control.

#### A glow that crosses and returns

A sweep is a brightness formula: `x` is the zero-based terminal column, `t` is elapsed seconds, and
`w` is the divider width. `u` runs from `0` to `1` across the fill regions only, skipping fixed
labels. Use `u` for a width-independent animation that stays on the line, or `x` for motion in
terminal columns. The result is clamped to `0..1`, blending `divider_rule` with `divider_glow`.
Labels keep their own colors.

<!-- figure: sweep-normalized -->
```toml
[ui.divider]
sweep = "exp(-((u - pingpong(t / 3, 1)) / 0.1) ** 2)"
```

```{figure} _static/appearance-custom-sweep.svg
:alt: At 0, 1.5, 3, 4.5 and 6 seconds, a glow moves from the left to the center, to the right, then back to the center and left.

Across in three seconds, back in three more.
```

| Change | Effect |
| --- | --- |
| `t / 3` → `t / 6` | Take six seconds to cross instead of three |
| `/ 0.1` → `/ 0.2` | Make the glow wider |
| `pingpong(...)` | Turn around at the ends |

For a fixed speed of 18 columns per second:

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
