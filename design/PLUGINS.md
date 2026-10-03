# Plugin implementation boundaries

The three prompt-adjacent slots share one visual-order height budget. Never budget each slot
independently: several individually valid plugins could otherwise squeeze out the input.
The TUI snapshots all slot fragments once per render, and offline previews reuse that projection.
Context-category estimation runs at admission/request boundaries, not in the 5 Hz sampler.
Sampling observers may keep bounded histories; render callbacks consume them without IO.

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
CLI manager / PluginHotReload / agent lifecycle
                ↓
         SessionPlugins ── PluginCatalog (project startup preferences)
                ↓
         PluginRuntime ── PluginProcess ── worker / LoadedPlugin
                ↓
         wizolt.sdk (values and registration contracts)
```

- The SDK receives immutable facts, never `Session`, `Agent`, or terminal objects.
- Every agent has its own runtime. Each generation owns a process, including its imported
  dependencies. This is fault isolation, not a filesystem/network sandbox.
- Installation preferences are project-local, separately persisted from session snapshots.
  Existing agents are not silently reconfigured by another agent's changes.
- Bundled plugins supply disabled installation defaults. They use the same loader, SDK, and
  controls as user plugins; the terminal does not know which component is the pet.

## Replacement and cancellation

Validation constructs an unpublished generation. A failed candidate leaves active code intact.
An active turn or command/tool invocation holds a lease; replacement returns pending and publishes
after both leases end. Publication has no awaits. Each entry retains at most one previous and one
pending generation. The previous revision is source only, not a second worker; superseded
workers are retired by owned tasks that shutdown joins.

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
can admit a reverse call, with four concurrent calls and a 50-second deadline per generation.
The pipe reader dispatches calls without awaiting them. Returning/cancelling the enclosing call,
worker EOF, and retirement cancel and join its host work. No service may survive its request.
`agent/plugin_models.py` assembles a detached request session using existing wire adapters;
provider credentials stay host-side. No history, tools, hooks or persistence lease are copied.
Returned usage is independent of agent ctx/cache counters. Offline trials deliberately have no
model-service binding, so validation cannot silently incur model costs.

The model has two fixed gateways. PluginHotReload reconciles saved choices into the calling
agent; standalone CLI commands perform authoring and save those choices. `Plugin` reaches tools
that active plugins register by progressive disclosure: `list` returns names and descriptions,
`describe` one tool's parameters, `call` runs it under the usual approval. Neither reshapes the
tool block per plugin, so many plugin tools cost the prompt nothing until asked for; `Plugin`
is offered only while some active plugin has a tool, like MCP. Resume reads saved preferences. Human plugin commands share the core command
catalog for completion, collision checks and turn admission. Offline handler trials are fresh
instances with preview context, never a claim to inspect or mutate live plugin memory.

Observers and actions run in workers. Host-enforced deadlines can kill a blocked worker. A bounded
JSON protocol carries immutable context and plain values; ordinary prints are drained into a
bounded stderr tail. Cached UI snapshots refresh at 5 Hz outside terminal rendering. Width changes
update the next sample; immediate host clipping keeps stale-width snapshots inside the viewport.
Reported sample time includes IPC; it is not plugin CPU time. Never invoke user Python in paint.

The host supplies viewport dimensions, theme roles and height budgets. Repeated layout queries
reuse one projection per frame. Plugins return plain data, never ANSI or prompt-toolkit widgets.
Empty or clipped components must leave the input usable, including after multiplexer resizing.

## Dependency environments

Prepare a fresh environment constrained by host versions and persist its interpreter with the
installation. New candidate workers use that interpreter, without restarting the host. Never
pip-install into the running environment. Failure/cancel removes a candidate under construction.
The environment borrows the installed host, including editable-source paths: it is not a portable
bundle or a promise of survival after the host is removed.

## Offline feedback

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
