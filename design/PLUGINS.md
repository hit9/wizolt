# Plugin implementation boundaries

Read this with [the proposal](PLUGINS_PROPOSAL.md). The proposal is direction; the SDK and its
packaged reference define what this implementation currently exposes.

## Ownership and dependency direction

```text
CLI manager / core Plugin tool / agent lifecycle
                ↓
         SessionPlugins ── PluginCatalog (project startup preferences)
                ↓
         PluginRuntime ── LoadedPlugin (one module per generation)
                ↓
         wizolt.sdk (values and registration contracts)
```

- The SDK receives immutable facts, never `Session`, `Agent`, or terminal objects.
- Every agent has its own runtime and source-module instances. Imported dependencies still
  share Python's process-wide module cache. This is state scoping, not sandboxing.
- Installation preferences are project-local, separately persisted from session snapshots.
  Existing agents are not silently reconfigured by another agent's changes.
- Bundled plugins supply disabled installation defaults. They use the same loader, SDK, and
  controls as user plugins; the terminal does not know which component is the pet.

## Replacement and cancellation

Validation constructs an unpublished generation. A failed candidate leaves active code intact.
An active turn or command/tool invocation holds a lease; replacement returns pending and publishes
after both leases end. Publication has no awaits. Each entry retains at most one previous and one
pending generation; superseded module identities are removed immediately.

The turn guard remains held while completion observers run. Never clear the engine's active task
before awaiting observers: another turn could otherwise overlap the old generation. Cancellation
must release invocation leases in `finally` and must not leave pending replacement stranded.

Import and setup side effects cannot be rolled back. The current SDK prohibits unmanaged tasks;
it does not yet provide persistent plugin data, managed background tasks, workflow interventions,
or agent services. Those need explicit ownership and teardown contracts before extension.

## Execution and UI

Commands and tools are explicit async invocations. The core Plugin tool owns model approval and
uses the existing runner. A stable gateway avoids changing the tool schema on every reload.
Validate individual operation schemas and arguments independently of provider enforcement.

Observers are cooperative async callbacks. UI callbacks are synchronous pure projections: clocks
can animate cached state, but rendering cannot initiate work. Python timeouts cannot preempt a
blocking callback. Timing counters report actual callback work; they are not isolation guarantees.

The host supplies viewport dimensions, theme roles and height budgets. Repeated layout queries
reuse one projection per frame. Plugins return plain data, never ANSI or prompt-toolkit widgets.
Empty or clipped components must leave the input usable, including after multiplexer resizing.

## Dependency environments

In-process plugins cannot isolate incompatible versions with separate search paths. Resolve the
enabled set against host version constraints into a fresh environment; never pip-install into
the running process. Validate there before returning an explicit launch command. Failure/cancel
removes the candidate. The environment borrows the installed host, including editable-source
paths, so it is not a portable bundle or a promise of survival after that host is removed.

## Verification

`test_plugins.py` executes examples from the packaged SDK reference. Lifecycle tests exercise
actual module loading and offline wheel installation; UI tests exercise the real selector and
renderer boundaries. Keep source paths, disabled preferences, pending state and active state
distinct in both model output and human UI.
