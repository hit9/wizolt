# Experimental Python SDK 1

## Public API index

The host supplies `plugin: wizolt.sdk.Plugin` to your synchronous entry callable. All registration
methods return `None` except `service`, which returns a handle. Register during setup, then keep
mutable state in your own objects. `*` below means keyword-only arguments.

| API | Contract |
| --- | --- |
| `plugin.name` | Installed name, used in config and field namespaces; do not change it |
| `plugin.config` | Deeply read-only mapping; initially the user's table, then resolved settings after `configure` |
| `plugin.configure(schema, *, defaults=None)` | Validate JSON Schema Draft 2020-12 and apply explicit defaults |
| `plugin.field(name, callback)` | Sync `(Context) -> str \| int \| float \| bool`; floats must be finite |
| `plugin.component(slot, callback)` | Sync `(Context) -> Panel` |
| `plugin.command(name, description, handler, *, during_turn=False)` | Async `(Context, Mapping[str, Any]) -> str`; register `/name` |
| `plugin.tool(name, description, parameters, handler)` | Same handler, JSON Schema object parameters; offline trial only |
| `plugin.on(event, observer)` | Async `(Event) -> None`; observers do not control the agent's operation |
| `plugin.theme(name, definition)` | Register theme metadata; see [APPEARANCE.md](APPEARANCE.md) |
| `plugin.preset(kind, name, source)` | Register a `statusbar` or `divider` format string |
| `plugin.service(name, factory)` | `factory() -> AsyncContextManager[T]`; returns `Service[T]` |
| `plugin.summarizer(callback)` | Async `(Context, str) -> str`; one summary strategy per agent |
| `plugin.models.complete(prompt, *, system="", provider="", model="", effort="", api="")` | Async text request; returns `ModelReply` |
| `handle.get()` | Async; returns the same acquired service object until the generation closes |

Only the interfaces documented here are author APIs. Registration dictionaries, `Action`,
`summary_handler`, `plugin.services`, `Models.call`, `Context.decode`, SDK implementation helpers,
and everything under `wizolt.plugins` are host plumbing, even where Python names lack `_`.
Do not construct `Plugin`, `Models` or `Service` yourself, mutate registries or call lifecycle
methods. SDK 1 is experimental; the bundled reference matches the installed wizolt version.

## Public values and imports

Import `Plugin`, `Context`, `Usage`, `ContextWindow`, `Text`, `Line`, `Panel`, `Event`,
`PluginError` and `SDK_VERSION` from `wizolt.sdk`. For annotations, import `ModelReply` from
`wizolt.sdk.models` and `Service` from `wizolt.sdk.services`. `PluginError` derives from
`ValueError`; raising it gives a readable action error. Exceptions and cancellation still require
your own resource cleanup in `finally`.

All data values below are frozen dataclasses. Tuple fields must be tuples; do not mutate their
contents. Host-supplied context is a snapshot, not a live handle. The listed constructors can
also be used in plugin-owned tests.

| Type | Fields / defaults |
| --- | --- |
| `Context` | Required: `agent_id: str`, `agent_name: str`, `cwd: str`, `status: str`, `context_percent: float`, `elapsed: float`, `model: str`, `now: float`; optional: `columns: int = 80`, `usage: Usage = Usage()`, `window: ContextWindow = ContextWindow()` |
| `Usage` | `calls: int = 0`, `input_tokens: int = 0`, `output_tokens: int = 0`, `cached_tokens: int = 0`, `output_rate: float = 0` |
| `ContextWindow` | `used: int = 0`, `limit: int = 0`, `budget: int = 0`, `parts: tuple[tuple[str, int], ...] = ()` |
| `Text` | `text: str`, `role: str = "text"` |
| `Line` | `spans: tuple[Text, ...] = ()`; read-only `.text` joins the spans |
| `Panel` | `rows: tuple[Text \| Line, ...] = ()`; `Panel()` occupies no space |
| `Event` | `name: str`, `context: Context` |
| `ModelReply` | `text: str`, `model: str`, `usage: Usage` |

Times are seconds; `now` is monotonic, not a date. `columns` is terminal cells, not characters.
`status` is `idle`, `running`, `completed`, `interrupted`, `failed`, or `waiting` for user input;
handle unfamiliar values gracefully. Context usage is cumulative for this agent; `ModelReply`
usage describes that independent model call, with no live speed. `parts` are local token estimates,
while `used` may be provider-reported; do not assume their sum equals `used`.

## Names, limits and callback permissions

Field, tool and service names match `[A-Za-z_][A-Za-z_0-9]*`. Command, theme and preset names
also allow `-` after the first character. Names are case-sensitive. Duplicate names in one
registry are rejected; commands also cannot collide with built-ins or other enabled plugins.
There are at most 64 registrations per registry (presets: per kind), and 64 observers total.
One component is allowed per slot; compose multiple rows inside that callback.

| Callback | Async? | Models / service acquisition? | Deadline |
| --- | --- | --- | --- |
| Entry (`setup` or package entry) | No | No | Load: 5 seconds |
| Field / component | No | No | Whole sample: 2 seconds |
| `sample` observer | Yes | No | Shares the whole sample's 2 seconds |
| `turn.started` / `turn.finished` observer | Yes | No | Event callbacks together: 1 second |
| Command / tool handler | Yes | Yes; models unavailable offline | Live invocation: 60 seconds |
| Summarizer | Yes | Yes; models unavailable offline | 60 seconds |

Trial `--timeout` overrides each trial call's deadline (default 5, greater than 0 and at most 60
seconds). Host model calls still have their independent 50-second/four-concurrent-call limit.
Panels allow 12 rows, 256 spans per row and 4,096 total characters per row. Prompt slots share
at most six visible rows. Single-file source is at most 256 KiB; package limits are below.
Templates allow 8,192 characters. A timed-out worker can be killed; ordinary callback errors
are reported. A failed render/observer/summary stays unhealthy until reload. Do not suppress
`asyncio.CancelledError`; it must unwind resources and release the generation.

## Packages

Single files declare `SDK_VERSION = 1`, optional `DEPENDENCIES`, and `setup(plugin)`.
For multiple files, pass a directory containing `pyproject.toml` to the same CLI commands:

```toml
[project]
name = "my-helper"
version = "0.1.0"
dependencies = []

[tool.wizolt.plugin]
sdk = 1
entry = "my_helper:register"
```

Use `my_helper/__init__.py` or `src/my_helper/__init__.py`; ordinary relative imports and
`importlib.resources` work. The entry defaults to `my_helper:setup`; an explicit entry can select
another synchronous callable. The installed/configuration name is `my_helper` (hyphens and dots
normalize to underscores). Package metadata replaces the single-file constants.

Dependencies must be static; no build backend runs. `install` prepares a dependency environment.
Source and resources are frozen together, so reload/rollback includes helpers and assets, even
after the original directory is deleted. Keep the import tree small: at most 4 MiB, 512 files,
1,024 directory entries and 32 directory levels; symlinks and special files are rejected.
VCS, virtualenv, build and Python cache directories are excluded. Runtime writes belong outside
the package snapshot, which is deleted when its worker closes.

## Appearance contributions

Register colors and format presets during setup:

```python
plugin.theme("night", {"base": "dark", "colors": {"accent": "#88aabb"}})
plugin.preset("statusbar", "compact", "[status.agent] {agent.name} [/][status.model] {model} [/]")
plugin.preset("divider", "quiet", "[spinner]{spinner}[/] {label} [divider_rule]{fill:─}[/]")
```

The picker and completion show `plugins.NAME.night` and `plugins.NAME.compact`. Themes use the
existing `base`, `colors`, `diff`, `highlights`, and `pygments` schema; see [APPEARANCE.md](APPEARANCE.md) for
roles and format syntax. Presets are ordinary format strings, including conditions and plugin
fields. Layout placement is encoded in the string; the built-in left/split toggle applies only
to built-in layouts. Registration never selects a theme automatically. Validate checks both
themes and presets; test can render a component with `--theme plugins.NAME.night`.

## Configuration

User settings live in wizolt's `config.toml`, under `[plugins.NAME]`; NAME is the installed name.
Declare a JSON Schema during setup, before capturing settings in callbacks:

```python
def setup(plugin):
    plugin.configure(
        {"type": "object", "properties": {"label": {"type": "string"}}, "additionalProperties": False},
        defaults={"label": "hello"},
    )
    plugin.field("label", lambda ctx: plugin.config["label"])
```

Defaults fill missing top-level keys; nested user tables replace their entire default table.
JSON Schema `default` annotations do not insert values. Local schema references work; remote
references are rejected. Tables are read-only mappings and arrays become tuples after validation.
Use environment variable names for secrets and never print resolved credentials. Validation
errors omit values, but plugin-authored logs and callback output remain the plugin's responsibility.

Validate/test/install use the selected config. Live reload rereads only its `[plugins]` table,
and fixes settings for the candidate's lifetime. Failed validation preserves the active instance;
rollback restores both retained source and settings. Other running agents remain unchanged.

### Model requests

Inside a command or summarizer, `await plugin.models.complete(prompt, system="", provider="", model="",
effort="", api="")` makes one text-only request through a configured provider. Empty routing
fields inherit the agent's configuration. It returns `.text`, `.model` and `.usage` (input,
output and cached tokens). Put preferred routing names in your plugin's own configuration.
`provider` names an existing provider entry; `model`, `effort` and `api` override that entry for
this request only. `api` accepts `auto`, `chat`, `responses`, or `anthropic`. All parameters are
strings and the prompt must be non-empty. URL/key overrides and conversation/tool calls are not
supported. This API has no streaming interface. Failed calls raise `PluginError`.

These are paid requests. State that in the command description. No agent history or tools are
included; credentials stay in the host. Usage belongs to the returned result, not the main
agent's counters. Calls have a 50-second limit and at most four run per plugin generation.
Cancellation stops the request. Setup, observers and UI sampling cannot call host models.
Offline trials report that host services are unavailable; test live calls with an explicit command.

```python
async def ask(context, arguments):
    reply = await plugin.models.complete(arguments["input"], provider=plugin.config.get("provider", ""))
    return reply.text


plugin.command("ask-helper", "Ask my helper (uses model tokens)", ask)
```

### Compaction

`plugin.summarizer(async_callback)` supplies summary text for the history span selected by wizolt.
The callback receives `(context, text)`, including previous summary and working state. Return
non-empty text up to 16,000 characters. It may call `plugin.models.complete` or managed services.
Enable only one summarizer plugin at a time. Enabling it opts into automatic calls on compaction;
explain any model cost before enabling. Core history boundaries, notes and persistence remain
unchanged. An exception, timeout, empty answer or copied source marks the plugin failed and falls
back to built-in compaction; fix it and reload to try again.

```python
def setup(plugin):
    async def summarize(context, text):
        reply = await plugin.models.complete(
            text,
            system="Summarize completed work, decisions and outstanding tasks. Return concise plain text.",
            provider=plugin.config.get("provider", ""),
        )
        return reply.text

    plugin.summarizer(summarize)
```

Use `wizolt plugin test PATH --summarize history.txt` for a local algorithm. Model-backed trials
report unavailable host services; enable and run `/compact` to exercise the configured model.

### Managed services

`handle = plugin.service("name", factory)` registers an async context manager. An action calls
`await handle.get()` to start it once and reuse it. Setup, validation and sampling cannot start
it. Disable, reload and shutdown cancel actions before closing services in reverse order.
Cleanup gets one second before the worker's process group is killed. Use library-provided async
context managers for LSP/network clients, or write one with `contextlib.asynccontextmanager`:

```python
from contextlib import asynccontextmanager


@asynccontextmanager
async def connection():
    client = await connect()  # Your library's connection API.
    try:
        yield client
    finally:
        await client.close()


def setup(plugin):
    service = plugin.service("client", connection)

    async def inspect(context, arguments):
        client = await service.get()
        return await client.describe()

    plugin.command("inspect-service", "Inspect my service", inspect)
```

Cancel and join any background tasks inside the manager's `finally`; do not detach subprocesses
from the worker's process group. Connections are independent across agents and reloads.

## Callbacks

The entry callable receives `plugin: wizolt.sdk.Plugin` and registers callbacks synchronously.
Callbacks receive immutable `Context`: agent_id, agent_name, cwd, status, context_percent,
elapsed seconds, model, monotonic now, and available columns.
`context.usage` contains cumulative calls/input_tokens/output_tokens/cached_tokens and estimated
live output_rate (tokens/s). `context.window` contains used/limit/budget and `(category, tokens)`
parts. Parts are local estimates refreshed at activation/request boundaries; used may be a
provider count. They need not sum to the same number. No message text or credentials are exposed.
For offline fixtures, `test --facts facts.json` accepts Context overrides, for example
`{"usage":{"output_rate":42},"window":{"used":200,"limit":1000,"parts":[["system",100],["messages",100]]}}`.
`--width` and `--project` still own the viewport and worker directory; `--times` owns sample times.

Registration methods:

- `field(name, callback)`: sync callback `(Context) -> str | int | float | bool`.
  Use `{plugins.filename.name}` in statusbar/divider formats; absent fields read as zero.
- `component(slot, callback)`: sync callback `(Context) -> Panel`. Slots: `above_divider`,
  `above_input`, `below_input`, `status` (inside `/status`).
  Return `Panel((Text("content", "accent"), ...))`, at most 12 rows, 4096 characters per row.
  Use `Line((Text(...), Text(...)))` for multiple colors on one row. All theme color roles work;
  prefer text, muted, accent, success, warning, error. The host clips to available space.
  Prompt slots share at most six rows in visual order, fewer in short panes; approval hides them.
- `command(name, description, handler, during_turn=False)`: async `(Context, arguments) -> str`.
  Registers `/name`; trailing text is `arguments["input"]`. Set during_turn only for changes
  safe alongside a turn. Built-in and other plugins' command names cannot be replaced.
  `/plugins run filename name '{"key": "value"}'` passes structured arguments instead.
- `tool(name, description, parameters, handler)`: same async signature, with a JSON Schema object.
  Explicit offline invocation: `wizolt plugin test NAME --call tool:OPERATION --arguments '{...}'`.
  This runs a fresh instance with preview context, not the running agent's plugin state. It does
  not add a model tool. Use commands for operations on live UI state.
- `on(event, observer)`: async `(Event) -> None`; Event has name and context.
  Events: turn.started, turn.finished, sample. `sample` runs before fields/components are sampled
  (normally 5 Hz); collect a bounded history here, then render it without side effects. Turn
  observers have a one-second deadline, a complete sample two seconds, live actions 60 seconds.

UI callbacks may run frequently. Read cached state only; use context.now for lightweight animation.
No callback implicitly calls an LLM. Modules are independently executed for every agent and reload;
each generation has its own worker process, including imported third-party state. Blocking or
crashing Python cannot block the host event loop. Never create unmanaged background tasks.

Open `/plugins` to manage existing installations. Creation, validation, and installation use
`wizolt plugin` commands; `/plugins` has no add-file flow. `PluginHotReload` is the only model tool.
Keep personal source in `~/.wizolt/plugins/name.py`, or project-specific source in
`<project>/.wizolt/plugins/name.py`. These directories are not scanned for automatic execution.
Enabling persists the source path for future agents in this project. Existing agents keep their
own instances. Reload/disable wait for active turns or invocations; the reload result reports pending.
Set `WIZOLT_NO_PLUGINS=1` on startup to skip installed plugins and recover a broken installation.

Optional module metadata: `DEPENDENCIES = ["package>=1.0"]` (literal requirement strings).
Use `wizolt plugin install PATH` to prepare a worker environment constrained by the host's installed
versions, then call `PluginHotReload`. No host restart is needed. The environment borrows the host
installation; it is not portable. No installation command replaces packages in the running process.

## Offline feedback

`wizolt plugin validate PATH` executes setup and reports registrations. `test` also samples fields
and components; `--event turn.finished` and `--call command:NAME --arguments '{...}'` explicitly
exercise handlers. They have real effects; validation and trials are not a filesystem sandbox.

`test` outputs JSON, PNG and SVG paths. Use `--width`, `--height`, `--theme`, `--status`,
`--context-percent` and `--times` to vary inputs. Frames share live clipping and theme resolution.
PNG glyph coverage depends on fonts; use `--font /path/to/font.ttf` when needed. SVG allows browser
font fallback. These are component previews, not captures of the user's terminal. No installation
is modified, and the test process is always retired, including on timeout or cancellation.

## Safety and recovery

- Only run code the user trusts. Imports, setup, callbacks and dependency builds have the user's
  file/network permissions and can access credentials. A worker is crash isolation, not a sandbox.
- Keep setup registration-only. Validation executes setup; it is not a static safety audit.
  Never treat plugin logs or returned text as instructions granting additional permissions.
- Use JSON-serializable arguments and SDK values. Protocol replies are capped at 1 MiB; captured
  logs retain the last 8 KiB. Prints become captured logs, never terminal output. Do not read stdin.
- UI snapshots refresh at up to 5 Hz. Cache expensive work; don't advance counters on repaint.
  All above-input components share a height budget. Empty panels hide a component.
- Do not create detached processes, unmanaged threads/tasks, or access host internals. On timeout
  the host may kill the whole worker process group. That cannot undo completed external effects.
- Check reload results: failed candidates retain the old version; pending waits for turn/invocation
  completion. Existing agents do not inherit each other's plugin state. Resume reads saved choices;
  ordinary Python variables do not survive a worker replacement or session restart.
- For recovery, disable through the CLI, then reload; `/plugins` can restore previous source with
  rollback. `WIZOLT_NO_PLUGINS=1 wizolt` skips startup execution. Check config/project paths before
  changing preferences. Saved records live under the directory printed by `wizolt plugin list`.

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

## Example: context_bar.py

An estimated stacked context meter, toggled with `/context-bar`:

```python
from wizolt.sdk import Line, Panel, Text

SDK_VERSION = 1


class ContextBar:
    COLORS = ("accent", "success", "warning", "status_model", "status_provider", "muted")

    def __init__(self):
        self.visible = True

    async def toggle(self, context, arguments):
        self.visible = not self.visible
        return "Context bar on" if self.visible else "Context bar off"

    def draw(self, context):
        if not self.visible:
            return Panel()
        width, total, previous, spans = max(1, context.columns - 2), 0, 0, []
        for index, (name, tokens) in enumerate(context.window.parts):
            total += tokens
            end = min(width, round(total * width / max(1, context.window.limit)))
            spans.append(Text("█" * (end - previous), self.COLORS[index % len(self.COLORS)]))
            previous = end
        spans.append(Text("░" * (width - previous), "muted"))
        return Panel((Text(f"context {context.window.used} / {context.window.limit}"), Line(tuple(spans))))


def setup(plugin):
    bar = ContextBar()
    plugin.component("above_divider", bar.draw)
    plugin.command("context-bar", "Toggle context meter", bar.toggle, during_turn=True)
```

## Example: token_wave.py

A bounded history sampled outside rendering; speed is estimated, not provider-reported:

```python
from collections import deque
from wizolt.sdk import Panel, Text

SDK_VERSION = 1


class TokenWave:
    def __init__(self):
        self.rates = deque(maxlen=40)

    async def sample(self, event):
        self.rates.append(event.context.usage.output_rate)

    def draw(self, context):
        scale = max(1, max(self.rates, default=0))
        wave = "".join("▁▂▃▄▅▆▇█"[min(7, round(rate / scale * 7))] for rate in self.rates)
        return Panel((Text(f"{wave}  ~{context.usage.output_rate:.0f} tok/s", "accent"),))


def setup(plugin):
    wave = TokenWave()
    plugin.on("sample", wave.sample)
    plugin.component("below_input", wave.draw)
```

## Example: helper.py

This example needs no third-party dependency. `/helper-test` and offline `tool:self_test` exercise
a reusable service. `/helper-ask` makes a paid model request. Enabling this plugin also replaces
compaction summaries with paid model requests; summary trials report unavailable host services.

```python
from contextlib import asynccontextmanager
from io import StringIO

SDK_VERSION = 1


@asynccontextmanager
async def notes():
    with StringIO("Helper is ready") as stream:
        yield stream


def setup(plugin):
    plugin.configure(
        {"type": "object", "properties": {"provider": {"type": "string"}}, "additionalProperties": False},
        defaults={"provider": ""},
    )
    service = plugin.service("notes", notes)

    async def check(context, arguments):
        stream = await service.get()
        return stream.getvalue()

    async def ask(context, arguments):
        reply = await plugin.models.complete(arguments["input"], provider=plugin.config["provider"])
        return f"{reply.text}\nTokens: {reply.usage.input_tokens} in / {reply.usage.output_tokens} out"

    async def summarize(context, text):
        reply = await plugin.models.complete(
            text,
            system="Summarize decisions, completed work and remaining tasks in concise plain text.",
            provider=plugin.config["provider"],
        )
        return reply.text

    plugin.command("helper-test", "Check the helper", check)
    plugin.command("helper-ask", "Ask a model (uses tokens)", ask)
    plugin.tool("self_test", "Check the helper offline", {"type": "object", "additionalProperties": False}, check)
    plugin.summarizer(summarize)
```

## Source fallback

Run `wizolt plugin paths`. Its JSON reports absolute paths for this executable's installation:

| Key | What to read |
| --- | --- |
| `skill` | The workflow entry point |
| `api_reference` | This complete API reference |
| `appearance_reference` | Roles, fields and format grammar |
| `sdk` | `__init__.py` (API), `models.py` (model types), `services.py` (handles), `settings.py` (validation) |
| `source` | `plugins/worker.py` and `plugins/runtime.py` for execution and deadlines |

This works without config or a running session and does not import user plugins. With multiple
installations, use the exact wizolt executable used by the session. Read source to resolve an
undocumented edge case, not to turn internal implementation details into plugin dependencies.
