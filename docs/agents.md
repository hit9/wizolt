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

<div class="term-shot" role="img" aria-label="The agents picker shows each agent's status, turn duration and context usage. Archived agents have muted dots and appear last. The selected agent has a bordered task and live reply preview."><span class="fs-title">  Agents</span><span> </span><span>  1. ● main        completed          12s      ctx  10% (current)</span><span class="fs-selected">  2. <span class="fs-i fs-ok">●</span> api-review  running            3m12s    ctx  24%          </span><span>  3. <span class="fs-i fs-approve">●</span> ui-review   waiting for input  2m08s    ctx  18%          </span><span class="fs-dim">  4. ● old-review  archived</span><span> </span><span class="fs-rule">  ┌─ api-review · running · 3m12s ───────────────────────┐</span><span>  │                                                     │</span><span>  │ Task                                                │</span><span>  │   Review the API. Do not edit files.                 │</span><span>  │                                                     │</span><span>  │ Live                                                │</span><span>  │   Checking validation and error handling…            │</span><span>  │                                                     │</span><span class="fs-rule">  └─────────────────────────────────────────────────────┘</span><span> </span><span class="fs-hint">  ↑/↓ j/k move · Enter open · x stop · X stop now · d archive · Esc back</span></div>

Green dots breathe while an agent works. Warning-colored dots need input; other dots stay still.
The preview shows this turn's elapsed time; wider lists show it alongside the status.
The time freezes when the turn ends and survives resume.

| Key | In the picker |
|---|---|
| ↑/↓ or j/k | Move and preview |
| Enter | Open the highlighted conversation |
| x | Stop it after confirmation |
| Shift+X | Stop it immediately |
| d | Archive it and its children after confirmation; free their slots |
| Esc | Return without switching |

The input box, history, statusbar and `/status` follow the selected agent.
Use `/provider`, `/model` or `/reason` there to change its settings.

Archived agents appear last, marked **archived** with a still, muted dot. Move to preview;
Enter opens the conversation in the same view as a live agent, with its statusbar showing
**archived**. The input line is read-only: Enter there opens `/agents`. Archiving keeps
conversations and file changes. If you are viewing an agent when it is archived, you stay on
it and its view becomes read-only.

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
skills still count. Stopping does not free a slot. Archive finished agents with **d** in `/agents`
to make room for new tasks with fresh conversations.
Change the limit from main with `/set runtime.max_subagents NUMBER`; check it with `/status`.

## Model tools

The model uses one `Subagent` tool with **spawn**, **send**, **list**, **inspect**, **report**, **wait**, **stop** and **archive** actions. Creating agents
returns immediately. Waiting defaults to **3 minutes**, up to **10 minutes** per call
(`timeout=600`); `timeout=0` checks immediately. A timeout does not stop the child.

Use `{"action": "wait"}` to wait for any of your currently running direct children.
If none are running, it returns `[]` immediately. Already completed agents are skipped.

Select specific agents with `{"action": "wait", "agent_ids": ["A", "B"]}`.
When **any** target stops running, the call returns all currently settled targets and their results.
If a target continues with queued input, waiting continues until that work settles too.
Other agents keep working. An already settled target returns
immediately; remove returned IDs before waiting again. A timeout returns `[]`.
For one agent, use a one-item `agent_ids` list. Waiting requires no approval.

Add `"mode": "all"` to wait until **every** target has settled instead, for example after
starting several reviewers. On timeout it returns every target, still-running ones marked
`running`. The transcript shows which kind of wait it was (`wait any` or `wait all`).

The parent receives each direct child's latest completed, failed or interrupted result before
its next model request, even while doing other work. Notifications include up to **1000 characters**.
`wait` returns each answer's first **4,000 characters** and marks the rest `truncated`; **report**
returns one child's answer in full, live or archived. Results already returned by `wait` or `inspect`
are not announced again. This does not start a new parent turn or add user input to history.

**inspect** reads an active or archived agent without approval: its task, plan, model settings,
duration, eight recent messages, four recent tool results and current tool batch. Long text is
clipped, and a clipped result says to read the answer with **report**; partial streaming text is
not included. **list** is a status overview — state, context use and errors, no answer text — and
also includes archived agents so the model can find their IDs. None of these actions starts work
or changes the selected conversation.

Main can request **archive**. Approval lists the target, its descendants, their status and the
slots freed. Archiving stops their work and discards queued inputs while retaining history and
file changes. If the branch changes during approval, main must request approval again.

| Action | Human approval |
|---|---|
| list, inspect, report, wait | Not required by default |
| spawn, send | Required, including with `--yolo` |
| archive | Main only; required, including with `--yolo` |
| stop | Required normally; `--yolo` can skip it |

**send** adds steering input at the child's next safe boundary; it does not interrupt an ongoing
model request or tool call. An idle child starts another turn by default. Archived agents cannot
receive input.

Tasks must be self-contained: children receive system and project guidance, not the parent's
conversation or notes. See [forked skills](skills.md) for tasks that run in their own child.
Creating or sending tasks requires interactive approval; piped input cannot approve child tools.
