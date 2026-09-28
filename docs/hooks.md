# Hooks

A **hook** is a shell command wizolt runs at a fixed point in a turn: before or after a tool
call, when you send a message, or when the agent is about to stop. Hooks can check, block or add
to what happens. They use Claude Code's format, so a hook written for Claude Code runs here
unchanged as long as it matches wizolt's [tool names](tools.md).

## Configuring hooks

Put hooks in the config file (`~/.wizolt/config.toml`):

```toml
[[hooks.PreToolUse]]
matcher = "Bash"
hooks = [{ type = "command", command = "~/bin/no-force-push.sh", timeout = 10 }]

[[hooks.PostToolUse]]
matcher = "Edit"
hooks = [{ type = "command", command = "ruff format --quiet ." }]
```

`matcher` is a regular expression that must match the whole tool name (`Bash|Edit`); leave it
out, or use `*`, to match every tool. `timeout` is in seconds and defaults to 60. A malformed
hook stops wizolt at startup with the reason, rather than never running.

A [skill](skills.md#hooks-and-pre-approved-tools) can carry the same table in its frontmatter;
its hooks start when the skill first loads. `/status` counts the hooks in force.

## Events

| Event | Runs | Exit 2 (or a JSON "block") |
|---|---|---|
| `PreToolUse` | Before a tool call, before its approval prompt | Refuses the call; the agent is told why |
| `PostToolUse` | After a tool call succeeds | The call already ran; the reason goes to the agent with the result |
| `UserPromptSubmit` | When you send a message | Refuses the message; nothing is sent. A follow-up typed mid-turn is withheld |
| `Stop` | When the agent gives its final answer | The agent keeps working, with the reason as its next instruction, at most 5 times a turn |

A worker runs `PreToolUse` and `PostToolUse` only; `UserPromptSubmit` and `Stop` belong to your
own turns.

## What a hook receives and returns

The hook reads one JSON object on stdin:

```json
{"session_id": "…", "cwd": "/path/to/repo", "hook_event_name": "PreToolUse",
 "tool_name": "Bash", "tool_input": {"command": "git push --force"}}
```

`tool_input` holds the tool's own arguments; `PostToolUse` adds `tool_response`,
`UserPromptSubmit` adds `prompt`, and `Stop` adds `stop_hook_active` (true once a Stop hook
already sent the agent back this turn, so a hook can avoid looping). `CLAUDE_PROJECT_DIR` and
`WIZOLT_PROJECT_DIR` hold the repository's top folder.

The hook answers with its exit code:

- **0** — carry on. Printed JSON can decide more (below). For `UserPromptSubmit`, plain printed
  text is added to your message as context.
- **2** — block, with stderr as the reason.
- **Anything else, or a timeout** — the hook is broken. You see the error; the turn continues
  as if the hook weren't there.

JSON printed on exit 0, following Claude Code:

```json
{"hookSpecificOutput": {"permissionDecision": "allow"}}
```

| Field | Effect |
|---|---|
| `hookSpecificOutput.permissionDecision` | `PreToolUse` only: `deny` blocks, `allow` skips the approval prompt, `ask` shows it even under yolo |
| `hookSpecificOutput.additionalContext` | Text added for the agent |
| `decision: "block"` with `reason` | Blocks, like exit 2 |

When several hooks match, they run in order: any block blocks, and `ask` outranks `allow`.
