# Trials

## Scripted interaction trials

Run a command or tool with answers, without a live TUI:

```sh
wizolt plugin test PATH --call command:pet --interactions answers.json
```

`answers.json` is an ordered array, at most 64 steps:

```json
[
  {
    "expect": {"title": "Pet", "kind": "selection"},
    "reply": {"selected": ["dragon"]}
  },
  {
    "expect": {"title": "Name", "kind": "form"},
    "reply": {"values": {"value": "Ember"}}
  }
]
```

Match exact title and kind (`selection`, `form`, `document`), not random runtime view IDs.
`selected` contains choice IDs, never labels; `values` maps field IDs to text. Convenience
`ui.input` uses field `value`; `ui.confirm` uses selection IDs `yes` and `no`.
`reply: null` cancels. Optional `action` chooses a declared action ID; omitted means the first
declared action or `submit`. Omitted answers retain the current draft: a single-choice list
starts at its first item unless given a default. Multi-selection results follow display order.
Forms use the same required/choice/text validation as the live host; invalid replies fail
without partially applying field changes.

For `ui.open()` followed by updates, list expected revisions before answering:

```json
[
  {
    "expect": {"title": "Loading", "kind": "document"},
    "updates": [{"title": "Ready", "kind": "document"}],
    "reply": {"action": "submit"}
  }
]
```

Missing, mismatched, invalid or unused answers fail the trial. Updates must retain the view kind
and fullscreen mode. Waiting for updates is bounded by `--timeout`; it never waits for a person.
Notifications are recorded. Component layout, shortcut management and model services still
require a live session; a fixture does not simulate their effects.

Every trial copies the plugin's starting settings to a disposable configuration. SDK
`settings.update()` writes there, even without `--interactions`; the selected real config is
unchanged. This is not a filesystem sandbox: ordinary plugin Python can still write files or
use the network. Do not use production resources in trial handlers.

## Interception trials

Run your interceptors against scripted operations, without a live agent:

```sh
wizolt plugin test PATH --operations operations.json [--interactions answers.json]
```

`operations.json` is an ordered array, at most 64 entries. Each names an operation, its input
(the value's fields; `type` may be omitted) and `next`, what wizolt would return when your
handler calls `next`: a result's fields, or `{"error": "message"}` for a failure.

```json
[
  {"operation": "tool.call", "input": {"id": "c1", "tool": "Bash", "arguments": {"command": "ls"}},
   "next": {"content": "a.txt b.txt", "status": "ok"}},
  {"operation": "tool.call", "input": {"id": "c2", "tool": "Bash", "arguments": {"command": "rm -rf build"}}},
  {"operation": "context.compact", "input": {"text": "user: ship it", "trigger": "manual"}}
]
```

Omit `next` when your handler must answer by itself (a refusal, a cached result, a summary).
A `next` your handler never calls, or a call to `next` the fixture does not answer, fails the
trial: nothing reaches wizolt's real tools, models or compaction. The trial runs your real
handler through wizolt's chain executor with the same read-only, refusal and `next` rules; views
your handler opens use `--interactions`. Live placement rules (attachment, tool-name and context
block checks) apply in a session.

Read the JSON report:

- `operations`: per entry, `effective` (the value that reached `next`, or null), `result` (what
  was delivered), `origin` (which registration produced it, or `core`) and `shaped`.
- `results`: handler return values.
- `interactions`: view declarations, projected fragments, answers and notices.
- `settings_changes`: successful temporary writes (`values` and `reset`); existing unrelated
  settings are not copied into the report.
- `previews`: component, preset and interaction exports. `svg_path` and `png_path` are file
  paths, not image content. Open the files to inspect the actual images.
- `render_sha256`: resolved cell content/colors/geometry, excluding temporary paths and random
  IDs. It is not a PNG checksum and does not cover font differences or real-terminal behavior.

Test success covers handler flow, declarations and replies. It does not prove real key routing,
focus, terminal resizing or font rendering; exercise those in a live session when relevant.
