# Experimental Python SDK 1

Every plugin declares `SDK_VERSION = 1` and synchronous `setup(plugin: wizolt.sdk.Plugin)`.
Callbacks receive immutable `Context`: agent_id, agent_name, cwd, status, context_percent,
elapsed seconds, model, monotonic now, and available columns.

Registration methods:

- `field(name, callback)`: sync callback `(Context) -> str | int | float | bool`.
  Use `{plugins.filename.name}` in statusbar/divider formats; absent fields read as zero.
- `component(slot, callback)`: sync callback `(Context) -> Panel`. Slots: `above_input`, `status`.
  Return `Panel((Text("content", "accent"), ...))`, at most 12 rows, 4096 characters per row.
  Roles: text, muted, accent, success, warning, error. The host clips to available space.
- `command(name, description, handler)`: async `(Context, arguments) -> str`.
  User invocation: `/plugins run filename name '{"key": "value"}'`.
- `tool(name, description, parameters, handler)`: same async signature, with a JSON Schema object.
  Discover via Plugin inspect and invoke via Plugin(action="invoke", target="filename",
  operation="name", arguments={...}). Invocations use normal tool approval and cancellation.
- `on(event, observer)`: async `(Event) -> None`; Event has name and context.
  Events: turn.started, turn.finished. Observer exceptions are reported by inspect; each observer
  has a one-second cooperative timeout. Tool/command handlers have a 60-second timeout.

UI callbacks may run frequently. Read cached state only; use context.now for lightweight animation.
No callback implicitly calls an LLM. Modules are independently executed for every agent and reload;
imported third-party modules remain process-shared. Never create unmanaged background tasks.

Open `/plugins` to manage existing installations. Creation, validation, and installation use
the `Plugin` tool; `/plugins` has no add-file flow.
Keep personal source in `~/.wizolt/plugins/name.py`, or project-specific source in
`<project>/.wizolt/plugins/name.py`. These directories are not scanned for automatic execution.
Enabling persists the source path for future agents in this project. Existing agents keep their
own instances. Reload/disable wait for active turns or invocations; inspect reports pending.
Set `WIZOLT_NO_PLUGINS=1` on startup to skip installed plugins and recover a broken installation.

Optional module metadata: `DEPENDENCIES = ["package>=1.0"]` (literal requirement strings).
Use `install` to prepare a new environment against all enabled plugins and the host's installed
versions. It returns `restart_required` with an explicit launch command. This environment borrows
the current host installation; it is not a portable bundle. No installation command replaces
packages in the running process.

## Example: pet.py

Save this as a user plugin; it is not a built-in feature. Animation reads the host clock and uses
the normal visible refresh cadence, without creating a timer or background task.

```python
from wizolt.sdk import Context, Panel, Plugin, Text

SDK_VERSION = 1


def draw(context: Context) -> Panel:
    working = context.status == "running"
    eyes = "o.o" if not working or int(context.now * 2) % 2 else "-.-"
    return Panel((Text(" /\\_/\\", "accent"), Text(f"( {eyes} )  {context.agent_name} · {context.status}", "accent")))


def setup(plugin: Plugin) -> None:
    plugin.component("above_input", draw)
```

## Example: tokenweather.py

This uses the current agent's reported context percentage, falling back to the host's estimate.
It shares the above-input area with other plugins and exposes a field for existing format strings.

```python
from wizolt.sdk import Context, Panel, Plugin, Text

SDK_VERSION = 1


def weather(context: Context) -> str:
    return "storm" if context.context_percent >= 90 else "cloudy" if context.context_percent >= 70 else "clear"


def draw(context: Context) -> Panel:
    role = "error" if context.context_percent >= 90 else "warning" if context.context_percent >= 70 else "success"
    return Panel((Text(f"{weather(context)} · context {context.context_percent:.0f}% · {context.model}", role),))


def setup(plugin: Plugin) -> None:
    plugin.field("weather", weather)
    plugin.component("above_input", draw)
    plugin.component("status", draw)
```
