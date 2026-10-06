# Tool offers and prompt files

Status: revised 2026-10-06, not implemented. The goal is that a **plugin can** narrow or extend the
tools a request offers and replace instruction text from a file the user edits. The host ships
those abilities, not a policy built on them: no bundled tool-visibility or system-prompt plugin
and no `/tools` panel are part of this work. A user writes such a policy as an ordinary plugin
(the acceptance examples below are two of them). It follows the interception contract in
[Plugin operations](PLUGIN_INTERCEPTION.md); the [implementation boundaries](PLUGINS.md) still
apply, including "every bundled plugin is off by default".

## The two gaps this closes

1. Tools — a plugin cannot change which tools a request offers the model. It cannot hide a built-in
   tool from a request; it can only refuse a call the model has already spent a round trip on
   (`tool.call`). Its own tools are reachable only through the `Plugin` gateway (list, describe,
   call). `model.request` keeps `tools` read-only on purpose: routing already lives there.
2. Prompt — a plugin can replace the `system` block through `context.compose`, but has no agreed
   place for user-written text, and the host has no way to offer that text for editing.

## Operation: `tools.offer`

One intercepted operation per turn, between tool-schema resolution and the turn's first request.

- Value `ToolOffer`: `tools`, the names the turn offers (wire names, in host order), and the
  read-only `available`, the tools of enabled plugins that may be added, as `plugin.tool`. Plugins
  see names, never schemas. No match keys: every request that carries tools belongs to one turn.
- Rules: a handler may remove any offered name and add only a name from `available`; anything
  else blocks the registration like any invalid transition. The host restores its own order
  (offered names first, in their original order, then added ones in `available` order), so a
  handler cannot change the bytes by reordering.
- An added plugin tool is offered under the wire name `<plugin>-<tool>` (both are identifiers, so
  the hyphen is unambiguous) with the schema the plugin registered. A call to it runs exactly as
  `Plugin(action="call")` does: same approval, same `tool.call` chain (`tool` is `Plugin`), same
  result and transcript row. It still runs when the handler removed `Plugin` itself.
- Applied once per turn and held for the turn: every request in it — steps, the image-fallback
  resend, the tool-correction resend, an inline compaction request — offers the same set, so the
  provider cache survives a multi-request turn. Vision and `plugin.models.complete` requests send
  no tools and are unaffected. The next turn asks again; a policy that answers the same way keeps
  the prefix byte-identical, and one that changes the set costs a cache miss once, the same
  trade-off `context.compose` documents.
- A call to a name the turn did not offer is refused before it runs, whatever the model
  remembers from earlier turns. `ModelRequest.tools` reflects the narrowed set, so the existing
  `offered_tools` check already refuses a routed response that calls a removed tool.
- Failure policy follows `context.compose`: a failing chain fails the turn; with no matching
  registration the fast path applies and nothing is copied. A receipt records the added and
  removed names, shown in `/status`.
- MCP and skills stay behind their own gateway tools (`MCP`, `Skill`): removing the gateway
  removes the family. Narrowing inside a family is not part of this operation.

Not done: a mutable `tools` field on `model.request`. Routing and response replacement already
live at that boundary, and the wire schemas there are host-owned JSON; two sources of truth for
what the model can call would need reconciling at every caller.

## Prompt files: declared by a plugin, written only by the user

- A plugin declares a user-editable file: `plugin.user_file(name, description, template="")`.
  Declaring writes nothing. Install, enable, reload and first use never create the file.
- Location: `<project>/.wizolt/plugins/<plugin>/<name>` overrides
  `<data_dir>/plugins/<plugin>/<name>` (the AGENTS.md layering). `Context` gains the `data_dir`
  fact, and one SDK helper resolves the effective path, used by plugins and host alike, so
  nobody reimplements the lookup. Reading the file is the plugin's own ordinary file read.
- `/prompt edit [plugin.name]` (host convenience, any declared file) suspends the TUI and opens
  `$VISUAL`/`$EDITOR` on the effective file, or on the template when no file exists yet. Only a
  save that changes the text writes: to the effective file, else to the `data_dir` location.
  Quitting without changes leaves the disk untouched. `/prompt` without arguments lists declared
  files and whether each exists.
- A plugin reads the file when it composes, so an edit takes effect on the next request without
  a restart. Unchanged file, byte-identical text, cache holds; a save invalidates once.

## Acceptance examples (written as trial fixtures, not shipped)

| Wish | Plugin built on |
| --- | --- |
| Hide `Subagent` and `ToolScript` from every turn | `tools.offer` removing two names |
| Offer my `notes.search` tool directly instead of through `Plugin` | `tools.offer` adding `notes.search` |
| Replace the system prompt with my own file | `user_file("system.md")` + `context.compose` editing `system` |

## Order of work

1. `tools.offer`: SDK value and rules, the per-turn engine adapter, resident wire names and their
   dispatch, the refusal of unoffered calls, receipts.
2. `user_file` declarations, the `data_dir` fact and the path helper.
3. `/prompt edit` and `/prompt` listing.
4. Trials and tests for the three examples, the bundled authoring skill, CHANGELOG.
