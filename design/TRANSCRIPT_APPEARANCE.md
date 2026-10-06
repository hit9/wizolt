# Transcript appearance

Status: design agreed with the user; not implemented. Host-owned display settings for the whole
conversation transcript (density, status marker, and the other knobs below), with the existing
presenter seam unchanged above it. Storage follows the same sparse-exceptions principle as
[tools and prompts](TOOLS_AND_PROMPTS.md).

## The problem

Users differ in how much of a turn they want to see: one wants full tool-call cards and complete
results, another wants the turn as a one-line checklist (call name or command, a status marker,
nothing else). Today the only lever is the presenter seam — which means writing or installing a
plugin to see less, and the density plugin then monopolizes a presenter site (presenters do not
compose: one renderer per site), crowding out any other customization. There is no builtin
control at all.

## Decision: density is a host setting, not a plugin and not a theme

Three levels, applied to the builtin rendering:

| Density | `tool.call` | `tool.result` |
| --- | --- | --- |
| `full` | Complete card: parameters, edited paths, diffs | Full output |
| `compact` | One-line call | Tail-truncated summary (the shape resume previews already use) |
| `minimal` | Status marker + one line (a checklist row) | Not shown; failures still surface |

The same turn at each level (illustrative, not pixel-exact):

```text
full

  ● Bash  rg -n export_rows src
    command: rg -n export_rows src
    cwd: /srv/app
  ─────────────────────────────────────────────
  src/jobs/export.rs:42:  export_rows(&pool, &cfg);
  src/db/rows.rs:118:     fn export_rows(
  ─────────────────────────────────────────────
  exit 0 · 0.4s

  ● Read  src/db/rows.rs
    1  fn export_rows(pool: &Pool, cfg: &Cfg) -> Result<Vec<Row>> {
    2      let mut rows = Vec::new();
    3      for chunk in pool.fetch_chunks(cfg.query())? {
    ...

compact

  ● Bash  rg -n export_rows src
    src/jobs/export.rs:42:  export_rows(&pool, &cfg);
    ... 2 more matches · exit 0 · 0.4s
  ● Read  src/db/rows.rs
    fn export_rows(pool: &Pool, cfg: &Cfg) -> Result<Vec<Row>> {
    ... · 312 lines

minimal

  ● bash rg -n export_rows src
  ● read src/db/rows.rs
  ● bash cargo bench export
  ● edit src/db/rows.rs
```

At `minimal` the checklist row is all that is written for both the call and the result; a failed
call still surfaces its row with a failure marker and the error's first line.

## The status marker

The leading marker is the row's execution state; its meaning is fixed and not configurable,
because at `minimal` it is the only channel left:

| Shape | Meaning |
| --- | --- |
| `○` hollow | running |
| `●` filled | completed successfully |
| `●` in the failure color, with the error's first line | failed |

Color is not a separate setting: the marker uses the theme's existing success/danger roles, so
a theme change recolors it automatically and no orphan color can appear. Whether it is shown at
all is a setting, default on:

```toml
[transcript]
marker = "state"   # default: ○ running / ● done
# marker = "none"  # plain rows, no marker
```

At `compact` and `full` the marker is a visual anchor only (elapsed time, exit codes and output
are already shown), so turning it off costs nothing. At `minimal` with `marker = "none"` the
user opts into plain text deliberately — failure still surfaces its error line, because failure
visibility belongs to the density level, not to this switch.

Why host and not plugin:

- Density changes how much information a user sees by default. That is core experience, not an
  add-on; a user who wants fewer lines should not have to install anything.
- The presenter seam is one-renderer-per-site by design. A "simplicity" plugin would take the
  whole `tool.result` site and exclude every other presenter. Density is a floor shared by all
  rendering; presenters are deep customization on top.
- Nothing in density is conversation-dependent: the set of visible information changes only
  through explicit user action, so it costs nothing at the model-request boundary (display only).

`/theme` placement: density changes information content, not styling — but its whole
interaction is visual (pick a level by looking, recolor with the theme, toggle a marker), which
is exactly what the `/theme` panel already is. It is therefore a first-class **Transcript tab**
in `/theme`: one surface with a live preview per option, persisting and applying immediately,
next to the theme's other tabs. The only part kept out of the panel is the per-tool override
table, which stays a config-file escape hatch.

## Interaction: a Transcript tab in the `/theme` panel

The `/theme` panel owns this configuration. Its Transcript tab covers the whole transcript's
presentation, each option shown with a live preview built from the same sample turn:

- **Density** — the three levels above: how much of a tool call and its result is shown.
- **Status marker** — `state` (○ running / ● done / failure color) or `none`.
- **Timestamps** — show per-row elapsed time (`0.4s`) on call rows, or omit it.
- **Result truncation width** — how many lines a tool result may occupy before the tail
  summary (a number, previewed at the chosen value).
- **Thinking display** — model reasoning as collapsed first line, expanded, or hidden.
- **Divider style** — how a run of tool calls is closed before the next model text.

Choosing an option persists immediately and applies to the next rendered line — no restart, no
config editing. This follows the panel's existing pattern (tabs such as StatusBar): a visual,
preview-driven selector is the right surface for visual settings, and the panel is already
where users look for how wizolt appears.

## Storage: sparse exceptions, same as tool visibility

The tab's global choices (density, marker) persist through the same preferences the `/theme`
panel already writes, with live preview on readback. Per-tool density overrides stay in the
config file, and unset tools inherit the global level; no defaults are written to disk:

```toml
[transcript.tool.Bash]   # exception: bash output in full
density = "full"
```

- A new builtin tool in a release is unset for every existing user and inherits the global
  level; no migration.
- Removing an override means deleting the table; the tool returns to the global level with no
  residue.
- Keys are tool names as the model sees them (`Bash`, `Read`, MCP wire names).
- The panel is the discoverable surface for the global choices; the file is the escape hatch for
  per-tool exceptions.

## Relationship with presenters

The presenter seam is unchanged and stays the deeper layer:

- A presenter that wins a site renders it; the builtin density does not apply to that site.
- View models carry the effective density so a presenter can default to matching the user's
  chosen level — recommended, not enforced.
- On presenter conflict, failure or a missed deadline the builtin rendering (and with it the
  density level) is the fallback. Density therefore always has an effect, even with plugins
  installed.

## Order of work

1. Density levels in the builtin `tool.call`/`tool.result` rendering, driven by the effective
   (global, per-tool) level.
2. `[transcript]` configuration with sparse per-tool overrides.
3. Transcript tab in the `/theme` panel: one live preview per level, selection persists and
   applies immediately.
4. Expose the effective level in presenter view models.
5. Tests for the three levels (including failure surfacing at `minimal`), persistence, and the
   presenter-priority rule; quality gates; CHANGELOG; docs (`docs/usage.md`, `docs/commands.md`).
