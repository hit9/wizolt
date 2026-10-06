# Transcript appearance

Status: design agreed with the user; not implemented. Transcript rows are expressed as format
strings — the same template language the status bar and divider already use — over a host-owned
block structure. Density levels become presets of that one mechanism, and the existing presenter
seam stays unchanged above it. Storage follows the same sparse-exceptions principle as
[tools and prompts](TOOLS_AND_PROMPTS.md).

## The problem

Users differ in how much of a turn they want to see: one wants today's call lines and result
previews, another wants the turn as a one-line checklist (call name or command, a status marker,
nothing else). Today the only lever is the presenter seam — which means writing or installing a
plugin to see less, and the density plugin then monopolizes a presenter site (presenters do not
compose: one renderer per site), crowding out any other customization. Every other wish (move
the elapsed time, drop the exit code) has no lever at all: it needs a host switch per knob, and
the knobs multiply.

## Decision: format strings for rows, a fixed engine for structure

Split what a transcript is into two layers and configure only one of them:

- **Row layer (configurable)** — how each line's facts are joined. Format strings over the
  facts a row already has: `{marker}`, `{tool}`, `{args}`, `{output}`, `{elapsed}`, `{exit}`,
  `{citation}`, `{elided}`. Same template language as `[statusbar] format`/`[divider] format`
  (`ui.bars.expand`: presets, `{% optional %}`, filters, `[role]` styling fragments), same
  validator, same preview.
- **Structure layer (host-owned, never configurable)** — which rows exist, their tree nesting,
  the `├ │ └` edges and rails, role-based coloring, diff/code width-dependent rendering, and the
  stored-result (Ctrl-O) views. A template renders one line of text; where that line sits in the
  block tree is the engine's business.

Three sites get format strings:

```toml
[transcript]
call_format    = "preset:standard"      # or an explicit template
closing_format = "… +{elided} more lines · {citation}"
# result rows (output, answer, summary) render the value itself; their
# frame is the closing row, so one format covers both
```

`preset:standard` is today's rendering, byte-for-byte: the current per-line assembly in
`toolblocks.py` is plain string joining, so collecting those joins into templates makes the
defaults the fixed form of the status quo, not an approximation of it. That is the completeness
requirement — the default must reproduce the current transcript exactly.

`preset:minimal` is the checklist:

```toml
[transcript]
call_format = "{marker} {tool|lower} {args}"
```

```text
  ● bash rg -n export_rows src
  ● read src/db/rows.rs
  ● bash cargo bench export
  ● edit src/db/rows.rs
```

A result row at `preset:minimal` is nothing (the call row is all that is written; a failed call
still surfaces its error first line). With formats, "levels" stop being an enumeration: a user
moves `{elapsed}` to the end, deletes `{exit}`, or writes a narrower row — without a host switch
per wish. The old two-level design (standard/minimal) is exactly the two presets above.

## The status marker

`{marker}` is a template field, not a separate setting; leaving it out of the format is
"no marker". Its meaning is fixed, because a one-line row may leave it as the only channel:

| Shape | Meaning |
| --- | --- |
| `○` hollow | running |
| `●` filled | completed successfully |
| `●` in the failure color, with the error's first line | failed |

Color is not configurable: the marker uses the theme's existing success/danger roles, so a
theme change recolors it automatically and no orphan color can appear.

## Boundaries the templates may not cross

- The citation `{citation}` may not be dropped from a row that shows output: a viewer must
  always be able to reach the stored result.
- A failed call keeps its error first line even under the narrowest template.
- Multi-line bodies (diffs, code, full output) enter as one `{output}` value rendered by the
  engine; templates never split or re-indent them.
- Fields are facts the host already computed; templates cannot invent new facts or call code.

Why host and not plugin: this is core experience — a user who wants fewer lines should not have
to install anything — and the presenter seam stays the deep customization layer (one renderer
per site, chosen per site). Formats are the floor shared by all rendering; presenters sit above.

`/theme` placement: the interaction is visual and preview-driven, exactly what the `/theme`
panel already is. The Transcript tab picks presets with a live preview per option, and edits
formats through the same draft/validate/preview flow the status bar's FormatPanel already has
(`ui.cli.formats`): pick, edit, validate, apply — no restart, no config editing.

## Interaction: a Transcript tab in the `/theme` panel

The `/theme` panel owns this configuration. Its Transcript tab shows, with live previews built
from the same sample turn:

- **Row formats** — the three sites above, as presets (`standard`, `minimal`) or edited
  templates, using the existing format editor (validate on draft, preview, Ctrl-S apply).
- **Thinking display** — model reasoning as collapsed first line, expanded, or hidden.
- **Divider style** — how a run of tool calls is closed before the next model text.

(The earlier knob list — timestamps, marker, truncation width — collapses into the formats:
`{elapsed}` present or absent, `{marker}` present or absent, and the preview's line budget is
part of the standard preset's frame, not a separate setting.)

Choosing persists immediately and applies to the next rendered line.

## Storage: sparse exceptions, same as tool visibility

Global formats persist through the same preferences the `/theme` panel already writes, with
live preview on readback. Per-tool format overrides stay in the config file, and unset tools
inherit the global format; no defaults are written to disk:

```toml
[transcript.tool.Bash]   # exception: bash rows as a checklist
call_format = "preset:minimal"
```

- A new builtin tool in a release is unset for every existing user and inherits the global
  format; no migration.
- Removing an override means deleting the table; the tool returns to the global format with no
  residue.
- Keys are tool names as the model sees them (`Bash`, `Read`, MCP wire names).
- The panel is the discoverable surface for the global choices; the file is the escape hatch for
  per-tool exceptions.

## Relationship with presenters

The presenter seam is unchanged and stays the deeper layer:

- A presenter that wins a site renders it; the builtin formats do not apply to that site.
- View models carry the effective formats so a presenter can default to matching the user's
  chosen rows — recommended, not enforced.
- On presenter conflict, failure or a missed deadline the builtin rendering (and with it the
  formats) is the fallback. Formats therefore always have an effect, even with plugins
  installed.

## Order of work

1. Extract the per-row string assembly in `toolblocks.py` into format templates whose defaults
   reproduce today's rendering exactly (golden tests against the current transcript output).
2. `[transcript]` configuration: the three sites, presets, per-tool sparse overrides.
3. Transcript tab in the `/theme` panel: preset picker with live previews, format editing via
   the existing FormatPanel flow.
4. Expose the effective formats in presenter view models.
5. Tests: default-equals-today golden files, boundary rules (citation, failure line, multi-line
   `{output}`), per-tool inheritance, persistence; quality gates; CHANGELOG; docs
   (`docs/appearance.md`, `docs/usage.md`).
