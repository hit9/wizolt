# Appearance

Make wizolt feel at home in your terminal. Run **`/theme`** to try colors, statusbars,
animated dividers and input symbols in one place.

For your own colors, define `[ui.themes.my-theme]` in your config or keep a separate theme file.
See the [copyable examples](appearance-reference.md#define-a-theme-in-your-config).

```{figure} _static/appearance-picker.svg
:alt: The appearance picker with gruvbox-dark selected and labeled samples of code, removed and added lines, user messages, replies, tool calls, results and menu selection colors.

One picker for the whole look.
```

**← / →** or **h / l** switches tabs. **↑ / ↓** moves through the choices.
The screen previews each choice as you move. **Enter** saves everything you changed;
**Esc** cancels. Use **/** to search a long list.
The line beneath the shortcuts points to your config file for fuller customization.

## Color themes

Choose a familiar classic or a newer favorite in the **Colorscheme** tab.
Each theme colors code, menus and diffs together.
The labeled samples show code, removed and added lines, your messages, replies, tool calls,
result colors and a selected menu item. The preview heading says how many of the eight types
are shown. Small panes show fewer samples to leave room for choices; enlarge the pane to
see them all. The other tabs preview diff details, the actual statusbar, idle/running/queued
dividers and ordinary/follow-up input symbols.

```{figure} _static/appearance-colors.svg
:alt: The same Python function in gruvbox-dark, one-dark, desert, tokyonight, catppuccin-dark and kanagawa.

Six themes, the same code. Preview the others in /theme.
```

| Feel | Themes |
| --- | --- |
| Warm and retro | `gruvbox-dark`, `gruvbox-light`, `desert` |
| Quiet and earthy | `zenburn`, `kanagawa`, `everforest` |
| Bright on dark | `jellybeans`, `one-dark`, `tokyonight` |
| Soft pastels | `catppuccin-dark`, `catppuccin-light`, `rose-pine-dark`, `rose-pine-light` |
| Your terminal's own colors | `auto`, `dark`, `light` |

The default is `auto`. Named themes change wizolt's colors, so choose one that suits your
terminal's background. Your terminal keeps its own background.

Pick directly with `/theme gruvbox-dark`. Choices are saved for your next session.

## Diff colors

Leave **Diff** on **`auto`** to match your theme, or choose another set in that tab.
The preview shows added and removed lines, with changed words highlighted more strongly.

```{figure} _static/appearance-diff.svg
:alt: A Python diff with a removed line, an added line and stronger highlighting on the changed words.

See what changed at a glance.
```

Try `classic` for bright red and green, `delta` for deeper colors, or `zebra` for softer ones.
Tokyo Night uses teal for additions; Rosé Pine uses blue.

## Statusbar and divider

The **StatusBar** tab changes the bottom row. Try a compact layout for fewer details,
or a fuller one for tools, context and cache usage.

```{figure} _static/appearance-statusbars.svg
:alt: Five statusbar layouts: default, minimal, split, blocks and vim, showing the same model and usage.

The same session, five layouts.
```

`default` stays transparent. `vim`, `split` and `monitor` have a background band.
`powerline` and `lualine` use arrow-shaped color segments and need a Nerd Font.

The **Divider** tab changes the line above your input. Choose a **Layout**, then scroll down
to **Sweep** for its animation. **Space** chooses the highlighted value; **Tab** jumps between
the two groups. A star marks each chosen value. Browsing animations keeps your chosen layout.

```{figure} _static/appearance-dividers.svg
:alt: The comet, frame and rail dividers showing a running task with elapsed time and output speed.

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
/theme gruvbox-dark
/theme diff zebra
/theme statusbar vim
/theme divider frame
/theme sweep aurora
/theme input chevron
```

For your own colors, statusbar backgrounds, layouts or animations, see
[Custom appearance](appearance-reference.md).

If colors look wrong and wizolt warns about true color, add `export COLORTERM=truecolor`
to your shell profile. `NO_COLOR=1` turns colors off.
