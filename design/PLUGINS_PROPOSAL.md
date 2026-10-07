# Plugins: make wizolt yours

Status: proposal for review. The API names and paths below are illustrative, not existing interfaces.

The experimental DIY foundation is now implemented; see [implementation boundaries](PLUGINS.md)
for the shipped subset and the packaged `plugin-workshop` SDK reference for actual API names.
Interventions, managed background tasks, persistent plugin data and deeper UI sites remain future work.
The [operations and presentation design](PLUGIN_INTERCEPTION.md) refines interventions and
UI replacements; it takes precedence over illustrative APIs below for that future work.

## Goal

Let users shape wizolt through natural language, configuration, and small Python plugins.
Personal DIY comes first; distribution and a marketplace are secondary.

Notifications stay inside wizolt. Desktop notifications are out of scope.

A user should be able to say: “Show my test failures in the statusbar,” and have wizolt create,
validate, activate, and later revise the extension. The user can inspect the change and undo it.

Keep the mental model small:

- **Configuration** describes appearance and simple conditions.
- **Python** implements behavior and interaction.
- **The host** owns execution, state boundaries, terminal rendering, and resource cleanup.

## Architecture

```text
User request
    |
Plugin skill ---- SDK references and runnable examples
    |
Existing editing tools + core plugin management tool
    |
Python plugin
    |
Public SDK: registrations, events, interventions, host services
    |
Internal adapters
    |
Agent / tools / session / UI implementations
```

Keep one plugin concept and one registration entry point. A simple plugin is a Python file;
a larger plugin is a package with a manifest, resources, dependencies, and tests. Define one
metadata schema with defaults for the single-file form, rather than separate plugin kinds.

```python
def setup(app):
    app.fields.register("review.pending", pending_reviews)
    app.commands.register("review", open_reviews)
    app.events.subscribe("turn.finished", summarize_review)
```

`app` exposes bounded services, not the application object graph. Callback contexts identify
the owning agent explicitly. A registration scope owns every registered handler and resource.

Proposed module responsibilities:

| Boundary | Responsibility |
| --- | --- |
| `wizolt.sdk` | Public protocols, value types, component descriptions, errors |
| `wizolt.plugins` | Discovery, validation, instances, versions, registrations and lifecycle |
| Feature adapters | Connect semantic extension points to agent, tool and UI implementations |
| CLI and management tool | Present and invoke the same plugin management service |

Public contracts do not import CLI assembly or internal engine objects. Application lifecycle
assembles the adapters and manager. Add abstractions for real extension boundaries, not a
wrapper around every existing method. Migrate built-ins to shared interfaces where useful;
do not require a wholesale rewrite to ship plugins.

## The extension contract

Expose four complementary operations:

| Operation | Examples | Contract |
| --- | --- | --- |
| Register a capability | Tool, command, field, component | Host validates and owns registration |
| Observe an event | Turn finished, tool completed | Immutable facts; return values do not change execution |
| Intervene in a workflow | Inspect or transform a tool request | Typed decisions; host awaits and validates the result |
| Call a host service | Query an agent, send input, store plugin data | Explicit scope; existing approval and cancellation rules apply |

Publish semantic boundaries such as “before tool execution,” not positions inside current
functions. Each public extension point documents ordering, cancellation, error behavior,
reentrancy, and data visibility.

Observers receive bounded notifications and cannot delay a turn indefinitely. Delivery and
overflow behavior must be explicit; observers needing durable delivery require a separate
documented contract. Interventions execute in a configured deterministic order. A failed
execution check reports an error and prevents the checked operation; ordinary observer errors
are reported without converting a successful turn into a failure.

Namespace registrations by plugin identity. Reject collisions instead of silently replacing
another plugin. Exclusive UI replacements require an explicit user choice. New tools use the
existing tool runner, including approvals, cancellation, results, and transcript integration.

### Stable and internal

- **Stable SDK:** documented behavior, versioned contracts, and contract tests.
- **Experimental SDK:** explicitly marked interfaces that may change.
- **Internal:** engine classes, mutable session objects, queues, storage formats, and renderer details.

Plugins declare their supported SDK major version; incompatible plugins fail validation with
an actionable message. Stability applies to semantics, not just Python signatures. Internal
refactors remain free to change behind adapters.

## UI customization

Use three reusable concepts: **fields**, **components**, and **slots**.

A plugin field such as `review.pending` can appear in existing format strings. A component
describes structured content and interaction. A slot places it in the host UI.

| Surface | Intended customization |
| --- | --- |
| Statusbar and divider | Fields, conditions, segments, colors, progress and timing |
| `/status` | Additional sections, statistics and visualizations |
| Input surroundings | Hints, completion sources and attachment summaries |
| Transcript and details | Tool presentations, notifications and expandable results |
| Selectors and dialogs | Lists, previews, confirmations and forms |
| Optional panels | Tasks, tests and background activity |

First-release UI scope: statusbar/divider fields and formats, extra `/status` sections,
plugin-owned command dialogs, tool details, in-app notices, and a bounded area above the input.
Later extensions can customize message presentation, agent-picker rows/previews, and docked
panels. These are proposed public sites, not permission to replace arbitrary internal widgets.
The input editor, terminal renderer, focus routing, and approval decisions remain host-owned;
custom presentation does not change the underlying message or authorization result.

Claude Code provides a useful precedent: its mods expose named render sites for messages,
tools, prompt hints, working indicators, and panes, with shared UI elements. Its statusline
also supports scripts receiving structured data. Borrow the explicit sites and shared components;
retain wizolt's format strings for simple customization. See the official
[render-site reference](https://code.claude.com/docs/en/plugins/mods/reference#render-sites)
and [statusline guide](https://code.claude.com/docs/en/statusline).

The host owns layout, focus, scrolling, resize, refresh, and ANSI output. Plugins return
structured content and use theme roles by default. They must not write directly to the terminal.
Rendering reads prepared values; network calls and expensive computation run outside rendering.
New field sources extend the existing format mechanism rather than introducing another template
language. General fields should work across compatible surfaces without surface-specific hooks.

## State and execution

Plugin definitions and immutable resources may be shared. Mutable state defaults to the owning
agent; workspace-wide state requires an explicit scope. Persistent plugin data is namespaced
separately from core session data, with plugin-owned schema versions. Core state changes go
through services, never direct mutation of `Session` or `Agent`.

Trusted local Python plugins run in separate managed processes from the first release. Host
deadlines can terminate blocking callbacks; imports and native crashes cannot take down the host.
This is not a filesystem/network sandbox. UI callbacks remain short, pure projections sampled
outside terminal painting. Managed background work needs an explicit ownership API.

### Runtime and call frequency

Load and register once per plugin generation; execute callbacks when their subscribed event or
operation occurs. Routine callbacks run Python, not an LLM request. Model use is explicit and
must have a trigger, cancellation path, and budget.

```text
Load → register → event or user action → update scoped state → refresh visible UI
                     (repeat as needed)
Disable or retire → stop callbacks → drain owned work → release resources
```

For a test-status plugin, test events update cached counts. A statusbar field reads those counts;
rendering never reruns tests or invokes a model.

| Capability | Trigger and expected work |
| --- | --- |
| Command or tool | Explicit invocation; perform the requested operation |
| Intervention | Each matching operation; await its decision within the documented budget |
| Completion observer | Matching tool or turn completion; update state or emit an in-app notice |
| UI field or component | Visible refresh; read prepared values and build lightweight content |
| Stream observer | Potentially high frequency; batch deltas while preserving required order |
| Background monitor | Change notification or explicit interval; avoid overlapping polls |

Subscribe with filters rather than sending every event to every plugin. Coalesce UI invalidations
and avoid rendering hidden components; required background work may continue while hidden.
Bound queues and concurrent work. Latest-value updates can be coalesced, but operation decisions
and completion events need their documented delivery rules. Report slow callbacks, failures, and
queue overflow through plugin inspection. Do not promise a fixed refresh rate before measuring
the real terminal workloads.

### Third-party dependencies

Support declared third-party Python dependencies through the plugin manager. Use one standard
dependency declaration in package metadata and resolve a reproducible environment; the plugin
skill should use the manager rather than run arbitrary pip installs into wizolt's environment.

Prepare and validate a fresh worker environment without changing the running host installation.
The first implementation borrows the host SDK installation and constrains host package versions;
conflicting requirements fail explicitly. Hot reload starts a candidate with the saved interpreter,
then replaces the old worker at a safe boundary. No host restart is needed. Dependency installation
can execute third-party build code and follows the same trust boundary as plugin activation.

Authoring uses standalone `wizolt plugin` commands. The only permanent model tool is the small
`PluginHotReload` gateway for applying saved choices to the current agent. This avoids progressive
tool disclosure, resume-specific tool state, and a local socket protocol. Trial reports include
errors, logs and component preview images before activation.

## Loading, reload, and recovery

Provide local-path loading, `/plugins`, and one core management tool with actions such as
inspect, validate, enable, disable and reload. Installation records a plugin source;
activation executes it. Workspace discovery alone must not execute newly found Python code.
The user request determines authorization; avoid asking again for each already-authorized step.

Reload replaces an instance instead of mutating live Python objects:

1. Validate a candidate version and stage its registrations without publishing them.
2. On failure, keep the current version active and report the error.
3. Wait for a safe boundary, then switch registrations and retire the old instance.
4. Cancel and drain retired background work, and release subscriptions and resources.

Active turns pin the relevant plugin generation. A reload requested during a turn returns
`pending`; it must not wait on its own caller. Prevent new old-generation work while draining.
Check duplicate registrations and lifecycle operations for idempotency.

Preparation must avoid external side effects. Import-time effects in trusted Python cannot be
rolled back by the manager, so staged validation is not a general transaction. Persistent data
survives reload; migrations must preserve the promised rollback path or explicitly require a
backup. Dependency changes and modules that cannot unload cleanly may require restart.

Report installed and active versions separately, with pending operations and errors. Previous
versions are recovered from the user's Git history, not kept by wizolt. Core management remains available when a plugin fails; startup
must offer a way to disable plugins before importing them.

## LLM-assisted DIY

Ship one built-in skill covering creation, installation, reload, and management; no separate
installation is needed. Make its short description discoverable to the LLM through normal skill
discovery, and load instructions and references only when needed. Natural-language DIY requests
can select it without requiring the user to invoke a special command. SDK references and runnable
examples ship with the matching wizolt version and are verified in CI.

The workflow is: inspect existing configuration and plugins, choose the smallest solution,
edit with existing tools, validate and test, activate or reload, then inspect actual host state.
The skill teaches the method; management code enforces lifecycle correctness. The LLM must
distinguish “files written,” “pending activation,” and “active.”

Provide a short development loop: validate, preview or exercise, reload, inspect, repair.
Validation errors identify the plugin, registration, offending field, and expected contract.
Offer sample-data previews for UI components, event fixtures for observers, and controlled tool
invocation tests. Reuse the real SDK and renderer in these checks; do not create a second mock
implementation. Preview and test execution must state which effects run and which services are
substituted; “preview” is not a sandbox for arbitrary Python. Core management remains usable
without loading the skill, and the skill never substitutes its judgment for activation results.

## Self-improvement direction

Start with user-directed customization. Later, use execution evidence and feedback to propose
improvements, compare candidate versions, and keep successful changes within a user-defined budget.
The improvement strategy can itself be a plugin.

```text
Feedback → candidate → isolated evaluation → comparison → activation → feedback
                                               |
                                          retain rollback
```

Writing a plugin is self-extension; demonstrated improvement requires independent evidence.
Recursive improvement additionally changes how improvements are proposed and selected. Keep
baseline evaluations outside the candidate's control, use held-out tasks, and compare cost and
regressions as well as success. UI preference changes may need human judgment instead of a score.
Core modifications remain ordinary reviewed code changes, not live patches to the running host.

Research references: [Darwin Gödel Machine](https://sakana.ai/dgm/),
[AIDE²](https://arxiv.org/abs/2609.26457), and
[MetaSkill-Evolve](https://arxiv.org/abs/2607.05297). These motivate the direction; they do not
establish that an individual wizolt installation will improve indefinitely.

## Delivery and acceptance

1. **DIY foundation:** local Python loading, lifecycle management, commands, tools, fields,
   basic UI components, a small documented event set, and the bundled skill.
2. **Deeper composition:** selected workflow interventions, context and agent services,
   and additional UI slots, driven by real plugins.
3. **Measured improvement:** version comparisons, evaluation workloads, promotion and rollback;
   then experiment with improving the improvement strategy itself.

First-release acceptance:

- Create a field plugin and use it in both statusbar and divider without changing core code.
- Create a tool plugin whose approval, cancellation, and output use existing host behavior.
- Verify main/subagent state isolation and explicitly shared state.
- Failed reload preserves the old plugin; repeated reload leaves no duplicate callbacks or tasks.
- Reload from an active turn returns pending without deadlock; disabling cleans up owned resources.
- Test theme and resize behavior through the host renderer, including real terminal acceptance.
- Measure startup, input latency, rendering, and memory with zero, a few, and many plugins.
- Verify filtered delivery, coalesced refresh, cached field reads, and bounded background work;
  rendering and routine callbacks must not implicitly request a model response.
- Verify SDK examples in CI, built-in skill discovery and on-demand loading, actionable validation
  errors, and previews using the real rendering contract. Confirm reported activation state.
- Resolve compatible third-party dependencies without changing the running environment; report
  conflicts, restart requirements, and restore the prior environment on activation failure.

Before implementation, finalize the initial event/intervention set, loading paths and metadata,
the smallest useful component vocabulary, and the exact safe-boundary policy for UI resources.
Keep these decisions in the implementation plan rather than expanding the user-facing concepts.
