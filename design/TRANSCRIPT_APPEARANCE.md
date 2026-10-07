# Transcript appearance

Status: implemented. Transcript rows are expressed as format
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

## Decision: one template per record, a fixed engine for structure

Split what a transcript is into two layers and configure only one of them:

- **Record layer (configurable, completely)** — how one settled tool call is drawn. One
  template per record owns the whole static shape: how many lines it takes, which fields appear
  and where, branching and loops. Same template language as `[statusbar] format`/
  `[divider] format` (`ui.bars.expand`: presets, `{% if %}`, `{% for %}`, `{% optional %}`,
  filters, `[role]` styling fragments), same validator, same preview. Fields are the facts a
  record already has; filters bound them:

  | Field | Filters |
  | --- | --- |
  | `{marker}`, `{tool}`, `{citation}`, `{error}` | `firstline` (error), `lower` |
  | `{args}` | `firstline`, clipping is the preset's business |
  | `{output}` | `tail:N`, `head:N` — this is how truncation is controlled |
  | `{elapsed}` | `duration` (`0.4s`) or raw seconds |
  | `{exit}`, `{elided}` | — |

  The template renders any number of lines:

  ```toml
  [transcript]
  format = """
  {marker} {tool} {args}
  {% if failed %}error: {error|firstline}{% endif %}
  {% for line in output|tail:3 %}{line}{% endfor %}
  {% if elided %}… +{elided} more lines · {citation}{% endif %}
  """
  ```

  One template per record, not one per row: `preset:minimal` is simply the template
  `"{marker} {tool|lower} {args}"` (a record is one line; a failed call still emits its error
  first line), and `preset:standard` is today's multi-line rendering expressed as a template.
  "Levels" stop being an enumeration — a user writes the shape they want.
- **Structure layer (host-owned, never configurable)** — which records exist, their tree
  nesting, the `├ │ └` edges and rails, role-based coloring, diff/code width-dependent
  rendering, and the stored-result (Ctrl-O) views. Templates produce the record's lines; where
  those lines sit in the block tree is the engine's business.

The completeness requirement: `preset:standard` must reproduce today's rendering byte-for-byte.
The current per-line assembly in `toolblocks.py` is plain string joining over the same facts, so
collecting it into a template makes the default the fixed form of the status quo, not an
approximation — guarded by golden tests against the current transcript output.

```text
preset:minimal, the checklist

  ● bash rg -n export_rows src
  ● read src/db/rows.rs
  ● bash cargo bench export
  ● edit src/db/rows.rs
```

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

- **Record format** — the one template above, as presets (`standard`, `minimal`) or edited
  templates, using the existing format editor (validate on draft, preview, Ctrl-S apply).
- **Thinking display** — model reasoning as collapsed first line, expanded, or hidden.
- **Divider style** — how a run of tool calls is closed before the next model text.

(The earlier knob list — timestamps, marker, truncation width — collapses into the formats:
`{elapsed}` present or absent, `{marker}` present or absent, and the preview's line budget is
part of the standard preset's frame, not a separate setting.)

Choosing persists immediately and applies to the next rendered line.

Formats are copyable like every bar format: the panel's existing `c` (copy the format string)
and `t` (copy a paste-ready `[transcript]` TOML snippet, serialized with tomlkit) apply to the
transcript sites unchanged. A chosen preset copies out as its one-line TOML entry, so sharing a
look means pasting one snippet into another machine's config — the same flow bar formats have
today (`ui.cli.formats`).

## Storage: sparse exceptions, same as tool visibility

Global formats persist through the same preferences the `/theme` panel already writes, with
live preview on readback. Per-tool format overrides stay in the config file, and unset tools
inherit the global format; no defaults are written to disk:

```toml
[transcript.tool.Bash]   # exception: bash rows as a checklist
format = "preset:minimal"
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
2. `[transcript]` configuration: the one format key, presets, per-tool sparse overrides.
3. Transcript tab in the `/theme` panel: preset picker with live previews, format editing via
   the existing FormatPanel flow.
4. Expose the effective formats in presenter view models.
5. Tests: default-equals-today golden files, boundary rules (citation, failure line, multi-line
   `{output}`), per-tool inheritance, persistence; quality gates; CHANGELOG; docs
   (`docs/appearance.md`, `docs/usage.md`).

## As implemented

All five steps are in, except that presenters do not receive the effective format (step 4 was
built, then removed: no presenter read it, and it tied the presenter API to a config string).
Where the text above differs from what shipped:

- **One format string per row, in the bar language itself.** Each line of the format is one row,
  parsed by the shared language in `wizolt/formats.py` (moved below `ui.bars`, which binds it to
  the status facts) over the record's fields. Conditions are its expressions. A row that renders
  empty is left out, and one reserved row form -- a line of just `{output}`, `{output|tail:N}` or
  `{output|head:N}` -- stands for the output lines, so there are no loops or filters: an earlier
  dedicated record parser with both was replaced after it produced most of this feature's bugs.
  Rows refuse `[role]` styles, fills, joins and optional spans: a record's colors are host-owned
  roles, and its rows are not width-driven.
- **`preset:standard` is the builtin assembly, not a format.** The completeness requirement above
  is met by construction rather than by a golden-equal template: standard never reaches the
  engine, has no rows to show or edit, and the `/theme` sample settles its call through
  `finish_display` itself, so every preset previews exactly what the transcript prints.
- **Engine rows are computed once.** The rows a call carries under any shape (MCP summary,
  ToolScript envelope, Ask answer, vision trace) come from one function used by the builtin path
  and the format path alike.
- **`thinking` and `close` are keys of the same table** rather than separate settings, and the tab
  lists all three of its settings as one grouped list (`Space` chooses, `Tab` jumps groups, `f`
  edits the format row): the two look choices change what the same stream prints.
- **An unknown fact renders as nothing, not as a zero**, and a format spec on one falls back to
  the standard rendering for that record.
- **Performance.** The record path is per tool call: formats are parsed once per source (failures
  included), output is split only for a format that shows it, and rows skip the bar layout. See
  [the benchmark README](../benchmarks/README.md#transcript-records-branch-review).
