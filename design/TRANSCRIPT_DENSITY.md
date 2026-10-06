# Transcript density

Status: design agreed with the user; not implemented. Host-owned display density for the
conversation transcript, with the existing presenter seam unchanged above it. Storage follows
the same sparse-exceptions principle as [tools and prompts](TOOLS_AND_PROMPTS.md).

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

Why host and not plugin:

- Density changes how much information a user sees by default. That is core experience, not an
  add-on; a user who wants fewer lines should not have to install anything.
- The presenter seam is one-renderer-per-site by design. A "simplicity" plugin would take the
  whole `tool.result` site and exclude every other presenter. Density is a floor shared by all
  rendering; presenters are deep customization on top.
- Nothing in density is conversation-dependent: the set of visible information changes only
  through explicit user action, so it costs nothing at the model-request boundary (display only).

Why not a whole `/theme` identity: density changes information content, not styling. Colors,
borders and bar formats can change without touching what is shown; "should tool output be
visible" must survive a theme switch. So the setting is separate from theme data — but its
picker is visual and preview-driven, which is exactly what the `/theme` panel already is; the
selection UI therefore joins that panel as a Transcript tab (see Interaction below).

## Storage: sparse exceptions, same as tool visibility

A global level plus per-tool overrides; unset tools inherit the global level. No defaults are
written to disk:

```toml
[transcript]
density = "compact"

[transcript.tool.Bash]   # exception: bash output in full
density = "full"
```

- A new builtin tool in a release is unset for every existing user and inherits the global
  level; no migration.
- Removing an override means deleting the table; the tool returns to the global level with no
  residue.
- Keys are tool names as the model sees them (`Bash`, `Read`, MCP wire names).

## Interaction

Selection lives as a **Transcript tab in the `/theme` panel**, with a live preview: each level
is shown by rendering the same sample tool-call card and result at that density, so the user
picks by looking, not by imagining what `compact` means. Choosing persists immediately. This
follows the existing pattern — the panel is already a tabbed appearance selector (StatusBar tab).

The concept stays separate from themes even though the picker is shared: density is stored as
`[transcript]`, not as theme data. Switching a theme never changes what information the
transcript shows; changing density never touches colors or borders. The picker is visual and
preview-driven like a theme — that is why it lives in that panel — but the setting itself is
information content.

A per-tool override is edited in the config file (the full-capability fallback, as with every
sparse setting). The panel is the discoverable surface, the file is the escape hatch.

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
