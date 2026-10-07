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
| `plugin.settings.update(values, *, reset=())` | Async; atomically save declared overrides, remove reset keys and return resolved immutable settings; current `config` is unchanged |
| `plugin.field(name, callback)` | Sync `(Context) -> str \| int \| float \| bool`; floats must be finite |
| `plugin.component(slot, callback, *, gap_before=0)` | Sync `(Context) -> Panel` |
| `plugin.command(name, description, handler, *, during_turn=False)` | Async `(Context, Mapping[str, Any]) -> str`; register `/name` |
| `plugin.tool(name, description, parameters, handler)` | Register a plugin operation, not a standalone model tool; same handler, JSON Schema object parameters; call only through `Plugin` list/describe/call |
| `plugin.on(event, observer)` | Async `(Event) -> None`; observers do not control the agent's operation |
| `plugin.theme(name, definition)` | Register theme metadata; see [APPEARANCE.md](APPEARANCE.md) |
| `plugin.preset(kind, name, source)` | Register a `statusbar` or `divider` format string |
| `plugin.service(name, factory)` | `factory() -> AsyncContextManager[T]`; returns `Service[T]` |
| `plugin.user_file_paths(context, name)` | Where a file the user writes for you lives, in precedence order: `<project>/.wizolt/plugins/NAME/<name>`, then `<data_dir>/plugins/NAME/<name>`; read the first that exists, write a new one to the last; reads and creates nothing. Read it when you need it, so an edit applies without a reload |
| `plugin.intercept(operation, handler, *, match=None, response=None)` | Async `(Context, value, next) -> result`; wrap one operation; see [INTERCEPTION.md](INTERCEPTION.md) |
| `plugin.presenter(site, render, *, match=None)` | Async `(Context, view) -> Panel`; render one presentation site; see [UI.md](UI.md) |
| `plugin.models.complete(prompt, *, system="", provider="", model="", effort="", api="")` | Async text request; returns `ModelReply` |
| `plugin.agent.tools()` | Async; `tuple[ToolInfo, ...]`: the built-in tools a turn's `tools.offer` starts from and the plugin tools it may add (`plugin.tool`), each with `name`, `description` (first line), `kind` (`builtin`/`plugin`) and `offered` (by the last turn); works before any turn |
| `plugin.agent.system_prompt()` | Async; the text a `context.compose` handler receives as its `system` block, before any plugin changes it |
| `plugin.agent.system_directives()` | Async; `tuple[Directive, ...]`: the fixed blocks wizolt's settings append after the `system` block in every request, in sent order, whatever replaced it; each has `setting` (`runtime.reactions`) and `text` |
| `plugin.ui.components.list()` | Async; returns `tuple[Component, ...]` in visual order |
| `plugin.ui.components.move(component, *, before="", after="")` | Async; exactly one anchor, same slot; persists user order and returns the updated list |
| `plugin.ui.components.set_gap(component, gap_before)` | Async; save a nonnegative integer gap, or `None` to restore the declared default; returns the updated list |
| `plugin.ui.components.reset_order(slot="")` | Async; restore name order for one slot, or all; returns the updated list |
| `plugin.ui.input(title, *, default="", multiline=False, required=False)` | Async; text or None on cancel; see [UI.md](UI.md) |
| `plugin.ui.confirm(title)` | Async; bool, Cancel selected by default |
| `plugin.ui.select(title, *, items, default="")` | Async; stable choice ID or None |
| `plugin.ui.select_many(title, *, items, defaults=())` | Async; tuple of IDs or None |
| `plugin.ui.show(view)` | Async; ViewResult or None |
| `plugin.ui.open(view)` | Async context manager; `result()` and `update(view)` |
| `plugin.ui.notify(message, *, level="info")` | Async; themed session-local notice |
| `plugin.ui.shortcuts.list()` | Async; bindings, protected keys and preference error |
| `plugin.ui.shortcuts.bind(key, command, *, replace=False)` | Async; save a global key for plugin.command |
| `plugin.ui.shortcuts.unbind(key)` | Async; remove saved override |
| `handle.get()` | Async; returns the same acquired service object until the generation closes |

Only the interfaces documented here and in [UI.md](UI.md) are author APIs. Registration dictionaries, `wizolt.sdk.Action`,
`plugin.interceptors`, `plugin.services`, `Models.call`, `Context.decode`, SDK implementation helpers,
and everything under `wizolt.plugins` are host plumbing, even where Python names lack `_`.
Do not construct `Plugin`, `Models` or `Service` yourself, mutate registries or call lifecycle
methods. SDK 1 is experimental; the bundled reference matches the installed wizolt version.

**Plugin tools are not entries in the model's tool table.** `plugin.tool("remember", ...)`
does not make `remember` or `my_plugin.remember` directly callable, including as a ToolScript
tool name. The model uses `Plugin(action="list", name="my_plugin")`, then
`Plugin(action="describe", name="my_plugin", tool="remember")`, then
`Plugin(action="call", name="my_plugin", tool="remember", arguments={...})` with normal approval.
Registration does not change the model's fixed tool schema; descriptions and parameters are
disclosed on demand. A `tools.offer` interceptor may offer one directly instead, as
`my_plugin-remember` (see `tools.offer` in [INTERCEPTION.md](INTERCEPTION.md)). The offline CLI is a
testing entry point, not another model tool.

## Public values and imports

Import `Plugin`, `Context`, `Usage`, `ContextWindow`, `Text`, `Line`, `Panel`, `Event`,
`Viewport`, `Layout`, `Turn`, `ToolCounts`, `ToolActivity`, `PluginError` and `SDK_VERSION` from `wizolt.sdk`. For annotations, import `Component` from `wizolt.sdk.ui`, `ModelReply` from
`wizolt.sdk.models`, `ToolInfo` and `Directive` from `wizolt.sdk.agent` and `Service` from `wizolt.sdk.services`. `PluginError` derives from
`ValueError`; raising it gives a readable action error. Exceptions and cancellation still require
your own resource cleanup in `finally`.

All data values below are frozen dataclasses. Tuple fields must be tuples; do not mutate their
contents. Host-supplied context is a snapshot, not a live handle. The listed constructors can
also be used in plugin-owned tests.

Interactive declarations (`View`, `Document`, `Section`, `Selection`, `Form`, `Field`, `Choice`, `Action`,
`ViewResult`) are imported from `wizolt.sdk.views`; their complete tables and examples are in
[UI.md](UI.md). Do not import the unrelated internal registration `Action` from `wizolt.sdk`.

Presenter view models (`ToolCard`, `ToolSummary`, `ActivityStatus`) are imported from
`wizolt.sdk.presentation`; their sites, fields and limits are in [UI.md](UI.md).

| Type | Fields / defaults |
| --- | --- |
| `Context` | Required: `agent_id: str`, `agent_name: str`, `cwd: str`, `status: str`, `context_percent: float`, `elapsed: float`, `model: str`, `now: float`; optional: `columns: int = 80`, `usage: Usage = Usage()`, `window: ContextWindow = ContextWindow()`, `viewport: Viewport = Viewport()`, `layout: Layout \| None = None`, `turn: Turn = Turn()`, `data_dir: str = ""` |
| `Viewport` | `columns: int = 80`, `rows: int = 24` |
| `Layout` | `slot: str`, `columns: int`, `rows: int`, `gap_before: int = 0` |
| `Turn` | `tools: ToolCounts = ToolCounts()`, `active_tools: tuple[ToolActivity, ...] = ()` |
| `ToolCounts` | `started: int = 0`, `completed: int = 0`, `failed: int = 0`, `cancelled: int = 0`, `running: int = 0` |
| `ToolActivity` | `id: str`, `call_id: str`, `name: str`, `parent_id: str = ""`, `status: str = "running"`, `started_at: float = 0`, `elapsed: float = 0` |
| `Component` | `id: str`, `plugin: str`, `slot: str`, `rows: int`, `available_rows: int`, `visibility: str`, `gap_before: int = 0`, `rendered_gap: int = 0` |
| `Usage` | `calls: int = 0`, `input_tokens: int = 0`, `output_tokens: int = 0`, `cached_tokens: int = 0`, `output_rate: float = 0` |
| `ContextWindow` | `used: int = 0`, `limit: int = 0`, `budget: int = 0`, `parts: tuple[tuple[str, int], ...] = ()` |
| `Text` | `text: str`, `role: str = "text"` |
| `Line` | `spans: tuple[Text, ...] = ()`; read-only `.text` joins the spans |
| `Panel` | `rows: tuple[Text \| Line, ...] = ()`; `Panel()` occupies no space |
| `Event` | `name: str`, `context: Context`, `tool: ToolActivity \| None = None`, `reason: str = ""` |
| `ModelReply` | `text: str`, `model: str`, `usage: Usage` |
| `ToolInfo` | `name: str`, `description: str`, `kind: str` (`builtin` or `plugin`), `offered: bool` |
| `Directive` | `setting: str`, `text: str` |

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

| Callback | Async? | Models / service acquisition? | UI views, notices, settings writes? | Deadline |
| --- | --- | --- | --- | --- |
| Entry (`setup` or package entry) | No | No | No | Load: 5 seconds |
| Field / component | No | No | No | Whole refresh: 2 seconds |
| `tick` observer | Yes | No | No | Shares the whole refresh's 2 seconds |
| Lifecycle observer (session / turn / tool) | Yes | No | No | Each event's callbacks together: 1 second |
| Presenter | Yes | No | No | Tool sites 0.5 seconds, activity 0.25 |
| Command / tool handler | Yes | Yes; host services unavailable offline | Yes; views need a live TUI or trial fixtures | Live execution: 60 seconds, paused during host-owned human interaction |
| Interceptor | Yes | Yes; host services unavailable offline | `prompt.submit` and `tool.call` only | 60 seconds of its own time; `next` and human waiting do not count |

Trial `--timeout` overrides each trial call's deadline (default 5, greater than 0 and at most 60
seconds). Host model calls still have their independent 50-second/four-concurrent-call limit.
Panels allow 12 rows, 256 spans per row and 4,096 total characters per row. Prompt slots share
at most six visible rows. Single-file source is at most 256 KiB; package limits are below.
Templates allow 8,192 characters. A timed-out worker can be killed; ordinary callback errors
are reported. Health is per registration until reload: a failed observer or component is skipped
(your commands and tools keep working); a failed interceptor blocks the operations it matches; a
failed presenter falls back to the builtin rendering. Do not suppress
`asyncio.CancelledError`; it must unwind resources and release the generation.

## Layout and execution facts

Components receive `context.layout` with the remaining rows and cell width for this callback;
it is `None` for other callbacks. `context.viewport` describes the terminal. Return fewer rows
to leave room for later components; when `layout.rows == 0`, return `Panel()`. The host clips
whole rows as a final guard. Prompt slots share `min(6, max(0, viewport.rows // 4 - 2))` rows;
the scrollable `status` slot allows twelve per component.

Slots stay in visual order: above_divider → above_input → below_input. Within each slot,
components default to plugin-name order, regardless of activation order. Component IDs are
`NAME.SLOT`. `plugin.ui.components` manages saved user preferences. Moves cannot cross slots; change the component registration for that.
Disabled components retain their position and spacing. Other live agents adopt changes on reload.
Declare `gap_before` at registration or override it with `set_gap`. It separates visible
components within a slot: the first visible component has no leading gap; empty or hidden
components consume no gap. Gaps share the height budget and shrink to reserve one content row.
`Layout.rows` is the content budget after spacing; `Layout.gap_before` is the allocated gap.
`Component.rows` excludes spacing, `gap_before` is the configured request, and `rendered_gap`
is the space actually used. Resetting order preserves gaps; `set_gap(id, None)` restores the
plugin default without changing order. Do not add those empty rows yourself.

Layout calls are available inside explicit command/tool handlers only (not interceptors, renderers or observers),
and unavailable in offline trials. `visibility` is `pending layout`, `empty`, `visible`,
`clipped by height budget`, or `hidden by height budget`; allocation updates on the next refresh.

`context.turn.tools` counts admitted executions in this agent's current/latest turn, even
before a plugin is enabled. It resets at the next turn; it is not saved across resume.
Refused, blocked and invalid calls rejected before execution do not count. A script and each
nested call count separately; nested `ToolActivity.parent_id` identifies the script execution.
Execution IDs are unique within this live agent; provider `call_id` may repeat. A background
job's launch counts as one tool execution, not its entire later process lifetime.

`tool.started` fires after approval, immediately before execution. `tool.finished` includes
`event.tool.status` (`completed`, `failed`, or `cancelled`) and elapsed seconds. Nonzero Bash
exit status counts as failed. Parallel calls may overlap; each plugin's lifecycle callbacks
run serially. Events carry no tool arguments, output, or credentials. These are read-only
notifications: no veto, argument rewrite, shell-hook replacement, or automatic model call.
Use snapshots for current facts; observers are not a durable event log and receive no replay.

`session.started` fires once per open/resume, with `reason` `startup` or `resume`;
`session.finished` carries the host's closing reason. Neither callback is guaranteed after a
process crash. `turn.finished` includes the final tool counts; rendering can read them until
the next turn. An observer error disables that plugin's callbacks until reload without failing
the agent's operation.

## Packages

Single files declare `SDK_VERSION = 1`, optional `DEPENDENCIES`, and `setup(plugin)`. Open with
a module docstring written for the user, in Markdown: `/plugins` renders it as the plugin's page
when the user selects it, without running your code. Make the first paragraph a one-line
introduction, then add a `## Use` section and a small `text` block showing what it draws. Keep
notes for authors in comments, not the docstring.
For multiple files, pass a directory containing `pyproject.toml` to the same CLI commands;
`README.md` is the page and `[project] description` the one-line introduction (the entry
module's docstring is the fallback for both):

```toml
[project]
name = "my-helper"
version = "0.1.0"
description = "Shows the current branch's open review comments above the input."
dependencies = []

[tool.wizolt.plugin]
sdk = 1
entry = "my_helper:register"
```

Use `my_helper/__init__.py` or `src/my_helper/__init__.py`; ordinary relative imports and
`importlib.resources` work. The entry defaults to `my_helper:setup`; an explicit entry can select
another synchronous callable. The installed/configuration name is `my_helper` (hyphens and dots
normalize to underscores). Package metadata replaces the single-file constants.

Dependencies must be static; no build backend runs. `enable` prepares a dependency environment.
Source and resources are frozen together at load, so a running plugin keeps its helpers and
assets even after the original directory is deleted. Keep the import tree small: at most 4 MiB, 512 files,
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

Commands/tools can persist their own choices with
`saved = await plugin.settings.update({"label": "hello"}, reset=["color"])`.
Only top-level keys declared in `configure()` properties can be edited; `reset` removes a user
override and restores its default. Values can be nested finite JSON, excluding null (TOML has
no null). The complete candidate is validated in the worker before an atomic, comment-preserving
write to `[plugins.NAME]`. Concurrent edits fail explicitly; reread/retry instead of overwriting.
The returned settings are resolved and immutable. `plugin.config` remains this generation's
original snapshot: apply the returned values yourself for immediate changes, or explicitly reload.
There is no automatic config-file watching or reload. Other plugin tables cannot be targeted.

Validate/test/enable use the selected config. Live reload rereads only its `[plugins]` table,
and fixes settings for the candidate's lifetime. Failed validation preserves the active instance.
Previous versions are not kept; restore one from Git and reload. Keep the installed
package name unchanged when editing its manifest. Other running agents remain unchanged.

### Model requests

Inside a command, tool or interceptor, `await plugin.models.complete(prompt, system="", provider="", model="",
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

Intercept `context.compact` to supply or shape the summary of the history span wizolt selected
(see [INTERCEPTION.md](INTERCEPTION.md)). The span arrives as `Compaction(text, trigger)`,
including previous summary and working state. Return `Summary(text)` (non-empty, up to 16,000
characters) without calling `next` to replace the builtin strategy, or wrap what `next` returns.
Enabling it opts into automatic calls on compaction; explain any model cost before enabling. Core
history boundaries, notes and persistence remain unchanged. There is no fallback: an exception,
timeout, empty answer or copied source fails that compaction and blocks the interceptor until
reload or disable; disabling the plugin restores the builtin strategy.

```python
from wizolt.sdk.operations import Summary


def setup(plugin):
    async def summarize(context, span, next):
        reply = await plugin.models.complete(
            span.text,
            system="Summarize completed work, decisions and outstanding tasks. Return concise plain text.",
            provider=plugin.config.get("provider", ""),
        )
        return Summary(reply.text)

    plugin.intercept("context.compact", summarize)
```

Model-backed trials report unavailable host services; enable and run `/compact` to exercise the
configured model.

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
elapsed seconds, model, monotonic now, available columns, and wizolt's data_dir.
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
- `component(slot, callback, *, gap_before=0)`: sync callback `(Context) -> Panel`. Slots: `above_divider`,
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
  Arguments are checked before the handler, within its deadline. In-document references work;
  external schemas are not fetched.
  This is how a plugin gives the agent a capability. The agent's `Plugin` tool lists tools by name
  and description, describes one's parameters on request, then calls it with the user's approval
  (yolo auto-approves); arguments are validated before your handler runs. Write the description
  for a model: what it does and when to use it. Offline: `wizolt plugin test NAME --call
  tool:OPERATION --arguments '{...}'` runs a fresh instance with preview context, provided the
  handler does not need host RPC (see the trial boundaries below).
- `on(event, observer)`: async `(Event) -> None`; Event has name and context.
  Events: session.started, session.finished, turn.started, turn.finished, tool.started, tool.finished, tick. `tick` is the UI refresh
  (normally 5 Hz), not a lifecycle transition: it runs just before fields/components are read;
  collect a bounded history here, then render it without side effects. Turn
  observers have a one-second deadline, a complete refresh two seconds, live actions 60 seconds.

UI callbacks may run frequently. Read cached state only; use context.now for lightweight animation.
No callback implicitly calls an LLM. Modules are independently executed for every agent and reload;
each generation has its own worker process, including imported third-party state. Blocking or
crashing Python cannot block the host event loop. Never create unmanaged background tasks.

Open `/plugins` to manage existing installations. Creation, validation, and installation use
`wizolt plugin` commands; `/plugins` has no add-file flow. The agent's one plugin tool, `Plugin`, reloads saved choices and uses registered tools.
Keep user plugin source in `~/.wizolt/plugins/`, preferably one Git repository per plugin.
There are only built-in and user plugins, never project-scoped installations. Source directories
are not scanned for automatic execution.
Installation, enable/disable, order, gaps and shortcuts are user-level, stored in the selected
`config.toml` under `[plugin_manager]`. `wizolt plugin list` reports `config_path`.
The `installations.NAME` tables hold source path and enabled choice; `layout.SLOT` holds
`order` and `gaps`; `shortcuts.bindings` maps keys to `plugin.command`. Plugin-owned settings
stay under `[plugins.NAME]`, so strict configure schemas never see host fields. `--project` chooses
execution context only; it never scopes installation. Enabling persists the source path
for future agents in any directory. Existing agents keep their
own instances. Reload/disable wait for active turns or that plugin's invocations; unrelated
plugin invocations do not delay publication. Live `status` is `running`, `failed` or `off`;
a waiting change reports `starting`, `reloading` or `stopping`. `enabled` is the saved choice.
Set `WIZOLT_NO_PLUGINS=1` on startup to skip installed plugins and recover a broken installation.

Optional module metadata: `DEPENDENCIES = ["package>=1.0"]` (literal requirement strings).
Use PEP 508 requirements, including named direct URLs or environment markers; installer
options such as `--python`, `--target` and `--requirements` are not dependencies.
`wizolt plugin enable PATH` prepares a worker environment constrained by the host's installed
versions when the host or the saved environment cannot satisfy the declarations; an unchanged
list reuses the saved environment. Then call `Plugin(action="reload")`. No host restart is needed. The environment borrows the host
installation; it is not portable. No installation command replaces packages in the running process.

## Offline feedback

`wizolt plugin validate PATH` executes setup and reports registrations. `test` also samples fields
and components; `--event turn.finished` and `--call command:NAME --arguments '{...}'` explicitly
exercise handlers. They have real effects; validation and trials are not a filesystem sandbox.

| Check | What it verifies |
| --- | --- |
| `validate PATH` | Source loading, setup, configuration and registrations; does not execute handlers |
| `test PATH` | Fields and components with preview context; handlers run only when explicitly requested |
| `test PATH --call tool:NAME --arguments '{...}'` | Handler behavior and temporary settings writes; plugin-owned `service()` resources are available |
| `test PATH --call command:NAME --interactions answers.json` | Scripted input, selection, confirmation and window actions; see [TESTING.md](TESTING.md) |
| Reload, then `Plugin describe/call` in the session | Handlers that call `plugin.models.complete` or `plugin.ui.components.*` |

Offline trials have no live agent, provider service or component registry. Settings writes use
a disposable config, and interaction scripts can supply answers. For example,
`wizolt plugin test layout --call tool:list_components` returns
`Host services unavailable in offline trials`. This reports a missing live host, not an invalid
tool registration. Validate such plugins offline, reload them, then test their host-dependent
handlers through the session's `Plugin` gateway. A successful validation does not prove those
handlers work. Plugin-owned resources may still use real files or networks during a trial.

`test` outputs JSON, PNG and SVG paths for each component and each registered statusbar/divider
preset; presets show your sampled fields beside representative host values (`preview-model`,
the `--context-percent`, and "working" while `--status running`). Use `--width`, `--height`, `--theme`, `--status`,
`--context-percent` and `--times` to vary inputs. Frames share live clipping and theme resolution.
PNG glyph coverage depends on fonts; use `--font /path/to/font.ttf` when needed. SVG allows browser
font fallback. These are cell-layout previews, not captures of the user's terminal. No installation
is modified, and the test process is always retired, including on timeout or cancellation.
Exports use `svg_path` / `png_path`; `render_sha256` compares resolved cell content, not filenames
or raster fonts. See [TESTING.md](TESTING.md) for interaction fixtures and report fields.

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
- Check reload results: failed candidates retain the old version; `starting`, `reloading` and
  `stopping` wait for turn/invocation completion. A failure includes `traceback` and `log` tails naming the failing line, as does
  `/plugins inspect NAME` for a generation that failed later. Existing agents do not inherit each other's plugin state. Resume reads saved choices;
  ordinary Python variables do not survive a worker replacement or session restart.
- For recovery, disable through the CLI, then reload, or check out the last good commit and
  reload. `WIZOLT_NO_PLUGINS=1 wizolt` skips startup execution. Check config/project paths before
  changing preferences. Saved choices live in the config printed by `wizolt plugin list`.

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

    async def tick(self, event):
        self.rates.append(event.context.usage.output_rate)

    def draw(self, context):
        scale = max(1, max(self.rates, default=0))
        wave = "".join("▁▂▃▄▅▆▇█"[min(7, round(rate / scale * 7))] for rate in self.rates)
        return Panel((Text(f"{wave}  ~{context.usage.output_rate:.0f} tok/s", "accent"),))


def setup(plugin):
    wave = TokenWave()
    plugin.on("tick", wave.tick)
    plugin.component("below_input", wave.draw)
```

## Example: helper.py

This example needs no third-party dependency. `/helper-test` and offline `tool:self_test` exercise
a reusable service. `/helper-ask` makes a paid model request. Enabling this plugin also replaces
compaction summaries with paid model requests; summary trials report unavailable host services.

```python
from contextlib import asynccontextmanager
from io import StringIO

from wizolt.sdk.operations import Summary

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

    async def summarize(context, span, next):
        reply = await plugin.models.complete(
            span.text,
            system="Summarize decisions, completed work and remaining tasks in concise plain text.",
            provider=plugin.config["provider"],
        )
        return Summary(reply.text)

    plugin.command("helper-test", "Check the helper", check)
    plugin.command("helper-ask", "Ask a model (uses tokens)", ask)
    plugin.tool("self_test", "Check the helper offline", {"type": "object", "additionalProperties": False}, check)
    plugin.intercept("context.compact", summarize)
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

See [EXAMPLES.md](EXAMPLES.md) for complete small plugins.
