---
orphan: true
---

# Drawing documentation figures

`render.py` builds the SVG illustrations in these guides. Only complete UI previews drawn by
wizolt's picker renderer use a terminal window frame. Composed examples and side-by-side
comparisons use a plain “Rendered UI sample” panel. Concept diagrams sit directly on the page:
transparent background, serif type, fine connecting lines and the documentation's muted colors.
Avoid filled cards, badges, large repeated titles and window decorations.
Rebuilding unchanged figures produces identical files.

## Run it

From the repository root, with the project's development dependencies available:

```sh
# Rebuild all figures in docs/_static.
uv run python docs/tools/render.py

# List figure names.
uv run python docs/tools/render.py --list

# Rebuild one figure; repeat --figure to select several.
uv run python docs/tools/render.py --figure appearance-custom-statusbar

# Preview another theme without replacing published images.
uv run python docs/tools/render.py --theme slate --output-dir /tmp/wizolt-figures

# Build the documentation.
uv run make -C docs html
```

The script uses the project's Rich and prompt_toolkit dependencies. It uses fixed example
text and a temporary session, without contacting a provider or reading your personal config.
The clock, terminal size and SVG identifiers are fixed.

## Compare color presets

`theme_preview.py` builds a standalone HTML comparison using the same message, diff, menu,
statusbar and divider renderers. Open the generated file in your browser:

```sh
uv run --no-sync python docs/tools/theme_preview.py
# Default output: theme-preview.html in the repository root.

uv run --no-sync python docs/tools/theme_preview.py --output /tmp/theme-preview.html
```

The page compares the built-in palettes against default `dark` or `light`. Switch the
statusbar and divider selectors, then mark a favorite. Copy the configuration shown below
into your config. Every sample uses the existing `classic` diff colors.
The generated `theme-preview.html` is a local preview, ignored by Git; commit the script instead.

Preset names and captions live in `CANDIDATES`; colors come directly from wizolt.
Regenerate the HTML after changing palettes or layout. The page uses English text and
monospace fonts throughout. Messages have no shaded padding; input has shaded rows above and below its text
and a plain gap before the statusbar.

The terminal samples have no window decorations: they are assembled rendered examples,
not session screenshots. SVG preserves full-row backgrounds and Chinese character widths;
powerline joins use vector paths, so the comparison needs no Nerd Font or online assets.

## Figure names

| Guide | Names |
| --- | --- |
| Appearance | `appearance-picker`, `appearance-colors`, `appearance-diff`, `appearance-statusbars`, `appearance-dividers`, `appearance-input`, `appearance-input-editor` |
| Customization | `appearance-custom-theme`, `appearance-custom-statusbar`, `appearance-custom-width`, `appearance-custom-divider`, `appearance-custom-sweep` |
| Getting started | `getting-started-turn` |
| Interaction | `usage-followups` |
| Context | `context-compaction`, `context-cache` |
| Workers | `worker-handoff` |
| Skills | `skills-workflow` |
| Hooks | `hooks-workflow` |

## Change an example

Customization figures read TOML directly from
[`appearance-reference.md`](../appearance-reference.md). A comment identifies each example:

````markdown
<!-- figure: statusbar-model -->
```toml
[ui.statusbar]
format = "[status_provider]{model}[/]"
```
````

Edit that block, then redraw `appearance-custom-statusbar`. The code readers copy and the
picture stay together. Invalid TOML or layout syntax stops generation with an error.

Other sample messages and diagram labels live in the corresponding `Illustrations` method
in `render.py`. These are illustrations, not recordings of a real model run.

## Add a figure

1. Add a method to `Illustrations`. For terminal UI, use `message`, `log`, `styled` or `bar`
   and finish with `save("your-figure", rows)`. For a conceptual flow, use
   `diagram(name, title, steps, note)`, where each step is a `(label, detail)` pair.
   Do not dress a workflow or explanation as an application screenshot.
2. Add its name and method to `RECIPES`. For a configuration example, add a marked TOML block
   in the reference and read it with `example("your-example")`.
3. Run `--figure your-figure` and embed `_static/your-figure.svg` with a MyST `figure` directive.
   Give it descriptive alt text and a short caption.
4. Build HTML and check desktop and narrow windows. Keep text short enough to fit the figure.

Commit the example, script and SVG together. Redraw affected figures when the UI, colors or
example settings change.
