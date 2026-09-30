# Appearance

Make wizolt feel at home in your terminal. Run **`/theme`** to try colors, statusbars and
animated dividers in one place.

```{figure} _static/appearance-picker.svg
:alt: The appearance picker with Colorscheme, Diff, StatusBar and Divider tabs, gruvbox-dark selected, and a preview of code and diff colors.

One picker for the whole look.
```

**← / →** or **h / l** switches tabs. **↑ / ↓** moves through the choices.
The screen previews each choice as you move. **Enter** saves everything you changed;
**Esc** cancels. Use **/** to search a long list.

## Color themes

Choose a familiar classic or a newer favorite in the **Colorscheme** tab.
Each theme colors code, menus and diffs together.

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

## Make it yours

You can also choose without opening the picker:

```text
/theme gruvbox-dark
/theme diff zebra
/theme statusbar vim
/theme divider frame
/theme sweep aurora
```

For your own colors, statusbar backgrounds, layouts or animations, see
[Custom appearance](appearance-reference.md).

If colors look wrong and wizolt warns about true color, add `export COLORTERM=truecolor`
to your shell profile. `NO_COLOR=1` turns colors off.
