# Interactive UI

Read this after [SDK.md](SDK.md). Use these APIs only inside a registered command or tool
handler, or a `prompt.submit` / `tool.call` interceptor. No interaction from setup, ticks,
rendering, observers or other interceptors. Rendering callbacks are separate: a
[presenter](#presenters) returns a panel and never interacts. The host
owns input, theme, scrolling, focus, cancellation and terminal resizing.

## API

All calls below are async except `open`, which returns an async context manager.

| API | Result |
| --- | --- |
| `plugin.ui.input(title, *, default="", multiline=False, required=False)` | `str` or `None` on cancel |
| `plugin.ui.confirm(title)` | `bool`; Cancel is selected initially; Esc returns `False` |
| `plugin.ui.select(title, *, items, default="")` | Choice ID or `None` |
| `plugin.ui.select_many(title, *, items, defaults=())` | Tuple of choice IDs, possibly empty; `None` on cancel |
| `plugin.ui.edit(text)` | The user's editor (`$VISUAL`, `$EDITOR`) on `text`, as Ctrl-G edits the input: the saved text, or `None` when it did not save; writes no file. Not while your own view is open. Trials script it as `{"expect": {"kind": "edit"}, "reply": TEXT or null}` |
| `plugin.ui.show(view)` | `ViewResult` or `None` on cancel |
| `plugin.ui.open(view)` | `OpenView`; use `async with`, await `result()` and optionally `update(view)` |
| `opened.result()` | `ViewResult` or `None`; awaiting it does not block terminal input |
| `opened.update(view)` | Replace content; retain body kind/fullscreen and stable IDs to preserve navigation/drafts |
| `plugin.ui.notify(message, *, level="info")` | Session-local transcript notice; info/success/warning/error; no desktop notification |
| `plugin.ui.report(lines)` | Append one framed, theme-colored `Line` block to the answer area during an action, the moment it arrives; raises without interactive UI |
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
| `Document` | `text: str`, `lexer: str = "text"`, `sections: tuple[Section, ...] = ()`, generated `kind: str = "document"`; use `markdown`, `diff` or a Pygments lexer name |
| `Section` | `label: str`, `text: str`; continues its document in the same frame and lexer, under a gray rule carrying the single-line label |
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
- Documents: 256,000 characters, sections included; 32 sections. Lists: 1,000 choices, previews 16,000 characters each.
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

## Presenters

A presenter replaces how the host draws one **presentation site**: a named piece of settled output.
`render` is `async (Context, view) -> Panel`, and the view model comes from
`wizolt.sdk.presentation`. Register at most one presenter per site.

| Site | View | Fields |
| --- | --- | --- |
| `tool.call` | `ToolCard` | `id`, `tool`, `arguments`, `status`: `ok`, `failed` or `refused` |
| `tool.result` | `ToolSummary` | `ToolCard` fields, plus `output` (the text the model received), `elapsed` and `key` (the stored result, `tr.N`) |
| `activity` | `ActivityStatus` | `status`, `elapsed`, `stream_kind`, `stream_text`, `active_tools`, `counts` |

`match` prefilters on those read-only fields before the call reaches your worker. The tool sites
match on `tool`, as one name or a tuple of names; `activity` matches every registered view. A view
your match rejects keeps the builtin rendering and never reaches the worker.

```python
from wizolt.sdk import Line, Panel, Text
from wizolt.sdk.presentation import ToolSummary

SDK_VERSION = 1


def setup(plugin):
    async def bash_result(context, view: ToolSummary):
        command = str(view.arguments.get("command", ""))
        first = view.output.strip().splitlines()[:1]
        return Panel(
            (
                Line((Text("$ ", "muted"), Text(command, "tool"))),
                Text(first[0] if first else "(no output)", "success" if view.status == "ok" else "error"),
            )
        )

    plugin.presenter("tool.result", bash_result, match={"tool": "Bash"})
```

A panel has the component limits: at most 12 rows and 4,096 characters per row. An exception, a
panel over those limits, a missed deadline (0.5 seconds, 0.25 for `activity`) or a rejected match
all fall back to the builtin rendering, and the host skips that registration until the next reload
or enable. A missed deadline cancels the callback; your commands, tools and interceptors keep
working. A callback that blocks the event loop (synchronous sleep or I/O) cannot be cancelled, so
the worker is retired. Keep the callback trivial and read only cached state.

The two tool sites are independent: a `tool.call` panel replaces only the call line (its later
rows go under it), a `tool.result` panel only the builtin summary below. A site with no
presenter, or an empty panel, keeps its builtin rows.

Presenters own rows, not facts. Approval displays and running cards are never presented, and the
stored-result citation, the status tag, queued follow-ups, the live preview and the divider stay
host-owned and ride along whatever your rows said. Tool rows are rendered as log rows, so the role
of a row's first span sets its tone: use the log roles (`tool`, `meta`, `muted`, `success`,
`warning`, `error`, `diff`, `field`, `code`); any other role reads as ordinary output. The
`activity` panel comes from the host's refresh pass, not a repaint: an empty panel leaves the
region to the builtin stream preview.

Presenters are rendering, not interaction: like observers they receive no host services -- no
models, no services, no settings writes and no UI. Return a panel; never wait for the user.

One presenter renders each site. When two plugins register a site and both match, the builtin
rendering stays until the user picks with `wizolt plugin presenter list|choose|reset`. The choice
is saved in `[plugin_manager.presenters] choice`, and an agent adopts it with
`Plugin(action="reload")`.

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
