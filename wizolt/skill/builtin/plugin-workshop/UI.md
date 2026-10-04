# Interactive UI

Read this after [SDK.md](SDK.md). Use these APIs only inside a registered command or tool
handler. No interaction from setup, samples, rendering, observers or summarizers. The host
owns input, theme, scrolling, focus, cancellation and terminal resizing.

## API

All calls below are async except `open`, which returns an async context manager.

| API | Result |
| --- | --- |
| `plugin.ui.input(title, *, default="", multiline=False, required=False)` | `str` or `None` on cancel |
| `plugin.ui.confirm(title)` | `bool`; Cancel is selected initially; Esc returns `False` |
| `plugin.ui.select(title, *, items, default="")` | Choice ID or `None` |
| `plugin.ui.select_many(title, *, items, defaults=())` | Tuple of choice IDs, possibly empty; `None` on cancel |
| `plugin.ui.show(view)` | `ViewResult` or `None` on cancel |
| `plugin.ui.open(view)` | `OpenView`; use `async with`, await `result()` and optionally `update(view)` |
| `opened.result()` | `ViewResult` or `None`; awaiting it does not block terminal input |
| `opened.update(view)` | Replace content; retain body kind/fullscreen and stable IDs to preserve navigation/drafts |
| `plugin.ui.notify(message, *, level="info")` | Session-local transcript notice; info/success/warning/error; no desktop notification |
| `plugin.ui.shortcuts.list()` | Dictionary with `bindings`, `protected` keys and `error` |
| `plugin.ui.shortcuts.bind(key, command, *, replace=False)` | Updated shortcut report; command is `plugin.command` |
| `plugin.ui.shortcuts.unbind(key)` | Updated report; restores host behavior, idempotent |

The host supplies these service objects. Do not construct `UI`, `Shortcuts` or `OpenView`,
set their transport callbacks, or inspect internal tasks. Use the following declaration values.

## Values

Import all types in this table from **`wizolt.sdk.views`**. They are frozen dataclasses;
construct tuple fields as tuples. `kind` is generated, not a constructor argument.

| Type | Fields / defaults |
| --- | --- |
| `Choice` | `id: str`, `label: str`, `preview: str = ""` |
| `Action` | `id: str`, `label: str`, `key: str = ""` |
| `Field` | `id: str`, `label: str`, `default: str = ""`, `required: bool = False`, `multiline: bool = False`, `choices: tuple[Choice, ...] = ()` |
| `Document` | `text: str`, `lexer: str = "text"`, generated `kind: str = "document"`; use `markdown`, `diff` or a Pygments lexer name |
| `Selection` | `items: tuple[Choice, ...]`, `selected: tuple[str, ...] = ()`, `multiple: bool = False`, generated `kind: str = "selection"` |
| `Form` | `fields: tuple[Field, ...]`, generated `kind: str = "form"` |
| `View` | `title: str`, `body: Document \| Selection \| Form`, `actions: tuple[Action, ...] = ()`, `fullscreen: bool = False` |
| `ViewResult` | `action: str`, `selected: tuple[str, ...] = ()`, `values: dict[str, str] = {}` |

`Action` here is the **public view action**, not the internal registration object in
`wizolt.sdk`. Its ID is a result, never executable code. The first action is the default
Enter/submit action; later actions require keys. Without actions the result ID is `submit`.
Single-character shortcuts do not fire while editing text or searching. Esc and Ctrl-C cancel;
navigation, editing and emergency keys cannot be overridden. Key aliases such as Ctrl-X and
c-x are equivalent. Help shows declared actions automatically.

Lists use arrows/j/k, `/` search, Enter select, and Space to toggle multi-selection. Forms
use Tab/Shift-Tab between fields, Enter to submit a single-line field, and Ctrl-S to submit
from any field. A multiline field uses Enter for a newline. Choice fields use arrow keys;
their default/result is a choice ID. `required` is checked before submission. Perform
application-specific validation in your handler and reopen with the entered defaults if needed.
Documents support scrolling, g/G, `/` search and n for the next match.

Keyboard and scripted answers follow the same rules: omitted fields keep current drafts,
an unselected single-choice list starts at its first item, and multi-selection returns IDs in
display order. `ViewResult.resolve(view, reply)` validates a plain answer against declaration
defaults without opening a UI; it raises `ResponseError` (`PluginError`) with an optional
`field` ID. Use it for local checks, not to bypass host interaction.

Views use the normal modal region unless `fullscreen=True`, which temporarily uses the
alternate screen. Return to a parent list with an ordinary loop: await a selection, await its
detail view, then show the list again with its previous selected ID. There is no persistent
split, arbitrary-coordinate drawing or access to host widgets.

## Limits and lifetime

- Titles: 200 characters, single-line. IDs: 100 characters, unique and nonempty within their
  collection. Labels: 300 characters, single-line. Terminal control sequences are refused.
- Documents: 256,000 characters. Lists: 1,000 choices, previews 16,000 characters each.
  Forms: 1–32 fields with values up to 16,000 characters. Actions: 16. The shared 1 MiB
  worker-frame limit also applies to the whole request, so several large previews must be reduced.
- One open view per plugin per agent. Requests from different plugins queue. Host approvals
  retain ownership of the input; a plugin confirmation is not tool authorization.
- Human waiting pauses the action's execution deadline. Updates and other host services keep
  their ordinary deadlines. Esc remains host-owned even if your worker is busy.
- Cancellation, worker failure, disable and successful replacement dismiss owned views.
  A retiring call cannot open another view. A failed reload preserves the previous generation
  and its view. Do not suppress cancellation.
- Keep update tasks scoped to the command/tool and join them before leaving `async with`.
  Closing a view before awaiting its result is supported; a completed/closed view cannot update.
- Offline trials and non-TUI sessions cannot prompt the user. Use [scripted trials](TESTING.md)
  for declaration/response flows and window previews, then exercise keys, focus and resizing
  in a live session. Exports depict host cell layouts, not terminal screenshots.

## Example: prompt, choose, inspect

```python
from wizolt.sdk.views import Choice, Document, View

SDK_VERSION = 1


def setup(plugin):
    async def notes(ctx, args):
        name = await plugin.ui.input("Note name", required=True)
        if name is None:
            return "Cancelled"
        selected = "preview"
        while True:
            selected = await plugin.ui.select(
                name,
                items=(Choice("preview", "Preview"), Choice("done", "Done")),
                default=selected,
            )
            if selected != "preview":
                return "Done"
            await plugin.ui.show(View(name, Document("# " + name, "markdown"), fullscreen=True))

    plugin.command("notes-view", "Name and preview a note", notes)
```

For a live log, use `async with plugin.ui.open(View(...)) as opened`, keep a bounded log,
and call `await opened.update(View(...))` from an action-owned task while awaiting
`opened.result()`. Cancel and join the updater in `finally`; retain the same view kind and mode.

## Shortcuts: let the agent manage them

Enable the bundled `layout` plugin and use its `list_shortcuts`, `bind_shortcut` and
`unbind_shortcut` operations through the existing `Plugin` list/describe/call gateway.
No separate settings screen is needed. For example bind **F6** to `myplugin.notes-view`.
Global keys must be function keys or Ctrl keys. Occupied keys require `replace=True` after
the user chooses replacement; protected input/cancellation keys never allow replacement.
Bindings are active only at idle input, or mid-turn for commands declared `during_turn=True`.
They never fire in a modal or approval, and do not consume the user's draft.

Preferences live in `config.toml` under `[plugin_manager.shortcuts.bindings]`, not the plugin's configure
schema. They survive disable/re-enable and are agent-local in memory. Other live agents adopt
saved changes on reload. A report's `active` means the target is available, not that its key
can fire in the current input mode.

## Recovery

Start **`wizolt --no-plugins`** (or set `WIZOLT_NO_PLUGINS=1`) to skip all plugin code for
that launch, including reload/enable. Saved preferences are untouched. `/plugins` can still
disable a broken installation; restart normally after fixing it. Plugins are separate processes,
not a filesystem/network sandbox: review trusted source and keep it in its own Git repository.
