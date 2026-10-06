# Tool offers and prompt files

Status: implemented (revised 2026-10-06). The goal is that a **plugin can** narrow or extend the
tools a request offers and replace instruction text from a file the user edits. The host ships
those abilities and nothing more; the features are two bundled plugins, `tool_visibility`
(`/tools`) and `system_prompt` (`/prompt`), built only on the public SDK and, like every bundled
plugin, off until the user enables them. They are the proof that the abilities suffice. It follows
the interception contract in [Plugin operations](PLUGIN_INTERCEPTION.md); the
[implementation boundaries](PLUGINS.md) still apply.

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

## Abilities: files the user writes, and their editor

- `plugin.user_file_paths(context, name)` gives where a file the user writes for the plugin
  lives, in precedence order: `<project>/.wizolt/plugins/<plugin>/<name>`, then
  `<data_dir>/plugins/<plugin>/<name>` (the AGENTS.md layering). `Context` gains the `data_dir`
  fact for it. It reads and creates nothing; the plugin reads the first that exists and writes a
  new one to the last, with ordinary file IO. The host keeps no list of such files.
- `plugin.ui.edit(text)` hands the terminal to `$VISUAL`/`$EDITOR` on the text, as Ctrl-G does for
  the input, and returns the saved text or `None`. It writes no file. Like other views it is for
  commands, tools and the two user-facing interceptors; the wait is a person's, so it pauses the
  action's deadline. Trials script it as `{"expect": {"kind": "edit"}, "reply": TEXT or null}`.

Not done: a host `/prompt` command or file declarations in the manifest. Editing a file is the
feature, so it belongs in the plugin that owns the file.

## The bundled plugins

Both ship disabled and use only the public SDK; enabling one changes nothing until the user acts.

| Plugin | What the user does | Built on |
| --- | --- | --- |
| `tool_visibility` | `/tools`: a multi-select of the offered built-in tools and running plugins' tools; the choice is saved as `[plugins.tool_visibility] hidden` / `resident` | `tools.offer`, `ui.select_many`, `settings.update` |
| `system_prompt` | `/prompt`: the editor on their file, or on the prompt the last request carried; a changed save writes the file, and from the next request it replaces the `system` block verbatim | `user_file_paths`, `ui.edit`, `context.compose` |

`/tools` learns the tool names from the turn's offer, so it asks for one message first. A
`system_prompt` file that is unreadable, not UTF-8, empty or over 256 KiB leaves wizolt's prompt
in place rather than failing requests. Nothing is written by enabling either plugin.

Both are tested for the cache contract: a changed choice or saved file changes the request once,
and every later request is byte-identical again.
