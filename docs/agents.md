# Subagents

Ask wizolt to split a task into parallel work. It can start up to three child agents alongside
your main conversation. Each has its own conversation, model requests, context budget and usage
statistics. New children start with the creating agent's model and settings; additional agents
increase total model usage.

## One shared workspace

All agents work in the same directory and see file changes immediately. Give concurrent tasks
separate files to edit. Ask agents to coordinate before changing the same file, and review their
actual changes before accepting a report. Stopping an agent leaves its file changes in place.

## Select and inspect

`/agents` lists the main agent and its children with their state and context percentage. Move
the cursor to preview a task and its recent reply. Enter selects that agent; Escape keeps your
current selection.

The selected agent owns the conversation you see, your input, draft, history and queued messages.
Its statusbar shows its name, model and context usage. `/status` reports its identity, state,
parent and statistics. Use `/model`, `/provider` and `/reason` after selecting an agent to change
that agent's model.

Background agents continue working. Their output stays in their own conversations. A notice
identifies an agent that finishes, fails or needs input; it does not switch your selection.
Select that agent to answer an approval or question.

## Add input and stop work

Enter sends additional instructions to the selected running agent. For an idle or completed
agent, it starts another turn in that agent's existing conversation. Tab holds input for its
next turn. Input accepted before a switch keeps its original destination.

Ctrl-C interrupts the selected agent. `/agents stop-all` interrupts every agent. Exiting wizolt
stops all agents and saves their conversations. Resume the main session to restore its children;
queued work waits for new input rather than restarting automatically.

## Model tools

The `Subagent` tool starts agents with `spawn`, adds input with `send`, and exposes `list`,
`wait` and `stop`. Spawn returns an agent ID immediately, so the caller can continue working.
Waiting has a timeout of up to 60 seconds; timing out leaves the child running. A child keeps
its conversation for subsequent inputs. The three-child limit applies to retained children,
so reuse an existing child for follow-up work.

A skill with `context: fork` starts a child agent after you confirm its task. Its report returns
to the caller; `/agents` lets you inspect the child's work and add input while it runs.
