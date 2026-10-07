# Appearance

Make wizolt feel at home in your terminal. Run **`/theme`** to try colors, statusbars,
animated dividers and input symbols in one place.

For your own colors, define `[ui.themes.my-theme]` in your config or keep a separate theme file.
See the [copyable examples](appearance-reference.md#define-a-theme-in-your-config).

```{figure} _static/appearance-picker.svg
:alt: The appearance picker with slate selected, spaced navigation and a bordered panel showing five labeled color samples.

One picker for the whole look.
```

**← / →** or **h / l** switches tabs. **↑ / ↓** moves through the choices.
The screen previews each choice as you move. **Enter** saves everything you changed;
**Esc** cancels. Use **/** to search a long list.
The line beneath the shortcuts points to your config file for fuller customization.
Roomy panes separate the choices from a bordered sample panel. Small panes use a compact
preview so more choices remain visible.

## Color themes

Choose a familiar classic or a newer favorite in the **Colorscheme** tab.
Each theme colors code, menus and diffs together.
The labeled samples show code, removed and added lines, your messages, replies, tool calls,
result colors and a selected menu item. The preview heading says how many of the eight types
are shown. Small panes show fewer samples to leave room for choices; enlarge the pane to
see them all. The other tabs preview diff details, the actual statusbar, idle/running/queued
dividers and ordinary/follow-up input symbols.

```{figure} _static/appearance-colors.svg
:alt: The same Python function in dark, slate, forest, sand, plum and dracula.

Six themes, the same code. Preview the others in /theme.
```

| Feel | Themes |
| --- | --- |
| Cool blue and cyan | `slate` |
| Leaf green | `forest` |
| Amber and warm brown | `sand` |
| Pink and lavender | `plum` |
| Quiet ink on light gray | `paper` |
| Retro orange and olive | `gruvbox-dark` |
| Cyan and ochre | `solarized-dark` |
| Bright pink, purple and green | `dracula` |
| PaperColor's crisp, colorful syntax | `papercolor-light`, `papercolor-dark` |
| Your terminal's own colors | `auto`, `dark`, `light` |

The default is `auto`. Named themes change wizolt's colors, so choose one that suits your
terminal's background. Your terminal keeps its own background.

`papercolor` follows your terminal between its light and dark variants. Unknown or removed
theme names fall back to `dark`; no old-name mapping is applied. On a light terminal,
select `/theme light` or `/theme auto` if your previous theme is no longer available.

Classic palettes come from [Gruvbox](https://github.com/morhetz/gruvbox),
[Solarized](https://ethanschoonover.com/solarized/),
[Dracula](https://spec.draculatheme.com/) and
[PaperColor](https://github.com/NLKNguyen/papercolor-theme).

Pick directly with `/theme slate`. Choices are saved for your next session.

## Diff colors

Leave **Diff** on **`auto`** to match your theme, or choose another set in that tab.
The preview shows added and removed lines, with changed words highlighted more strongly.

```{figure} _static/appearance-diff.svg
:alt: A Python diff with a removed line, an added line and stronger highlighting on the changed words.

See what changed at a glance.
```

Try `classic` for bright red and green, `delta` for deeper colors, or `zebra` for softer ones.
These are the three diff presets. `auto` uses `classic` for every built-in theme;
your custom theme can override its diff backgrounds.

## Statusbar and divider

The **StatusBar** tab changes the bottom row. Try a compact layout for fewer details,
or a fuller one for tools, context and cache usage. Choose **Layout** and **Colorscheme**
separately; **Space** chooses the highlighted value and **Tab** jumps between groups.
Every layout starts with its details together on the left; press **p** to move context and
cache usage to the right edge (**Left / Right**) and back (**All left**). The placement stays
as you browse and choose other layouts.

```{figure} _static/appearance-statusbars.svg
:alt: All ten statusbar layouts, showing the same agent, model and usage with different grouping and color segments.

The same session, ten layouts.
```

`default` keeps its plain, transparent text and familiar information order.
`powerline` connects separate agent, model, provider, effort, YOLO and
group-count segments with arrows. `lualine` adds a full background band and tool/cache
segments. Both arrow layouts need a Nerd Font, and their arrows follow the segments to either edge.

For other shapes, try `blocks` for adjoining rectangles, `vim` for a colored agent tab, `split`
for a context meter across a tinted row, or `monitor` for a fuller dashboard. `brackets` uses
colored outlines; `compact` highlights the model; `minimal` pairs it with a small meter.
The colors follow your chosen theme, with separate text and segment colors.

Every preset names the selected agent and shows YOLO when enabled, and carries the
connected MCP and skill counts. Narrow rows drop optional details, the counts first;
every layout keeps the agent and model ahead of usage.
Context usage turns yellow at 70% and red at 90%.

The placement is saved in `ui.statusbar.format`: **All left** as the preset's name, **Left /
Right** as the preset's full format. A format you wrote yourself shows **custom format** instead,
and moves only when you edit it.

The **Divider** tab changes the line above your input. Choose a **Layout**, **Sweep** for
its animation, and **Colorscheme** for its colors. **Space** chooses the highlighted value;
**Tab** jumps between groups. A star marks each chosen value. Browsing previews leaves
your confirmed choices in place; **Enter** saves those choices.
Every layout shows a green breathing dot during model requests. The running and queued
samples animate that dot too, even with **Sweep: none**; the idle sample has no dot.

Both bars default to **inherit**, following the main Colorscheme. Choose a theme to give
either bar its own colors, or **auto** to follow your terminal's light or dark background
independently. Custom themes are available here too.

```{figure} _static/appearance-dividers.svg
:alt: The comet, frame and rail dividers showing a green activity dot, elapsed time and output speed.

A quiet line, a frame, or activity and speed.
```

| Animation | Look |
| --- | --- |
| `comet` | A bright head with a fading tail |
| `ripple` | Pulses spreading from the center |
| `aurora` | Slow, overlapping bands of light |
| `none` | A still line |

The picker animates idle, running and queued examples without sending a model request.
The rounded `capsule` layout and arrow-shaped `powerline` layout need a Nerd Font.

### View, copy and edit a format

On the StatusBar or Divider tab, press **f** to see the format that will be saved and, for a
preset, the full format it stands for. On the Divider tab's **Sweep** rows, **f** opens the
sweep's formula instead. **c** copies the value and **t** copies it as a `[ui.statusbar]` or
`[ui.divider]` block for your config file. Copying uses `pbcopy`,
`wl-copy`, `xclip`, `xsel` or `clip.exe`; without one, wizolt says so and leaves the text on
screen to select.

Press **e** to edit. The bar previews each valid change with your theme (a sweep animates in
the divider samples), and an invalid one shows the problem without touching the preview. **Ctrl-S** keeps the edit
and **Esc** discards it; **Enter** in the picker then saves it like any other choice. The
[template reference](appearance-reference.md#custom-templates) lists every field and style.

## Input symbols

The **Input** tab changes the symbol before your typing. Choose `>` (default), `❯`, `→`, `λ`
or `•`. The preview shows both ordinary input and a follow-up while the agent is working.

```{figure} _static/appearance-input.svg
:alt: The Input tab with five symbol presets and a custom choice, showing a chevron before ordinary input and a plus-chevron before a follow-up.

A familiar prompt, or your own.
```

Press **e** to edit your own prefixes. **Tab** switches between ordinary input and follow-ups;
**Ctrl-U** clears a field. Include a trailing space if you want room after the symbol.
**Enter** applies your edit and returns to the list; **Enter** in the list saves. **Esc** in
the editor discards that edit; **Esc** in the list cancels all changes.

```{figure} _static/appearance-input-editor.svg
:alt: The input-prefix editor with separate Chat and Running fields and a preview beneath them.

Edit each prefix separately, then return to the list to save.
```

## Make it yours

You can also choose without opening the picker:

```text
/theme slate
/theme diff zebra
/theme statusbar vim
/theme statusbar theme sand
/theme divider frame
/theme divider theme forest
/theme sweep aurora
/theme input chevron
```

For your own colors, statusbar backgrounds, layouts or animations, see
[Custom appearance](appearance-reference.md).

If colors look wrong and wizolt warns about true color, add `export COLORTERM=truecolor`
to your shell profile. `NO_COLOR=1` turns colors off.

## Transcript

Three settings share the **Transcript** tab. **Space** chooses the highlighted value, **Tab**
jumps between settings, and **Enter** saves; each choice is previewed as you move.

**Record format** decides how a finished tool call is written. Choose `standard` (the default)
for a call line, a short preview of its output and a `tr.N` reference that **Ctrl-O** expands, or
`minimal` for a one-line checklist row per call. Saving a format redraws the calls already on
screen in it, except calls a plugin reshaped.

```text
standard

  ● Bash  rg -n export_rows src
    src/jobs/export.rs:42:  export_rows(&pool, &cfg);
    … +2 more lines · Ctrl-O for more

minimal

  ● bash rg -n export_rows src
  ● read src/db/rows.rs
```

**Thinking display** decides how the model's reasoning reads while it arrives: `expanded` keeps
the newest lines (the default), `collapsed` keeps its opening line only, and `hidden` shows
nothing -- the divider below still says `thinking`.

**Tool-run divider** decides what closes a long run of tool calls the agent never talks over: a
full-width line (the default), a blank line, or nothing. Saving it redraws the dividers already on
screen, in a resumed session too.

Press **f** on a record format to see what it stands for and **e** to write your own. Each line of a
format is one row, written like a status bar format: fields such as `{tool}`, `{args}`,
`{duration}`, `{citation}` and `{elided}`, and `{% if %}` blocks. A line that is just
`{output|tail:3}` shows the call's last three output lines; a row that comes out empty is left out.
Rows wizolt owns -- a diff, an approval card, a failed call's error line, the stored result's
reference -- are always drawn, whatever the format says. **c** copies the format and **t** copies
it as a `[transcript]` block for your config file.

```toml
[transcript]
format = "preset:minimal"
thinking = "collapsed"
close = "blank"

[transcript.tool.Bash]
format = """{tool} {args}
{output|tail:5}"""
```

An unset key keeps the standard rendering, and an unset tool keeps the table's format, so a
per-tool line is only needed for the tools you want different. The
[template reference](appearance-reference.md#custom-templates) lists every field.
