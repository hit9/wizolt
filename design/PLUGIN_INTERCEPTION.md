# Plugin operations and presentation

Status: implemented through the shipped operations and presenter sites; view models are public
in `wizolt.sdk.presentation`. [Implementation boundaries](PLUGINS.md) and the
[packaged SDK reference](../wizolt/skill/builtin/plugin-workshop/SDK.md) describe shipped
behavior. What is not implemented as written is listed under
[Shipped deviations](#shipped-deviations), and the acceptance evidence this document requires
but does not yet have is listed under [Remaining work](#remaining-work).

## Goal and decisions

Let a user change how wizolt works, not only add things beside it. Personal DIY through the
LLM remains the goal: small Python plugins, a small mental model, and room for substantial
behavior changes. No second “mod” package type, marketplace requirement, or configuration DSL.

Keep three complementary extension mechanisms:

| Mechanism | Meaning | Existing foundation |
| --- | --- | --- |
| Observe | Receive an immutable fact; return values cannot change execution | `plugin.on` |
| Intercept | Wrap a semantic operation; change its input, handle it, or wrap its result | New `plugin.intercept` |
| Register | Add a named capability or implementation | Commands, plugin tools, components, fields, themes, services |

These are concepts, not three new generic facade objects. Keep the existing useful registration
methods. Do not turn paint, telemetry, or every internal function into middleware.

Retain process isolation, typed declarations, the fixed `Plugin` model gateway, user-level
preferences, and agent-local live state. No compatibility adapter is required for experimental
APIs, but changes must update the bundled authoring skill, trials and this user's plugins.

## One interception contract

Illustrative API; operation types and names become public only with a working adapter and tests:

```python
def setup(plugin):
    plugin.intercept("tool.call", normalize_bash, match={"tool": "Bash"})

async def normalize_bash(context, call, next):
    revised = call.replace(arguments=normalize(call.arguments))
    result = await next(revised)
    return result
```

- Inputs and results are immutable, typed SDK values, with bounded JSON representations.
  `replace` creates a candidate; the host validates every transition, not only initial input.
- `await next(input)` invokes the rest of the frozen chain and then the host implementation.
  The handler can run code before and after it, or return a valid result without calling it.
- `next` is an invocation-scoped, single-use continuation. Calling it twice, concurrently,
  after return, or from another invocation fails. It cannot be saved or transferred.
- Expected refusal uses the operation's typed rejection result. Exceptions mean a failed
  interceptor, not a successful refusal. An operation cannot deny execution retroactively.
- A handler returning a result may replace model-visible content where that boundary permits
  it. It cannot forge host execution status, identities, actual usage or approval receipts.
- Declarative matchers select stable input fields. No host-side execution of plugin predicates.
  Each handler matches the input it receives after earlier transformations.

Registrations use one stable ID per plugin/operation in the initial contract. A plugin composes
its own helpers in ordinary Python; do not build an internal routing framework for it. `match`
is a host-side IPC prefilter, not a second routing language. Multiple cases belong in the handler.
Absent or nonmatching registrations add no worker invocation or payload serialization.

## Boundaries to expose

| Operation | Input and result | Host-owned invariant |
| --- | --- | --- |
| `prompt.submit` | Submitted text/attachment references → accepted input or explicit refusal | Preserve original input for history; slash commands use command dispatch first |
| `context.compose` | Named request-context blocks → revised blocks | Change the request projection, not durable messages; protect protocol structure |
| `model.request` | Provider-neutral request → complete response | Resolve credentials in host; account actual requests; validate response/tool-call structure |
| `tool.call` | Tool identity and arguments → tool result or refusal | Validate final arguments and authorize actual execution; preserve execution receipts |
| `context.compact` | Selected conversation span and working state → summary | Host owns retained messages, pairing, checkpoint validation and persistence |

Adapters live at existing semantic boundaries, not inside a particular provider, CLI command,
or tool class. ToolScript calls and ordinary tool calls must reach the same tool adapter.
UI and headless submission must reach the same prompt adapter. Model adapters stay above the
wire encoders; plugins do not manipulate authentication headers or provider SDK objects.
Transformed prompt text remains model input; never redispatch it as a slash command.

### Input admission: once per submitted item

Admission order is: core slash-command dispatch → retain admitted image references →
`prompt.submit` → `UserPromptSubmit` shell hook on effective text → skill/mention expansion →
stage effective message and expansion records. Core commands, including recovery commands, never
enter middleware. A skill command that becomes model input does enter it before expansion.

This boundary covers the initial input, queued follow-ups when claimed, and model-origin child
inbox inputs. The latter retain their non-command origin; they cannot become executable slash
commands. Generated skill/mention messages and shell-hook context do not re-enter admission.
Both original and effective text are recorded, with original input used for user history/display
and effective input used for the model. Rewriting occurs before creating the model message.

Give each queued item an admission receipt, reused across request retries and release/reclaim.
Do not repeat plugin side effects or mention discovery whenever `prepare_request` is rebuilt.
A refused follow-up is acknowledged once and reported; it is not silently queued again.
Attachments are retained before asynchronous interception. Handlers may omit existing references
but cannot invent asset paths or bytes; retained references follow normal settlement on success,
refusal, cancellation and failed startup. Vision observation follows effective admission.

### Context blocks: an explicit new projection model

Named blocks do not exist as editable values today; `breakdown()` is only an estimator. Extract
them from the producers in `ContextManager.model_header`, not by parsing rendered strings:

| Block, in default order | Editing contract |
| --- | --- |
| `system` | Replace instruction text; language/attribution directives stay host-owned |
| `environment` | Read-only facts |
| `instructions` | Replace project/user instruction text for this request |
| `skills`, `mcp` | Read-only capability indexes; do not invent executable capabilities |
| `plugin:<name>:<id>` | Own namespaced blocks, stable ID order within interceptor order |
| `conversation` | Read-only history/current-turn view, with intact protocol and attachments |

Plugin blocks occupy one insertion region after the header and before conversation. No arbitrary
message indexes, reordering of core blocks, or edits to durable messages. Do not truncate input
silently to fit RPC: use bounded views/explicit size errors. The final projection is validated and
budgeted before sending; recomposition after compaction must also pass the budget check.

Prefix edits genuinely cost cache reuse. The existing tools-derived `prompt_cache_key` does not
make changed system text cacheable. Document that trade-off in the SDK; keep host-generated IDs
and ordering deterministic and expose changed block IDs/token estimates in request diagnostics.

### Model requests: all purposes, one logical boundary

Introduce a logical request adapter above wire encoding and below the caller's request assembly.
Do not blindly wrap both `ModelClient.request` and `api_request`: that would intercept some
requests twice. Route these callers explicitly through the one adapter:

| Caller | Host-owned purpose/reason | Accounting |
| --- | --- | --- |
| Agent step | `turn` / `normal` | Existing main-agent usage |
| Image-fallback resend | `turn` / `image_fallback` | Main usage; preceding observations remain vision usage |
| Textual-tool correction | `turn` / `tool_correction` | Main usage; correction remains a real history message |
| Vision observation | `vision` | Existing `Billing.VISION` |
| Builtin compaction model call | `compaction` | Existing `Billing.COMPACTION` |
| `plugin.models.complete` | `plugin` | Detached usage returned to plugin, not main context/cache counters |

`purpose`, billing owner, origin agent and request IDs cannot be rewritten. A matcher such as
`match={"purpose": "turn"}` restricts a router without creating a separate API. Provider/model/
effort overrides are request-local, validated against host capabilities and token budgets; no
session-default mutation. Image fallback and error reporting use the effective route. Credentials
remain host-side. `context.compact` wraps summary strategy; `model.request` wraps an actual model
call made by that strategy, if any. This is intentional nesting, not double interception.

Context assembly belongs to the caller: auxiliary requests do not acquire the main conversation,
tools or `context.compose` implicitly. PluginModels keeps its detached session but receives the
originating chain/ancestry explicitly. Routing it through the adapter is new work, not shipped
behavior. Structural history/tool fields are read-only in `model.request`; instruction editing
belongs in context composition, so a later request wrapper cannot bypass those restrictions.

### Retries and preview semantics

The chain wraps one logical request, including existing bounded transport retries inside `next`.
Those retries reuse the transformed input and do not rerun handlers. Record individual provider
attempts under that logical request, including real usage; one `next` is not one network attempt.
The host retries only classified provider failures, never plugin errors or completed tool work.

A manual `ModelRequestRetry` cancels/settles the old chain, rebuilds the request as today, and
starts a new chain with `retry_of` linking the previous request. Handlers may therefore run again:
document that their own side effects must be idempotent or explicitly keyed to these IDs. Semantic
corrections and image-fallback resends are new logical requests with the corresponding reason;
accepted follow-up admission and paid vision observations are not repeated.

Use a complete, bounded response contract now. The wire readers already stream through
`UiHooks.on_stream` and return buffered messages/tool calls; a pull-stream SDK is not a thin
adapter and is outside this contract. Add `response="preserve"|"replace"` to model interception,
default `preserve`: preserve handlers may change request routing but must return the downstream
response unchanged (host validates), or an explicit pre-execution refusal. Replacement/synthetic
response handlers must declare `replace`.

If the admitted request chain contains a response-replacing registration, suppress content preview
for that entire logical request and show progress only; publish the validated final response.
Conservatively include registrations that could match after earlier rewrites when choosing that
mode. Otherwise keep existing push streaming and never round-trip tokens through workers.
Discard attempt-local preview on retry; never revise transcript text already committed or splice
failed-attempt text into an accepted answer. Preserve mode is the cheap, streaming router path;
replace mode trades live preview for response transformation. The protocol retains bounded JSON
results and explicit oversized-result errors, rather than quietly truncating them.

A future streaming-transform contract would need frame ordering, backpressure, partial-output
failure, iterator ownership and wire-adapter changes designed together. Do not advertise it now.

The existing summarizer becomes an implementation of `context.compact`, rather than a parallel
execution framework. Middleware failure is explicit; do not retain the summarizer's implicit
fallback as a second failure policy. The user can disable it and retry with the builtin strategy.
Consequently, a compaction interception failure during automatic `prepare_request` fails that
turn when it cannot fit; it does not discard history or silently continue over budget. Explicit
`/compact` fails without applying a checkpoint. Builtin strategy's own validated provider-retry
policy remains separate from bypassing a failed plugin.

New boundaries need a concrete use case, an owning adapter, a typed contract and contract tests.
This design is not permission to export internal engine methods or a generic `host.invoke`.

## Approval and truthful results

```text
admit operation → interceptor A → interceptor B → validate final input
                                                → existing policy/hooks/approval
                                                → core execution
                ← wrap result  ← wrap result    ← recorded actual outcome
```

Final validation and authorization cannot themselves be replaced by a plugin. A rewritten
`ls` becoming `rm` must be approved as `rm`, not reuse the earlier input's approval. Permission
hook decisions use the existing runner policy; interception adds no competing approval policy.
Cache hits and synthetic results are recorded as plugin-produced, without claiming the builtin
ran. Producing a result does not give a plugin an SDK route to execute an unapproved core tool.
For a core execution, `PreToolUse`, policy/approval and execution observers see the final call.
Synthetic results and pre-execution refusals emit no `tool.started`/`tool.finished` and do not
increment execution counts. They still settle the requested tool call and its provenance record.
Existing post-tool hooks apply to actual core outcomes before middleware wraps the delivered
result; their feedback is preserved as a separate host-owned record, not removable by a wrapper.
The interception seam belongs in the runner path shared by ordinary calls and `_NestedGateway`
from ToolScript, before final tool construction/planning/approval. Rebuild argument-dependent
plans after rewriting; never approve a stale Edit diff. Do not intercept both gateway and runner.

Keep the actual execution receipt separately from the delivered result: a plugin error after
a successful Edit does not undo that edit. The transcript must retain the actual outcome and
identify the failed wrapper. Observation events report execution facts, not fabricated success
from a rewritten response. Model-delivered content may differ and carries its provenance.

This requires durable data, not only a live UI flag. Extend the existing tool record/message
metadata and `SessionSnapshotCodec` with one bounded operation receipt: operation ID, original
and effective call, origin (`core`/plugin ID), core state (`not_run`/`started`/`completed`/
`failed`/`interrupted`/`unknown`), actual-result reference, delivered-result reference and wrapper
failure. Use existing stored assets for large content, not duplicate payloads or a second journal.
Checkpoint actual completion before awaiting post-execution wrappers, then checkpoint delivery.
A crash between side effect and checkpoint remains uncertain, not exactly-once execution: resume
must show `unknown` rather than retry. Replay/inspect render both actual outcome and wrapper
failure without executing plugins; provider projection omits internal receipt metadata.
Prompt admission receipts and request `retry_of` metadata use the same semantic snapshot boundary.

Plugins remain trusted Python running with the user's OS permissions. These guarantees govern
host APIs; worker isolation is not a sandbox against direct file/network/process access.

## Ordering, ownership and reload

- Default order is plugin name, with the first matching interceptor outermost. Loading,
  disabling/re-enabling and restarting cannot change the order accidentally.
- One user-level ordered plugin list overrides that default for interception; unlisted names
  follow in name order. No priorities, dependency graph, tiers or per-event ordering language.
- Persist as `[plugin_manager.interception] order = [...]`, alongside but separate from
  `[plugin_manager.layout]`. Manage through `wizolt plugin order list/move/reset`; the LLM uses
  Bash, then existing `Plugin(action="reload")` applies saved choices in the live agent.
  Do not add gateway actions or require a managing plugin. A release could legitimately revise
  the fixed schema, but sorting does not justify more permanent per-request tool tokens.
- Each operation snapshots its chain. Existing turn leases freeze registrations for that turn;
  work outside a turn pins the chain's participating generations for its own lifetime.
  Publication and order changes affect the next eligible invocation, never half a call.
- Every operation belongs to one agent and root invocation. Parent and child agents have separate
  state, chains and receipts. Resume reconstructs registrations from user preferences; it never
  restores suspended continuations or silently replays interrupted operations.

## Cross-process execution without a second runtime

Reuse the existing bidirectional request transport and invocation ownership. A worker's
`next(input)` is a reverse RPC to the host, bound to an opaque continuation token. The host
validates token ownership, input and single-use state, executes the remaining chain, and returns
its result. Plugins never receive Python references to host objects or other workers.

Required changes are explicit: add an `intercept` worker operation; extend
`PluginProcess.admits_host_call` with per-operation service permissions; generalize
`RequestDeadline.pause` beyond `ui.views.show`. `next` uses a reserved continuation message
class on the same transport, not a normal host-service call: the four-call permit limit and
50-second service deadline must not truncate it. Ordinary host services retain those bounds.
One continuation per handler and bounded chain/nesting depth bound this reserved traffic.

The transport reader must keep dispatching while callbacks await reverse calls. No generation
mutex or invocation semaphore may be held across `next()` while blocking downstream work that
needs that worker. Handler tasks can interleave at awaits; plugin-owned mutable state needs
ordinary local synchronization. SDK docs must explicitly warn against holding such a lock over
`next` or a host service that can re-enter the plugin.
Nested work that exceeds admission limits fails explicitly; it must not queue behind a suspended
ancestor whose return depends on it. Continuations do not consume ordinary host-service permits
while waiting on the chain they own.

Host-service requests carry invocation ancestry and distinguish core descendants from explicit
plugin auxiliary requests. Core descendants, including ToolScript's nested calls and compaction's
model request, enter their full matching chains: being inside a ToolScript must not bypass a Bash
interceptor. Reentrant handler tasks are therefore required, not an optional optimization.
Explicit plugin auxiliary requests skip registrations already active in their ancestry, avoiding
self-recursion when a request interceptor calls `plugin.models.complete`. Other registrations
still apply. Auxiliary usage stays separate from main context/cache counters. Nesting and
in-flight work are bounded; unrelated invocations are unaffected. `next` advances the current
chain; it never restarts that same operation at the first handler.
Today a reverse call only has a worker-local parent request ID. Add a host-owned invocation scope
with root ID, parent ID, origin agent, ordered active registration IDs and cancellation ownership.
Propagate it explicitly across worker RPC, nested ToolScript dispatch and PluginModels; never
trust a worker-supplied ancestry list or rely solely on ContextVar inheritance. The host restores
ancestry after nested completion; skipped registrations are visible in diagnostics. These are
ephemeral routing facts, not restored executable continuations in session snapshots.

## Deadlines and failures

One host-owned invocation scope accounts for execution, human interaction and downstream waits.
Each interceptor has 60 seconds of its own elapsed execution budget across its before/after
sections; all plugins share the same policy, not custom per-plugin timers. With N handlers the
maximum plugin-owned latency is N times that budget, plus independently bounded downstream work.

- Plugin execution budget pauses only while awaiting registered `next` or
  host-owned human interaction. Arbitrary plugin sleeps and network waits still consume it.
  `next` must be awaited in its handler, not detached into a background task. The host ends the
  pause when downstream work settles, before waiting for the worker to consume its response.
- Downstream operations retain their own deadlines. The outer invocation remains cancellable;
  delegated waiting is not permission to create an immortal background task.
- Cancellation revokes continuations, closes provider responses/views and joins owned work. Cooperative
  cleanup has a grace period, then the host kills an unresponsive worker process group.
- An interceptor error/timeout stops the operation. Never automatically bypass it or repeat core
  execution: it may enforce policy, or core execution may already have happened.

Do not overload today's generation-wide `unhealthy` flag with two opposite meanings. Track
registration health separately from process availability; retain unavailable registrations in
the effective manifest so the host can apply the following rules without invoking dead code:

| Failure | Current operation | Subsequent matching work until reload/disable |
| --- | --- | --- |
| Observer | Report; core outcome unchanged | Skip observer |
| Field/component/presenter | Report; builtin fallback or omit additive content | Skip failed presentation |
| Interceptor, including compaction | Fail with actual-outcome receipt | Block that registration's matching operations |
| Worker exit/kill | Cancel all its outstanding work | Apply each registration's rule above |

Never remove an interceptor silently because it became unhealthy. If its matcher can be reached
only after a rewrite, matching/blocking still happens at its position in the chain. Other unrelated
plugins and the input loop remain usable.

Only `prompt.submit` and `tool.call` interceptors may open views, through existing invocation-
owned UI routing. A subagent uses its own runtime/TUI and attention queue, never the parent's
dialog state. Background agents request attention without taking focus. Headless mode returns
interaction-unavailable, never approval. Model requests, context composition and compaction
cannot open surprise dialogs. Settings/layout writes remain restricted to explicit plugin
command/tool handlers; interception does not gain every host service merely by being admitted.

Recovery cannot depend on a working model or chain. Core `/plugins` disable/reload runs outside
`prompt.submit` and all execution interception; disable cancels/joins affected work rather than
waiting for a poisoned turn to finish. Existing `--no-plugins` remains an unconditional startup
escape hatch. A blocked `model.request` makes model-issued Plugin reload unavailable, so that
gateway is never the only recovery path.

## UI: register presentation, do not intercept paint

Retain components, fields, format strings, themes, commands and declarative views. Add named
presentation sites for tool-call cards, result summaries and activity indicators. A presenter
receives a stable public view model and returns an existing declaration, not ANSI or widgets.

Illustrative registration: `plugin.presenter("tool.result", render, match={"tool": "Bash"})`.
Overlapping replacements require an explicit user choice; do not resolve by last-load-wins or
silently compose incompatible views. Settings expose the effective selection through the same
host management API. Additive components retain their existing ordering and spacing contract.

Compute presentations asynchronously when their source changes; paint consumes bounded cached
snapshots. Cache identity includes agent, tool-call/execution ID, generation, source revision,
theme and viewport. Live previews/viewers may update. For new `LogBlock` output, choose one ready
snapshot at emission, otherwise emit the builtin rendering; a late result cannot rewrite printed
native scrollback. Resume replay uses recorded core receipts and never needs the old presenter.
Reload affects future output and newly opened viewers. Host-owned approval facts, focus,
cancellation and recovery keys remain
outside replacement. Persistent terminal splits are not required by this design.

## Layers and scope

| Layer | Responsibility |
| --- | --- |
| `sdk` | Operation/result values, handler protocols, author-facing continuation |
| `plugins` | Registrations, ordered chains, scoped continuations, transport, leases and diagnostics |
| Agent/tool adapters | Map semantic operations; enforce final policy, accounting and persistence |
| UI adapters | Public view models, presentation cache, rendering and interaction |
| CLI / Plugin gateway | CLI manages order; existing gateway reload applies it; inspect effective registrations/failures |

Reuse lifecycle and service machinery; do not create a parallel scheduler, permission engine,
event bus, history store, or mod loader. Keep new modules at these responsibility boundaries,
not one module/class per event. Empty chains use the normal core path without IPC or copies.

Host-managed plugin storage, detached background agents and arbitrary window layouts are not
prerequisites. Add them only with concrete contracts; do not smuggle them into middleware scope.

## Examples and acceptance

| User request | Building blocks |
| --- | --- |
| Show context weather and token-rate waves | Existing snapshots, observers, components; no middleware |
| Expand my task shorthand before sending | `prompt.submit`; original input remains in history |
| Route selected requests to another provider | `model.request`; host resolves credentials and usage |
| Inspect risky Bash arguments and ask me | `tool.call` plus an existing view; final core approval still applies |
| Show test output as a navigable report | Tool-result presenter plus existing fullscreen view |
| Use my local summarizer | `context.compact`; host still owns checkpoint correctness |

Required evidence before publishing these APIs:

- Real worker tests prove chain nesting, typed rejection, final-input approval, valid synthetic
  results and rejection of forged receipts or reused/foreign/expired continuations.
- Deterministic barriers exercise cancellation before/during/after `next`, worker death after a
  real side effect, reload with a pinned request, and nested host calls without deadlock.
- Request tests cover every purpose/call site, retry counts, original/effective route accounting,
  preserve-mode streaming and replace-mode preview suppression. No per-token worker IPC.
- Context tests protect durable history, tool pairing, attachments and cache-stable prefixes.
  Ordering tests cover enable order, restart, reload and independent agent state.
- Offline trials add `--operations FIXTURE.json`: ordered entries match operation/purpose/input
  and supply `next` with a canned result or typed failure. Missing/unused entries fail rather
  than reaching real core execution. Compose with existing `--interactions`, not a new UI format.
  Trials use the same chain executor and validation with a scripted core adapter;
  they record effective inputs, execution receipts and delivered results. No live network or
  core side effects unless explicitly testing integration.
- Benchmark empty/nonmatching chains, several no-op interceptors, buffered response transforms
  and unchanged UI painting. Branch comparisons belong under `benchmarks/results/`; after merging
  to master, remeasure comparably, investigate regressions and update the release baseline as
  required by [the benchmark policy](../benchmarks/README.md#baseline-updates).
- Persistence tests crash at execution/delivery checkpoints, then resume/replay without plugins;
  receipts remain truthful and no uncertain tool is replayed. Test command-line recovery with a
  broken prompt/model/compaction interceptor and both foreground/background agents.
- Package the full author contract in the builtin skill references. Keep `docs/plugins.md` about
  user wishes and examples, not continuation internals. Test those author examples as code.

Reference: [Claude Code Mods](https://code.claude.com/docs/en/plugins/mods/reference#the-hook-function)
uses a `next(event)` middleware contract. We borrow composable semantic operations, while
retaining wizolt's worker isolation and host-owned terminal model.

## Shipped deviations

Implemented as designed unless named here. Known gaps carried from the implementation:

- Trials apply the shared chain rules, but not adapter-specific checks: attachment retention,
  offered tool names, and context block placement are not covered by `--operations` fixtures.
- Recovery tests cover the foreground agent only, not background agents.
- A crash between a tool's side effect and its checkpoint resumes as a model-visible notice
  (`unknown`); that turn's tool batch is lost, so history holds no settled tool call for it.
- Presenter tool sites await their panel briefly (0.5s) inside the runner's finish path rather
  than computing purely from cached snapshots: a settled call has no earlier revision to key a
  cache on. The activity site does use the cached-snapshot model described above.
- `plugin.presenter` registrations live beside interceptors but get no host services, matching
  the failure rule for fields/components/presenters (skip and fall back), not interceptors.

## Remaining work

The contracts above ship; the acceptance evidence this document requires does not yet exist.
Each item names what to produce and where it belongs.

- **Benchmarks.** Measure the empty chain, a non-matching chain, several no-op interceptors, a
  buffered response transform, and unchanged UI painting. Keep branch comparisons under
  `benchmarks/results/` without replacing the reference baseline, per
  [the benchmark policy](../benchmarks/README.md#baseline-updates).
- **Multiplexer acceptance.** Rerun `uv run pytest -m tmux` and `uv run pytest -m zellij` after
  the interception and presenter changes (0.45.1, the CI pin, or a compatible later version).
  The activity site touches the running region, so this run is also the evidence for unchanged
  painting.
- **User documentation.** `docs/plugins.md` still describes plugins without the interception or
  presenter wishes. Add the examples from [Examples and acceptance](#examples-and-acceptance)
  in user terms, and keep continuation internals in the packaged skill references.
- **Background-agent recovery.** `tests/test_plugin_recovery_interception.py` covers the
  foreground agent only. A background agent must recover the same way: core `/plugins disable`
  and reload stay outside every chain.
- **Trial adapter checks.** `--operations` fixtures apply the shared chain rules but not the
  adapter-specific ones. Cover attachment retention, offered tool names, and the placement of
  plugin context blocks.
