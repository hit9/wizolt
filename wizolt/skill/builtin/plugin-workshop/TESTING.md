# Scripted interaction trials

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
declared action or `submit`. Forms reuse the live host's required/choice validation.

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

Read the JSON report:

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
