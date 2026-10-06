# Tool visibility and prompt files

Status: design agreed with the user; not implemented. This document records the direction for
the two built-in plugins (`tool_visibility`, `system_prompt`) and the host operations they need.
It follows the interception contract in [Plugin operations](PLUGIN_INTERCEPTION.md); the
[implementation boundaries](PLUGINS.md) still apply.

## The two gaps this closes

1. Tools — a plugin cannot change which tools a request offers the model. It cannot hide a built-in
   tool from a request; it can only refuse a call the model has already spent a round trip on
   (`tool.call`). `model.request` keeps `tools` read-only on purpose: routing already lives there.
2. Prompt — a plugin can replace the `system` block through `context.compose`, but its only surface
   for user-typed text is the in-app `ui.input` box. Replaced instruction text has no file the user
   can edit in the editor they already use.

## Principle: the offered tool set is user data

The tool set a request offers changes only through explicit user action. Enabling or disabling a
plugin never changes it: plugins register tools, but resident admission is the user's choice, made
per tool in the `/tools` panel and persisted in plugin preferences. Plugin authors cannot touch
cache stability.

Cache accounting, one entry per invalidation source, all user-triggered and rare:

| Event | Trigger |
| --- | --- |
| User toggles a tool's visibility or residency in `/tools` | Explicit user action |
| User edits the system prompt file | Explicit user action |
| Normal conversation within unchanged settings | No invalidation |

Nothing drifts with conversation state. That is why residency is a stored user choice, not a
plugin-declared capability and not a runtime decision.

## Storage: sparse exceptions over a default, never a state table

Persisted state is two sets of explicit exceptions:

```
resident: { plugin_name.tool_name }   # schema enters the request directly
hidden:   { tool names }               # removed from the request
```

Every tool not mentioned is unset and keeps its original per-class behavior:

| Class | Unset (default) | resident | hidden |
| --- | --- | --- | --- |
| Built-in tools | Offered | Ignored (already offered) | Removed |
| MCP tools | Offered | Ignored | Removed |
| Plugin tools | `Plugin` gateway (progressive disclosure) | Schema offered directly | Neither offered nor listed by the gateway |

Consequences:

- No defaults are written to disk. A new built-in tool in a wizolt release is unset for every
  existing user and therefore offered; no migration.
- A newly installed plugin or MCP server starts fully unset: gateway behavior for plugin tools,
  offered for MCP tools.
- Removing all user intervention for a tool means deleting its name from the sets, returning to
  factory behavior with no residue.
- Keys: plugin tools are `plugin_name.tool_name`; MCP and built-in tools use their wire names.
  Names that no longer match anything (uninstalled plugin, renamed server) are retained but
  inert, following the interception-order philosophy of remembering disabled plugins' places.
- A name present in both sets: `hidden` wins (invisible is the conservative outcome) and `/status`
  reports the conflict.
- A disabled plugin's resident tools fall out of the request and their panel group grays out;
  re-enabling restores the user's stored choices.

## Host operation: `tools.offer`

A new intercepted operation between tool-schema resolution and the model request:

- Value: the offered tool names plus the host-owned `purpose` (`turn`, `vision`, `compaction`,
  `plugin`) as the single match key. Plugins see names, never schemas; schemas stay host-owned.
- Rules: a handler may remove any name, or add a name only if it is a tool of an enabled plugin —
  the host validates against registrations and injects the schema itself. No invented tools.
- Applied once per turn and cached for the turn, so every request in one turn offers the same set
  and the provider prompt cache survives multi-request turns.
- `ModelRequest.tools` stays read-only and reflects the post-narrowing set; `offered_tools` and
  `tool.call` remain the second line of defense for response and call validation.
- The chain failure policy follows `context.compose`: a failing chain fails the request; with no
  matching registration the fast path (`would_match`) applies and nothing is copied.
- Receipts record added and removed names, visible in `/status`.

Not done: a mutable `tools` field on `model.request`. Routing and response replacement already
  live at that boundary, and the wire schemas there are host-owned JSON; two sources of truth for
  what the model can call would need reconciling at every caller.

## Built-in plugin: `tool_visibility`

Consumes the persisted `resident`/`hidden` sets and applies them through `tools.offer` at the turn
boundary. It has no settings section of its own and no runtime configuration surface; it is the
single policy consumer of the user's stored choices. It never reads conversation state, which is
what makes its behavior cache-stable by construction.

## `/tools` panel

The only interaction surface, aligned with `/mcp` and `/plugins`. Displays derived state — hidden
first, then resident, then default per class — and offers three actions: set hidden, set resident,
clear (return to default). Space cycles, enter saves to plugin preferences, effective at the next
turn boundary; in-flight requests finish with the old set. Disabled plugins' groups gray out.

## Prompt files: a general convention, not one plugin's trick

The `system` block is only the first consumer. The file convention is host-provided and open to
any plugin that replaces instruction text through `context.compose`:

- A plugin declares user-editable prompt files (name, description, template) in its manifest.
  Installation creates them from templates where absent; uninstalling never deletes them — they
  are user data.
- Host provides `files.user(name)` resolving the effective file (project `.wizolt/plugins/<name>/`
  overriding the global `~/.wizolt/plugins/<name>/`, the AGENTS.md layering), so plugins do not
  each reimplement file lookup. UTF-8, size-bounded.
- Plugins are expected to substitute file contents verbatim, adding nothing of their own; the
  file is the single source of truth for the text they contribute.
- `/prompt edit` and the `/plugins` file listing are host conveniences that work for any such
  file, not only the built-in one. Request receipts record `system ← <path>` provenance, so
  `/status` shows which file replaced which block.
- Read at the turn boundary: an edit takes effect on the next turn without a restart. Unchanged
  file, byte-identical text, cache holds; a save invalidates once.

## Built-in plugin: `system_prompt`

The bundled, always-available consumer of that convention: binds the `system` block to
`.wizolt/system.md` in the workspace by default, overridable in config
(`[plugin.system_prompt] file = ...`), created from a template on first use. It substitutes the
file contents verbatim; the directive tail (language, attribution) stays host-owned and
untouched.

Why a file and not `config.toml`: the system prompt is long-form content for the model, not a
wizolt setting; a Markdown file has no syntax to break, can be symlinked, shared and committed,
and project-level override falls out of file layering. Config keeps one line: the path.

## `/prompt edit`

Host convenience, the same suspension model git uses: the TUI suspends rendering, hands the
terminal to `$EDITOR` (`VISUAL` over `EDITOR`, with a hint when neither is set) on the effective
prompt file, and re-reads it on editor exit. The next turn uses the new text. It is generic file
editing, reusable for other plugin-owned user files.

## Order of work

1. `tools.offer` value, subset/add rules and the engine adapter with per-turn caching and receipts.
2. `tool_visibility` consuming persisted sets; preference storage for the two exception sets.
3. `/tools` panel with derived-state display and the three actions.
4. `system_prompt` plugin, `/prompt edit` editor suspension.
5. Offline trials and tests (add/remove whitelist, sparse persistence, cache-set stability across
   turns), quality gates, CHANGELOG, bundled authoring-skill update.
