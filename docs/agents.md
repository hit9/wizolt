# Subagents

Ask wizolt to split a task into parallel work. By default it can start up to three child agents alongside
your main conversation. Each has its own conversation, model requests, context budget and usage
statistics, plan and notes. New children use your subagent defaults, or inherit the creating agent's model;
you can change settings before approval. Additional agents
increase total model usage.

## Approve and configure

Set a default provider for new children in `~/.wizolt/config.toml`:

```toml
[subagent]
provider = "deepseek"
```

The name must match an existing `[provider.deepseek]` entry. Optional `model`, `reasoning` and
`api` fields override that provider's settings. Omitted or empty fields inherit; without a
provider override, children inherit their parent's currently selected provider.
The old `[worker]` section is also accepted. If both sections exist, `[subagent]` wins per field.
These defaults apply to newly created children, including forked skills, not existing or resumed
children. Each child's approval configuration takes precedence over the defaults.

Creating a child or sending it another task requires approval, including with `--yolo`.
Choose **View agent task** (`v`) to read the full task. Before creating a child, choose
**Config** (`c`) to set its provider, model, reasoning effort or request API. Return with
**done** or Escape, then approve to start its first turn. Each creation has its own configuration;
configuring one child does not change the main agent or another child. Refusing discards the
pending child and its settings.
In piped/non-interactive mode, child tools that require approval return an error. They do not
read the main conversation's input. `--yolo` still permits tools eligible for automatic approval.

Forked skills offer the same configuration before approval. Once a child exists, select it
with `/agents` to change its settings for subsequent turns.

## Agent limit

Set `max_subagents` under `[runtime]` in your config file, or use
`/set runtime.max_subagents NUMBER` in the main conversation. The default is `3`; valid values
are `0` through `32`, excluding the main agent. `0` prevents new children. The limit includes
nested children and completed children whose conversations remain available. Lowering it keeps
existing conversations and prevents new children until there is room.
Forked skills use these same slots; stopping a child does not free its retained slot.

The model sees the configured limit. `/status` shows retained children and the limit for the
whole group. Reuse a child for follow-up work instead of creating another one.
If a child fails or is interrupted, its queued inputs remain paused. Send a follow-up to resume it.

## One shared workspace

All agents work in the same directory and see file changes immediately. Give concurrent tasks
separate files to edit. Ask agents to coordinate before changing the same file, and review their
actual changes before accepting a report. Stopping an agent leaves its file changes in place.

`/diff` reviews the selected agent's recorded edits. If agents edit the same file between its
calls, the viewer keeps that agent's individual edits instead of combining other agents' changes.

## Select and inspect

`/agents` lists the main agent and its children in aligned name, state and context columns.
`(current)` marks the open conversation, shortened to `*` in narrow panes. Move
with j/k or the arrow keys to preview the highlighted agent in a small bordered window. It shows
the task and recent reply, updating as new text arrives. Enter opens that agent's conversation;
Escape keeps your current selection. Roomy previews separate Task and Reply/Live headings from
their text; small panes use a compact layout.

Press **x** to stop the highlighted agent after confirmation, or **Shift+X** to stop it immediately.
The picker stays open, and the stopped conversation remains available for inspection and new input.

The selected agent owns the conversation you see, your input, draft, history and queued messages.
Main's Up/Ctrl-P recall includes earlier main sessions across projects; a child recalls only its
own inputs. Earlier recalled inputs enter an agent's context only when you send them.
Its statusbar shows its name, model and context usage. When children exist, it also shows group
counts, such as `agents 3 · run 1` with a separate `wait 1` alert: three retained agents including main, one running
and one waiting for input. Completed agents remain in the total. With only main, the default
layouts hide the count; narrow terminals may omit it. Colors and separators follow your statusbar
theme and layout, including segmented layouts.
`/status` reports its identity, state,
parent and statistics. Use `/model`, `/provider` and `/reason` after selecting an agent to change
that agent's model.

Background agents continue working. Their output stays in their own conversations. Emphasized
notices identify agents that finish, fail or need input, using your theme's success, error and
warning colors. A warning includes the `/agents` shortcut; select that agent to respond.
Child notices include a `subagent` prefix, such as `✓ subagent [ui-review] completed`;
main's notices use `agent [main]`.
The `/agents` list marks working agents with a breathing green dot, input waits with a warning
dot, and other agents with a static dot in the theme's text color.
The statusbar highlights `wait N` separately and keeps it visible ahead of ordinary details on
narrow screens. Notices do not switch your selection.

## Add input and stop work

Enter sends additional instructions to the selected running agent. For an idle or completed
agent, it starts another turn in that agent's existing conversation. Tab holds input for its
next turn. Input accepted before a switch keeps its original destination.

Ctrl-C interrupts the selected agent. `/agents stop-all` interrupts every agent. Exiting wizolt
stops all agents and saves their conversations. Resume the main session to restore its children;
queued work waits for new input rather than restarting automatically.
Each child's provider, model, effort and request API survive resume independently of later
changes to the main agent. Background shell jobs stop when you exit and are not restored.

## Model tools

Children use wizolt's system instructions and project guidance, with an additional reminder
that workspace files are shared. The task is their first input; it must include what they need
because the parent's conversation, plan and notes are not copied.

The `Subagent` tool starts agents with `spawn`, adds input with `send`, and exposes `list`,
`wait` and `stop`. Spawn returns an agent ID immediately, so the caller can continue working.
The creating agent assigns each child a unique task-based name, such as `api-review`,
`ui-review` or `test-check`; `main` is reserved for your main conversation.
Waiting defaults to 3 minutes, with a maximum of 10 minutes per call (`timeout=600` seconds).
Use `timeout=0` to check immediately. Timing out leaves the child running. A child keeps
its conversation for subsequent inputs. Stopping a child returns after its turn has settled.

A skill with `context: fork` starts a child agent after you confirm its task. Its report returns
to the caller; `/agents` lets you inspect the child's work and add input while it runs.

Resuming main also restores its child conversations without starting their queued work. If a
child snapshot is missing or damaged, wizolt reports it and restores the healthy conversations;
the damaged child's files are kept for recovery.
