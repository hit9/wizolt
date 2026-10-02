# Subagents

Split a task among agents working in parallel. Each has its own conversation, plan, notes and
usage statistics. **Files are shared**, so give agents separate files to edit. More agents use
more model tokens.

## Approve and configure

Ask for a concrete split, for example:

> Start api-review to review the API and ui-review to review the UI. Keep both read-only.

Approve each task, even with `--yolo`. **v** opens the task; **c** changes that child's provider,
model, effort or API before it starts.

<div class="term-shot" role="img" aria-label="A subagent creation approval with its name, provider, model, shared workspace and task, followed by approve, view, config and refuse actions."><span class="fs-tool">  Subagent  spawn api-review</span><span>    │ agent      api-review</span><span>    │ provider   deepseek</span><span>    │ model      deepseek-chat</span><span>    │ files      shared with all agents</span><span class="fs-dim">    ├ agent task</span><span>    │ Review the API. Do not edit files.</span><span> </span><span class="fs-approve">  Approve · View agent task (v) · Config (c) · Refuse</span></div>

Set defaults in `~/.wizolt/config.toml`:

```toml
[subagent]
provider = "deepseek"  # an existing [provider.deepseek] entry
```

Optional `model`, `reasoning` and `api` override that provider's settings. Without a provider
override, the parent's current provider is used. Empty fields inherit.

**Old config still works:** `[worker]` is accepted; `[subagent]` wins per field when both exist.
Approval choices win over defaults. Changes affect new children, including forked skills;
existing and resumed children keep their saved settings.

## Select and inspect

Run **`/agents`**. Move to preview; press Enter to switch conversations.

<div class="term-shot" role="img" aria-label="The agents picker with aligned names, states and context usage. api-review is highlighted with a green activity dot, and its task and live reply appear in a bordered preview."><span class="fs-title">  Agents</span><span> </span><span>  1. ● main        completed          ctx  10% (current)</span><span class="fs-selected">  2. <span class="fs-i fs-ok">●</span> api-review  running            ctx  24%          </span><span>  3. <span class="fs-i fs-approve">●</span> ui-review   waiting for input  ctx  18%          </span><span> </span><span class="fs-rule">  ┌─ api-review ────────────────────────────────────────┐</span><span>  │                                                     │</span><span>  │ Task                                                │</span><span>  │   Review the API. Do not edit files.                │</span><span>  │                                                     │</span><span>  │ Live                                                │</span><span>  │   Checking validation and error handling…           │</span><span>  │                                                     │</span><span class="fs-rule">  └─────────────────────────────────────────────────────┘</span><span> </span><span class="fs-hint">  ↑/↓ j/k move · Enter open · x stop · X stop now · Esc back</span></div>

Green dots breathe while an agent works. Warning-colored dots need input; other dots stay still.

| Key | In the picker |
|---|---|
| ↑/↓ or j/k | Move and preview |
| Enter | Open the highlighted conversation |
| x | Stop it after confirmation |
| Shift+X | Stop it immediately |
| Esc | Return without switching |

The input box, history, statusbar, `/status` and `/diff` follow the selected agent.
Use `/provider`, `/model` or `/reason` there to change its settings.

### Notice when an agent needs you

Background agents keep working. Notices identify completion, failure and input waits without
switching your conversation. Colors follow your theme; the statusbar gives `wait N` priority.

<div class="term-shot" role="img" aria-label="A successful subagent completion and a warning that another child needs input, followed by a statusbar with a highlighted wait count."><span class="fs-ok">  ✓ subagent [api-review] completed</span><span> </span><span class="fs-approve"><b>  ! subagent [ui-review] needs input</b></span><span class="fs-dim">    Open /agents and select this agent to respond.</span><span> </span><span><span class="fs-i fs-approve"><b>[wait 1]</b></span> [main] [agents 3 · run 0] deepseek/deepseek-chat</span></div>

## Add input and stop work

| Key | In an agent's running conversation |
|---|---|
| Enter | Send a follow-up to the current turn |
| Tab | Queue the text for its next turn |
| Ctrl-C | Interrupt this agent's current work |

Stopping keeps the conversation and file changes. After an interruption or failure, queued
inputs pause; send another message to continue. `/agents stop-all` interrupts the whole group.

Exit saves the conversations. **Resume main** to restore its children; queued work waits for
new input. Main's input history includes earlier sessions; each child's history stays separate.

## Agent limit

```toml
[runtime]
max_subagents = 3
```

The default is **3**, excluding main; allowed values are **0–32**. Completed children and forked
skills still count. Stopping does not free a slot: reuse a child's conversation for more work.
Change the limit from main with `/set runtime.max_subagents NUMBER`; check it with `/status`.

## Model tools

The model uses `Subagent` to **spawn**, **send**, **list**, **wait** and **stop**. Creating agents
returns immediately. Waiting defaults to **3 minutes**, up to **10 minutes** per call
(`timeout=600`); `timeout=0` checks immediately. A timeout does not stop the child.

Tasks must be self-contained: children receive system and project guidance, not the parent's
conversation or notes. See [forked skills](skills.md) for tasks that run in their own child.
Creating or sending tasks requires interactive approval; piped input cannot approve child tools.
