# Plugin implementation boundaries

Installations, enabled choices, layout and shortcuts live in `config.toml` under
`plugin_manager`, separate from plugin-owned `plugins.NAME` settings. Dependency environments
remain under `<data_dir>/plugins/.state/`. Source files can live anywhere. Never use
the session/project directory to scope user customization. Each agent still owns its live
workers and adopts changed preferences on reload; `Context.cwd` remains agent-local.

The three prompt-adjacent slots share one visual-order height budget. Never budget each slot
independently: several individually valid plugins could otherwise squeeze out the input.
The TUI snapshots all slot fragments once per render, and offline previews reuse that projection.
Pure row projection has a bounded cache keyed by immutable row content, terminal width and
`Theme.key()`. Never key it only by plugin name or row position: sampling and focused-agent
theme changes must be visible immediately. Cached fragments are immutable; callers own copies.
Context-category estimation runs at admission/request boundaries, not in the 5 Hz sampler.
Sampling observers may keep bounded histories; render callbacks consume them without IO.

`LayoutPreferences` owns per-slot user preferences; `LayoutBudget` allocates once in visual order.
The runtime samples each generation once, fusing that with its first allocated component, and
publishes one completed layout. Paint only reads that cache. A revision counter discards a
pass invalidated by resize, movement or generation replacement. The TUI's final clip still
protects input while the next asynchronous sample catches up. SDK management uses explicit
host operations; the optional bundled layout plugin owns no policy or persistence. Never
derive display order from process admission or dictionary insertion order.

Spacing belongs to the receiving component (`gap_before`), not a pair of neighbors. The host
persists overrides separately from plugin settings and retains them while disabled. Order resets
must preserve spacing; clearing a gap override restores the plugin declaration. Allocation
subtracts the gap before invoking the renderer and only consumes it for nonempty content.
The first visible component in each slot has no leading gap. `/plugins` manages lifecycle;
layout operations are exposed through the SDK and optional layout plugin.

The runner owns `TurnActivity`, via its agent-local plugin runtime even with no plugins enabled.
Observe the admitted execution boundary shared by serial and parallel paths, not PreToolUse
(before approval) or model request count. Nested scripts propagate a ContextVar through their
executor and gateway; every execution has its own ID and optional parent ID. Settle facts
before notifying observers, including cancellation. Snapshots permit late activation without
event replay. Plugin lifecycle observers are read-only and serial per generation; they cannot
veto execution or replace shell hooks. Tool payloads/results never cross this observation seam.

Appearance contributions are plain metadata. The CLI injects its validator before activation,
using the same theme compiler and format parser as user configuration. Each layout owns its
preset catalog; `Theme` projects only the focused agent's palettes. Never publish a background
agent's colors into the global renderer. Disabling a contribution falls back without rewriting
the user's configured choice, so re-enabling can restore it.

Package admission freezes regular files and resources before launching the worker. The parent
owns the extracted directory, including cleanup after native exits; never let the worker own
its only cleanup handle. Source rollback keeps the complete snapshot and settings, not just the
entry module. A fresh worker provides module isolation for both single-file and package plugins.
`pyproject.toml` is metadata only: admission must not invoke a build backend or install into the
host interpreter. Descriptor-based bounded reads reject symlinks and special files before code
execution.

Read this with [the proposal](PLUGINS_PROPOSAL.md). The proposal is direction; the SDK and its
packaged reference define what this implementation currently exposes.

## Ownership and dependency direction

```text
CLI manager / Plugin tool / agent lifecycle
                ↓
         SessionPlugins ── PluginCatalog (user startup preferences)
                ↓
         PluginRuntime ── PluginProcess ── worker / LoadedPlugin
                ↓
         wizolt.sdk (values and registration contracts)
```

- The SDK receives immutable facts, never `Session`, `Agent`, or terminal objects.
- Every agent has its own runtime. Each generation owns a process, including its imported
  dependencies. This is fault isolation, not a filesystem/network sandbox.
- Installation preferences are user-profile-wide, persisted in the chosen config, never in
  session snapshots. Config writes hold a nonblocking cross-process lease and preserve comments,
  mode and symlink targets; contention is an explicit retryable failure, never a UI wait.
  Existing agents are not silently reconfigured by another agent's changes.
- Config is the sole preference source; startup and queries never migrate or rewrite it.
  Runtime cache files carry no installation choices. Generated interpreters stay under local runtime data;
  config stores only their immutable environment IDs. Preparation precedes the atomic config
  switch, so failed saves cannot change which interpreter an installation uses. Explicit custom
  interpreter paths remain the user's responsibility. A copied profile requires reinstalling
  dependencies; never silently fall back to host Python when an environment is absent.
- Every bundled plugin is off by default; only the user's explicit enable turns one on. Wizolt
  never adds features, plugins or functions the user has not explicitly asked for. Never ship a
  bundled plugin enabled, enable one on upgrade, or make a host feature depend on one being
  enabled. They use the same loader, SDK, and controls as user plugins; the terminal does
  not know which component is the pet.

## Replacement and cancellation

Validation constructs an unpublished generation. A failed candidate leaves active code intact.
An active turn freezes the whole registry; command/tool and summarizer invocations pin only their
own generation. An idle plugin can therefore publish while another plugin is awaiting input.
Replacement returns pending until its applicable leases end. Publication has no awaits. Each
entry retains at most one previous and one pending generation. The previous revision retains
one `Revision` containing source, settings and its interpreter path,
not a second worker; superseded workers are retired by owned tasks that shutdown joins. Never
resolve a rollback's interpreter from today's installation preferences: dependencies may have
changed since that revision ran. Reload must also retain the installed identity, which owns
settings and tool namespaces, even if a package's manifest was renamed on disk.

The turn guard remains held while completion observers run. Never clear the engine's active task
before awaiting observers: another turn could otherwise overlap the old generation. Cancellation
must release invocation leases in `finally` and must not leave pending replacement stranded.

Import and setup side effects cannot be rolled back. `plugin.service` registers a lazy async
context manager, acquired only by explicit actions. Samples and validation cannot acquire SDK
services. One handle serializes acquisition; failed acquisition is retryable. Retirement cancels
and joins callbacks, then closes resources in reverse acquisition order. The parent grants one
second for cooperative teardown before killing the process group. Connections and their tasks
belong inside this scope; detached processes and unmanaged threads remain unsupported.

## Execution and UI

Host service RPC is bidirectional but request-scoped. Only an outstanding explicit invocation
can admit a reverse call, with four concurrent calls and a 50-second service deadline.
An interactive view is the sole deadline exception: human response time pauses the invocation's
remaining execution budget. Closing the view resumes it; a blocked worker can then be killed.
The pipe reader dispatches calls without awaiting them. Returning/cancelling the enclosing call,
worker EOF, and retirement cancel and join its host work. No service may survive its request.
`agent/plugin_models.py` assembles a detached request session using existing wire adapters;
provider credentials stay host-side. No history, tools, hooks or persistence lease are copied.
Returned usage is independent of agent ctx/cache counters. Offline trials deliberately have no
model-service binding, so validation cannot silently incur model costs.

The model has one fixed, always-present gateway, `Plugin`. `reload` reconciles saved choices
into the calling agent; standalone CLI commands perform authoring and save those choices. Tools
that active plugins register are disclosed progressively: `list` returns names and descriptions,
`describe` one tool's parameters, `call` runs it under the usual approval. The schema never
changes as plugins come and go, so enabling one cannot invalidate the provider's prompt cache,
and many plugin tools cost nothing until asked for. Merging reload into it costs about 27 more
tokens per request than a reload-only tool, and saves about 116 once any plugin has a tool
(schema JSON at four characters per token). Resume reads saved preferences. Human plugin commands share the core command
catalog for completion, collision checks and turn admission. Offline handler trials are fresh
instances with preview context, never a claim to inspect or mutate live plugin memory.

Observers and actions run in workers. Tool argument validation belongs there too: schema regexes
can take unbounded time, and reference resolution must never initiate implicit network IO.
Host-enforced deadlines can kill a blocked worker. A bounded
JSON protocol carries immutable context and plain values; ordinary prints are drained into a
bounded stderr tail. Cached UI snapshots refresh at 5 Hz outside terminal rendering. Width changes
update the next sample; immediate host clipping keeps stale-width snapshots inside the viewport.
Reported sample time includes IPC; it is not plugin CPU time. Never invoke user Python in paint.

The host supplies viewport dimensions, theme roles and height budgets. Repeated layout queries
reuse one projection per frame. Plugins return plain data, never ANSI or prompt-toolkit widgets.
Empty or clipped components must leave the input usable, including after multiplexer resizing.

Interactive views follow the same data boundary (see [Plugin views](PLUGIN_VIEWS.md)). The host
owns keys, validation, focus, queues and modal lifetime; no worker callback runs on the UI thread.
Views belong to their invocation and agent. Background agents request attention instead of
stealing focus. Cancellation, disable and successful replacement dismiss owned views. Retiring
generations cannot open new views: a callback handling dismissal must not indefinitely renew
its human-wait lease and prevent replacement. Saved
shortcuts live separately from installation records and target named commands through normal
command admission. `--no-plugins` bypasses startup loading without rewriting user preferences;
enable and reload stay unavailable for that process so recovery cannot accidentally reload a
broken plugin.

## Dependency environments

Prepare a fresh environment constrained by host versions and persist its environment ID with the
installation. New candidate workers use that interpreter, without restarting the host. Never
pip-install into the running environment. Failure/cancel removes a candidate under construction.
The environment borrows the installed host, including editable-source paths: it is not a portable
bundle or a promise of survival after the host is removed.

## Offline feedback

SDK settings updates validate a complete candidate in the worker, then compare the raw read
snapshot under the host config lease. The host derives the destination from the bound plugin
owner, never an RPC argument. Patches preserve untouched keys/comments; reset removes overrides.
No write mutates a generation's frozen settings or schedules reload. Only command/tool requests
admit the service, not observers or automatic compaction. Trusted Python is not sandboxed.

Trials bind the same settings writer to a disposable config and a scripted UI adapter to the
real dialog states. Fixtures match ordered semantic declarations; generated IDs are only used
internally for open/update/close routing. Missing answers fail, never wait for stdin. Rendering
stays in the UI exporter; the plugin execution layer does not import dialog states. Cell hashes
exclude export paths and runtime IDs and must not be advertised as raster equality.

`ViewResult.resolve` is the single semantic answer boundary. Widgets collect drafts, and
`DialogState.answer` validates before applying them; scripted input never touches buffers or
selection indexes. Both adapters use current drafts as defaults after live updates. Input
source must not change required-field rules, selection order or the default action.

Trials use the same source loader, worker protocol and panel validation as live generations.
Explicit events/actions execute before sampling; fixed context/time inputs make frames repeatable.
The UI layer exports the real PluginView projection into text, SVG and PNG. Themes with transparent
backgrounds need a documented preview canvas; PNG font coverage remains environment-dependent.
Reports include exact fragments and image paths. No screenshot service, desktop capture or
installation mutation belongs in this path. Each export gets a fresh directory, avoiding accidental
replacement of an author's previous evidence.

## Verification

A summarizer is a single-writer strategy, not a history transform. Admission rejects competing
strategies before publication, including pending generations. The compactor supplies the selected
flattened span and existing working state; this intentionally gives up main-prefix cache reuse.
Plugins return bounded text only. The core retains split/keep, protocol pairing, echo validation,
checkpoint application and persistence. Both automatic and manual compaction pin the generation;
cancellation propagates, while other failures mark it unhealthy and fall back to the builtin path.

`test_plugins.py` executes examples from the packaged SDK reference. Lifecycle tests exercise
actual module loading and offline wheel installation; UI tests exercise the real selector and
renderer boundaries. Keep source paths, disabled preferences, pending state and active state
distinct in both model output and human UI.
