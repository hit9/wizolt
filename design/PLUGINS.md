# Plugin implementation boundaries

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

Import and setup side effects cannot be rolled back. The current SDK prohibits unmanaged tasks;
it does not yet provide persistent plugin data, managed background tasks, workflow interventions,
or agent services. Those need explicit ownership and teardown contracts before extension.

## Execution and UI

The only permanent model gateway is PluginHotReload. Standalone CLI commands perform authoring
and save installation choices; the gateway reconciles those choices into the calling agent.
This avoids dynamic tool-schema disclosure and local session IPC. Resume sees the same small
tool schema and reads saved project preferences. Human plugin commands share the core command
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

`test_plugins.py` executes examples from the packaged SDK reference. Lifecycle tests exercise
actual module loading and offline wheel installation; UI tests exercise the real selector and
renderer boundaries. Keep source paths, disabled preferences, pending state and active state
distinct in both model output and human UI.
