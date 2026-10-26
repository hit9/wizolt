# Plugin issues and improvement options

Status: discussion draft, 2026-10-19; item 1 stage 1 implemented 2026-10-25 (read-only metadata,
target-aware confirmation, serial plugin calls kept), and item 3's first two steps implemented
2026-10-26 (discovery from cached capabilities, tools-only lazy park/resume with an idle sweep;
commands still imply a resident worker). Item 2 remains open. This note records gaps found while
comparing wizolt's plugin tool contract with Claude Code, Codex, Gemini CLI and Cursor. It is a
maintainer note, not a user guide; user-facing behavior belongs in `docs/plugins.md`.

## Current model

- Every enabled plugin runs in a worker owned by the agent. The SDK contract says a plugin is
  independently instantiated per agent (`wizolt/sdk/__init__.py:221`).
- Plugin tools are registered through `Plugin.tool()` (`wizolt/sdk/__init__.py:357`) and described
  to the host by the worker's capabilities (`wizolt/plugins/worker.py:126`).
- The model normally calls them through the `Plugin` gateway: `list`, `describe`, `call` and
  `reload`. Directly offered `plugin-tool` names are folded back into the same gateway call by
  `PluginTool.resident_call()` (`wizolt/tools/plugin.py:88`).
- `Tool.needs_confirmation()` is the default approval policy. The base returns `True`
  (`wizolt/tools/base.py:191`); the runner consults it before prompting and when deciding whether
  a call may run in parallel (`wizolt/agent/runner.py:759`, `wizolt/agent/runner.py:864`).
- `PluginTool.needs_confirmation()` currently prompts for `reload` and `call`, and not for
  `list`/`describe` (`wizolt/tools/plugin.py:39`). It does not look at the target tool.
- `PluginTool.MUTATES` is `True`. It participates in edit barriers and serial batch planning,
  separately from the confirmation decision.
- The worker dispatches incoming requests as independent asyncio tasks
  (`wizolt/plugins/worker.py:290`), so concurrent calls can already interleave inside one plugin.

## 1. Read-only plugin tools

### Symptom

A plugin that exposes a search or inspection tool cannot say that the tool is read-only. Every
`Plugin call` prompts, and read-only calls cannot enter a parallel segment. The only escape is to
enable yolo mode, which is much broader than the intended allowance.

This is not just an approval bug. Directly offered tools are renamed to the gateway before the
runner decides, so a decision based on `call.name == "Plugin"` is also wrong for those calls.

### Why the current contract causes it

`Plugin.tool()` has no mutability metadata; the worker capability has none; the host `Operation`
has none. The host therefore has only one conservative answer for every target tool.

### Proposed shape

Add `read_only: bool = False` along the existing metadata path:

1. `Plugin.tool(..., *, read_only=False)` and `Action.read_only` in `wizolt/sdk/__init__.py`.
2. The worker includes `read_only` in each tool's capability
   (`wizolt/plugins/worker.py:126`).
3. `Operation` decodes it with a default of `False` for older plugins
   (`wizolt/plugins/protocol.py:22`).
4. `PluginTool.needs_confirmation()` remains `True` for `reload`, looks up the target operation
   for `call`, and returns `False` only when that operation is read-only. `list` and `describe`
   stay non-confirming. An unknown target remains confirming.

The lookup must use the target `plugin` and `tool` from the gateway payload. Looking only at the
gateway call name, or at the plugin as a whole, would either over-prompt or under-prompt.

### Scope decision

Stage 1 should only remove the confirmation. It should not quietly make plugin calls parallel:
`parallel_safe()` currently derives its answer from `needs_confirmation()`
(`wizolt/agent/runner.py:759`), so a `False` there would also permit concurrency. Until the
plugin concurrency contract is documented, keep an explicit `PluginTool` exclusion in
`parallel_safe()` (or equivalent) and remove it in stage 2. This keeps the first change small and
preserves today's serial behavior.

Stage 2 can allow read-only plugin calls to join parallel segments. It needs a clear author
contract: read-only handlers may overlap inside one worker, so they must not mutate shared plugin
state or assume serialized callbacks. A search that hands work to `to_thread` is fine; a handler
that appends to an in-memory list is not.

### Safety and trust

`read_only` is self-declared, like MCP's `readOnlyHint`. The host cannot verify it. This does not
grant a plugin new permissions: plugin code already runs with the user's filesystem and network
access. It only changes the default prompt, so it is appropriate only for trusted plugins.

Precedence must stay unchanged. A PreToolUse hook's `ask` still forces a prompt, `deny` still
blocks, and `reload` still always confirms (`wizolt/agent/runner.py:864`). The flag must not
become a way to bypass hook policy.

The approval decision and the executed handler must be the same generation. Today that holds
because `runtime._publish` refuses to swap the registry while the turn is active, so a mid-batch
reload stays pending and cannot answer a read-only approval with newly reloaded mutating code.
This is an invariant, not an accident: a runner that bypasses the turn lifecycle re-opens the
window and must keep the same guarantee or confirm calls itself
(`wizolt/tools/plugin.py`, the `invoke` site). A mechanical pin (passing the approved generation
into `invoke`) remains a stage-2 option.

### Tests

- Capability round trip carries `read_only`; an older payload defaults to `False`.
- A read-only tool call does not confirm; a mutating tool in the same plugin still does.
- A directly offered read-only tool, after gateway rewriting, also does not confirm.
- An unknown tool and a missing plugin runtime remain confirming.
- `reload` remains confirming.
- Stage 2: two read-only calls are admitted to the parallel path; the stage 1 guard is gone.

### Touch points

- `wizolt/sdk/__init__.py` (`Action`, `Plugin.tool`)
- `wizolt/plugins/protocol.py` (`Operation`)
- `wizolt/plugins/worker.py` (capability description)
- `wizolt/tools/plugin.py` (`needs_confirmation`)
- `wizolt/agent/runner.py` (stage 1 parallel guard)
- `tests/test_plugins.py`, `tests/test_plugin_tool_offer.py`, `docs/plugins.md`

## 2. Per-tool persistent approval

### Symptom

Approval is per call. A user who trusts one plugin tool must either approve it repeatedly or
enable yolo mode for every tool in the session. Mainstream agents expose a rule between those
extremes: allow/deny/ask per tool, or a pre-approved MCP tool list.

### Options

| Option | Benefit | Cost |
| --- | --- | --- |
| Host-owned config allowlist | Durable, inspectable, explicit | Survives plugin changes unless tied to a fingerprint |
| Session-only "allow for this session" | Small and safe; no stale policy | Must be repeated every launch |
| Fingerprint-bound approval | Invalidates when code or schema changes | More state and migration work |

A config allowlist is the recommended first step: a host-owned section keyed by `plugin.tool`,
not plugin-owned `plugins.NAME` settings. It should be explicit, with no wildcard, and should
stop matching when the plugin is disabled or the tool disappears. `reload` should keep asking.

Fingerprint-bound approval is the stronger long-term answer: hash the plugin source and the
tool schema, and require a new approval when either changes. It can be added after the allowlist
has a stable storage and display model.

### Open questions

- Is the allowlist global, or can it differ per agent?
- Does a change to the tool's JSON schema invalidate approval, or only a code change?
- Where is the effective policy shown and revoked? `/plugins` is the natural place, but it is
  currently a lifecycle surface, not a permission surface.
- Should a plugin be able to suggest a default, while the host still owns the decision?

### Constraint

Persistent approval must not bypass PreToolUse `ask`/`deny`, and must not turn into a global
yolo switch. The rule applies after the hook result and only to the named `plugin.tool` pair.

## 3. Worker lifecycle and cost

### Symptom

A worker is started per enabled plugin per agent. With several agents and several plugins, idle
workers cost processes, memory and reload time even when no plugin tool is called. This is the
opposite trade-off from MCP, where a server process is normally shared per server/session rather
than duplicated per agent.

### What must not change casually

The SDK states that a plugin is independently instantiated per agent. That isolation protects
mutable plugin state, fields, views and reload generations. Sharing one plugin instance across
agents would violate the documented contract and the state-ownership rules.

### Options

- Keep per-agent isolation, measure actual process/memory cost, then add lazy start or idle
  shutdown. This preserves the contract.
- Share a process across agents while keeping separate registration scopes. This is a larger
  runtime redesign and needs evidence that state can be isolated safely.
- Pool only stateless tools. This needs a contract stronger than `read_only`; read-only does not
  imply stateless.

Recommendation: keep isolation for now. Collect measurements before changing lifecycle. This
item is noted, not agreed.

Implemented 2026-10-26, first two steps: discovery (`Plugin list`/`describe`, `/plugins`, direct
offers) answers from cached capabilities without a live worker; a tools-only plugin — nothing
registered besides tools, themes and presets — parks its worker right after validation, the first
call restarts it, and an idle sweep parks it again after five unused minutes (`runtime.py`,
`_park`/`_resume`/`_sweep`). A crash never marks `idle`, so a dead running generation stays
"failed" and undiscoverable. Commands still imply a resident worker; process sharing remains
rejected. Cold start measured at roughly a tenth of a second (full interpreter plus load
handshake) on a warm cache: `benchmarks/results/linux-arm64-py314-plugin-worker-park-resume.json`.
A park drops process memory between calls — plugins that cache in memory start over after the
idle window; durable state belongs in callbacks or files. Review follow-ups (2026-10-26): the
sampler still samples generations with a `tick` observer (it fires on the sample), the sweep
never parks a crashed generation, a restart that cannot happen leaves the generation "failed",
`PluginProcess.close` is idempotent for the park-then-retire double close, and a resume lets the
parked worker finish teardown and refuses to start one after the runtime closed.

## Recommended order

1. Read-only metadata and target-aware confirmation; keep plugin calls serial (stage 1).
2. Persistent per-tool allowlist with explicit revocation.
3. Parallel read-only plugin calls after the concurrency contract and tests (stage 2).
4. Worker lifecycle only after measurement shows the cost is material.

## Non-goals

- Sandboxing plugin code. Plugins run with the user's permissions by design.
- A plugin marketplace or distribution format.
- Sharing conversation state, fields or views across agents.
- Treating `read_only` as a security boundary. It is an approval hint, not enforcement.

## References

- `wizolt/sdk/__init__.py:212` (`Action`)
- `wizolt/sdk/__init__.py:221` (`Plugin` per-agent scope)
- `wizolt/sdk/__init__.py:357` (`Plugin.tool`)
- `wizolt/plugins/protocol.py:22` (`Operation`)
- `wizolt/plugins/worker.py:126` (tool capabilities)
- `wizolt/plugins/worker.py:290` (concurrent worker dispatch)
- `wizolt/tools/base.py:191` (default confirmation)
- `wizolt/tools/plugin.py:39` (`PluginTool.needs_confirmation`)
- `wizolt/tools/plugin.py:88` (direct offer to gateway)
- `wizolt/agent/runner.py:759` (`parallel_safe`)
- `wizolt/agent/runner.py:864` (confirmation and hook precedence)
