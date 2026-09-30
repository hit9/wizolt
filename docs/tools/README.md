---
orphan: true
---

# Drawing documentation figures

`render.py` builds the SVG illustrations in these guides. Terminal examples use wizolt's own
renderers; context and worker diagrams use the same colors and terminal frame.
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
uv run python docs/tools/render.py --theme one-dark --output-dir /tmp/wizolt-figures

# Build the documentation.
uv run make -C docs html
```

The script uses the project's Rich and prompt_toolkit dependencies. It uses fixed example
text and a temporary session, without contacting a provider or reading your personal config.
The clock, terminal size and SVG identifiers are fixed.

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

1. Add a method to `Illustrations`. Use `message`, `log`, `styled` or `bar` for terminal UI;
   use Rich `Text` for a conceptual diagram. Finish with `save("your-figure", rows)`.
2. Add its name and method to `RECIPES`. For a configuration example, add a marked TOML block
   in the reference and read it with `example("your-example")`.
3. Run `--figure your-figure` and embed `_static/your-figure.svg` with a MyST `figure` directive.
   Give it descriptive alt text and a short caption.
4. Build HTML and check desktop and narrow windows. Keep text short enough to fit the figure.

Commit the example, script and SVG together. Redraw affected figures when the UI, colors or
example settings change.
