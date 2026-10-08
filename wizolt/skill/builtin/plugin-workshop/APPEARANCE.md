# Plugin appearance reference

Read [SDK.md](SDK.md) for registration and callback contracts. Everything needed to write
`plugin.theme`, `plugin.preset`, and themed `Text`/`Line` components is below.

## Theme definition

`plugin.theme(name, definition)` accepts these optional keys; omitted values inherit the base:

| Key | Value |
| --- | --- |
| `base` | Built-in name below; default `dark` |
| `colors` | Mapping from a color role below to a color |
| `diff` | Mapping from a diff key below to a color; `default` is not accepted |
| `highlights` | Mapping from group name to `{fg, bg, bold, italic, underline}`; all keys optional |
| `pygments` | Name of an installed Pygments style, such as `monokai` |

Built-in bases: `dark`, `light`, `slate`, `forest`, `sand`, `plum`, `paper`, `gruvbox-dark`,
`solarized-dark`, `dracula`, `papercolor-light`, `papercolor-dark`. A base cannot be another plugin
theme. Colors accept `#rgb`, `#rrggbb`, `default`, or a terminal color name:
`ansiblack`, `ansired`, `ansigreen`, `ansiyellow`, `ansiblue`, `ansimagenta`, `ansicyan`,
`ansigray`, `ansibrightblack`, `ansibrightred`, `ansibrightgreen`, `ansibrightyellow`,
`ansibrightblue`, `ansibrightmagenta`, `ansibrightcyan`, `ansiwhite`.
`divider_glow` and `divider_rule` require hex colors. Highlight `fg`/`bg` take colors;
attribute values are booleans. Highlight names contain letters, digits, `.`, `_`, or `-`.

## Color roles

These are the complete `colors` keys. Components use `Text("label", "accent")`, not an ANSI
sequence or a highlight specification. Component roles are these names only; attributes and
custom highlight groups belong in bar formats.

| Area | Roles |
| --- | --- |
| General | `text`, `muted`, `subtle`, `accent`, `accent_secondary`, `info`, `rule` |
| Messages | `user`, `user_bg`, `tool`, `success`, `warning`, `error` |
| Syntax | `syntax_assign`, `syntax_string`, `syntax_number`, `syntax_ident`, `syntax_builtin`, `syntax_default` |
| Status text | `status_base`, `status_provider`, `status_model`, `status_reason`, `status_mcp`, `status_services`, `status_context`, `status_cache`, `status_yolo`, `status_agent` |
| Status backgrounds | `status_bg`, `status_provider_bg`, `status_model_bg`, `status_reason_bg`, `status_context_bg`, `status_cache_bg`, `status_yolo_bg`, `status_agent_bg` |
| Divider | `divider_glow`, `divider_rule`, `divider_label` |
| Menus | `selection_bg`, `selection_fg`, `menu_bg`, `menu_muted` |

## Diff keys

`added`, `added_word`, `removed`, `removed_word` color line/word backgrounds.
`gutter`, `header`, `hunk`, `added_sign`, `removed_sign` color their foregrounds.

## Bar highlight groups

All color roles above are available as foreground groups. The following groups also supply
surfaces or derived styles; use them for theme-aware segmentation:

| Area | Groups |
| --- | --- |
| Status segments | `status.agent`, `status.provider`, `status.model`, `status.reason`, `status.yolo`, `status.detail`, `status.cache`, `status.cache.segment`, `status.usage`, `status.context` |
| Status emphasis | `status.warning`, `status.error`, `status.attention`, `status.usage.warning`, `status.usage.error` |
| Status bands | `status.band`, `status.split`, `status.monitor` |
| Divider | `divider.activity`, `divider.metrics`, `divider.badge`, `divider.label`, `spinner` |

Themes can override these groups or define their own in `highlights`. Presets validate against
the current theme, so use built-in groups for presets that must work under every theme. A custom
group works only when the selected theme supplies it. `Text.role` does not accept these groups.

## Format grammar

`plugin.preset(kind, name, source)` uses `kind="statusbar"` or `"divider"`. The saved choice is
`preset:plugins.NAME.CHOICE`. Registration does not select it; use `/theme` to select it.
This is wizolt's small template language, not Jinja; there are no loops, includes or Python access.

| Syntax | Meaning |
| --- | --- |
| `{model}` | Plain-text field substitution; values cannot inject formatting |
| `{elapsed:duration}` | Seconds with `s`; numeric formats also support `:d`, `:.0f` through `:.6f` |
| `[accent bold]text[/]` | Push style, then restore the preceding style |
| `[fg=#fff bg=status_bg italic]text[/]` | Explicit color or color-role lookup |
| `[reset]` | Reset all styles to terminal defaults |
| `{>}` | Flexible spaces; following text moves right |
| `{fill:─}` | Flexible repeated pattern, 1–32 printable single-cell characters |
| `{join:\ue0b0}` / `{join:\ue0b2}` | Join neighboring background colors with Powerline arrows U+E0B0 / U+E0B2; written as Python escapes because the glyphs often render invisibly |
| `{% if running %}yes{% else %}no{% endif %}` | Conditional branch; `else` optional |
| `{% optional priority=10 %} · detail{% endoptional %}` | Remove whole span when narrow; lower priorities disappear first |
| `{{`, `}}`, `[[`, `]]` | Literal delimiters |

Styles accept `bold`, `italic`, `underline`, `reverse`, and their `no` forms (`nobold`, etc.).
Close styles within each conditional/optional span. Put a separator inside the optional span
so it disappears too. Multiple fills share free space. Keep everything on one row; the host
clips overflow. Limits: 8,192 characters, 512 template tokens, 16 nested blocks/styles.

Conditions accept field names, string/number literals, `True`/`False`, parentheses, `and`, `or`, `not`,
comparisons (`== != < <= > >=`), and numeric `+ - * / % **`. Supported functions:
`sin`, `cos`, `exp`, `sqrt`, `abs`, `min`, `max`, `pingpong(value, width)`.
No indexing or arbitrary function calls. Expressions allow 1,024 characters/100 AST nodes;
numeric magnitude is limited to 1e12 and exponent magnitude to 32. Missing plugin fields read as
zero. This syntax describes formats; the plugin API does not register divider sweep formulas.

## Format fields

| Scope | Fields |
| --- | --- |
| Current model | `provider`, `model`, `reasoning`, `context.percent`, `cache.percent` |
| Current agent | `agent.name`, `agent.id`, `agent.state`, `yolo` |
| Agent family | `agents.count`, `agents.running`, `agents.waiting` |
| Services | `mcp.count`, `mcp.label`, `skills.count`, `plugins.count` |
| Activity | `running`, `elapsed`, `rate` |
| Divider labels | `activity`, `spinner`, `label` |
| Divider queue | `queue.total`, `queue.followup`, `queue.next_turn` |

`plugins.count` counts healthy live plugins in this agent. Family counts include main.
`rate` is already formatted text, not a numeric token rate. The complete `label`, `spinner`,
and queue counts are populated only for the divider, empty/zero in the statusbar.
Registered scalar fields add `{plugins.INSTALLED_NAME.FIELD_NAME}`; there is no `{agent}` field.

## Runnable appearance plugin

Save as `appearance_demo.py`; validate, enable/reload, then choose its entries in `/theme`.

```python
SDK_VERSION = 1


def setup(plugin):
    plugin.theme(
        "night",
        {
            "base": "slate",
            "colors": {"accent": "#88aabb", "status_model_bg": "#405366"},
            "diff": {"added": "#163b2c", "added_word": "#235c42"},
            "highlights": {"status.model": {"fg": "#eeeeee", "bg": "#405366", "bold": True}},
        },
    )
    plugin.preset(
        "statusbar",
        "compact",
        "[status.agent] {agent.name} [/]{join:\ue0b0}[status.model] {model} [/]{join:\ue0b0}[reset]{>}[status_context]ctx {context.percent}%[/]",
    )
    plugin.preset(
        "divider", "quiet", "[divider_rule]{fill:─}[/]{% if running %} [spinner]{spinner}[/][divider.label]{label}[/] {% endif %}[divider_rule]{fill:─}[/]"
    )
```

## Source fallback

Run `wizolt plugin paths` and use its absolute `source` directory. Read `ui/bars.py` for the
template parser, `ui/themes.py` for theme compilation, or `ui/render.py` for style resolution.
They explain behavior; import `wizolt.sdk`, not these internals, in the plugin.
