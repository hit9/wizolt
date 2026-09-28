# Hooks

A **hook** is a shell command wizolt runs when a session starts or ends, a tool runs, the agent
responds, or context is compacted. Hooks can check actions, refuse them, add guidance, or record
what happened. The events below use a subset of [Claude Code's hook format](https://code.claude.com/docs/en/hooks)
with wizolt's [tool names](tools.md).

## Configuring hooks

Put hooks in the config file (`~/.wizolt/config.toml`):

```toml
[[hooks.PreToolUse]]
matcher = "Bash"
hooks = [{ type = "command", command = "~/bin/no-force-push.sh", timeout = 10 }]

[[hooks.PostToolUse]]
matcher = "Edit"
hooks = [{ type = "command", command = "ruff format --quiet ." }]

[[hooks.SessionStart]]
matcher = "startup|resume"
hooks = [{ type = "command", command = "git status --short" }]
```

`matcher` is a regular expression that must match the whole value in the table below. Leave it
out, or use `*`, to match every value. Commands run in order, and wizolt waits for them: each
matching command adds its running time to the operation. `timeout` is a finite positive number
of seconds, defaulting to 60. Each output stream keeps at most 32,000 characters.

A [skill](skills.md#hooks-and-pre-approved-tools) can carry the same table in its frontmatter;
its hooks start when the skill first loads. `/status` counts the hooks in force. An unsupported
event, handler type or command option is a configuration error. Supported command options are
`type`, `command` and `timeout`.

## Events

| Event | Runs | Matcher | What it can do |
|---|---|---|---|
| `SessionStart` | Once when a session opens, including resume | `startup` or `resume` | Add context to the first accepted message |
| `SessionEnd` | Once during orderly shutdown or session switch | `prompt_input_exit`, `resume` or `other` | Run cleanup; cannot block exit |
| `UserPromptSubmit` | When you send a message, including follow-ups | Ignored | Refuse the message or add context |
| `PreToolUse` | Before a tool runs and before approval | Tool name | Refuse, approve, require a prompt, or add context |
| `PermissionRequest` | When a tool needs approval | Tool name | Allow or deny through JSON |
| `PostToolUse` | After a tool succeeds | Tool name | Add feedback to its result |
| `PostToolUseFailure` | After a tool raises an error or Bash exits nonzero | Tool name | Add guidance to the failed result |
| `Stop` | Before the agent finishes its answer or suggested next steps | Ignored | Send it back to work, at most 5 times per turn |
| `StopFailure` | When a model error ends the turn | `unknown` | Record the error; cannot restart the turn |
| `PreCompact` | Before automatic compaction or `/compact` | `auto` or `manual` | Refuse compaction; an automatic refusal ends the turn |
| `PostCompact` | After compaction, including a fallback trim | `auto` or `manual` | Record the new summary |
| `SubagentStart` | At each Delegate send, including a forked skill | `worker` | Add context to the worker |
| `SubagentStop` | Before the worker finishes its response | `worker` | Send it back to work, at most 5 times per worker turn |

A refused or cancelled tool call does not fire `PostToolUseFailure`. Cancellation and the step
limit do not fire `Stop` or `SubagentStop`. A cancelled compaction does not fire `PostCompact`.

Workers run configured tool, compaction and model-failure hooks, plus their own loaded skills'
hooks. `UserPromptSubmit`, `SessionStart`, `SessionEnd` and `Stop` belong to the main session.
A parent's loaded skills can also supply `SubagentStart` and `SubagentStop` hooks. Subagent
feedback goes to the worker; use `PostToolUse` on `Delegate` to give feedback to the parent.

## What a hook receives

A hook reads one JSON object on stdin:

```json
{"session_id": "…", "cwd": "/path/to/repo", "hook_event_name": "PreToolUse",
 "permission_mode": "default", "tool_use_id": "…", "tool_name": "Bash",
 "tool_input": {"command": "git push --force"}}
```

`permission_mode` is `bypassPermissions` under yolo, otherwise `default`.
`CLAUDE_PROJECT_DIR` and `WIZOLT_PROJECT_DIR` hold the repository's top folder.

| Events | Additional input |
|---|---|
| Tool events | `tool_name`, `tool_input` (the tool's own arguments), `tool_use_id` |
| `PostToolUse` | `tool_response` |
| `PostToolUseFailure` | `error`, `is_interrupt: false` |
| `UserPromptSubmit` | `prompt` |
| `Stop`, `SubagentStop` | `last_assistant_message`, `stop_hook_active` (true after the first redirect) |
| `StopFailure` | `error: "unknown"`, `error_details`, `last_assistant_message` (the error text) |
| `SessionStart`, `SessionEnd` | `source` and `model` at start; `reason` at end |
| `PreCompact`, `PostCompact` | `trigger`; `custom_instructions: null` before; `compact_summary` after |
| `SubagentStart`, `SubagentStop` | The parent's `session_id`, the worker's `agent_id`, `agent_type: "worker"` |

## What a hook returns

- **Exit 0** — continue; JSON can decide more below. Plain stdout adds context for
  `UserPromptSubmit` and `SessionStart`.
- **Exit 2** — refuse with stderr as the reason for `PreToolUse`, `UserPromptSubmit`, `Stop`,
  `SubagentStop` and `PreCompact`. For `PostToolUse`, the reason is feedback: the tool already ran.
  `PermissionRequest` ignores exit 2; it needs a JSON decision. Other events cannot block.
- **Other failures, including timeouts** — show the error and continue.

JSON printed on exit 0:

| Field | Effect |
|---|---|
| `hookSpecificOutput.permissionDecision` | `PreToolUse`: `deny` refuses, `allow` skips approval, `ask` forces a prompt even under yolo |
| `hookSpecificOutput.permissionDecisionReason` | Reason for a pre-tool refusal |
| `hookSpecificOutput.decision` | `PermissionRequest`: `{"behavior": "allow"}` or `{"behavior": "deny", "message": "reason"}` |
| `hookSpecificOutput.additionalContext` | Context for `UserPromptSubmit`, `SessionStart`, `SubagentStart`, `PreToolUse`, `PostToolUse` and `PostToolUseFailure`; for `Stop` and `SubagentStop`, feedback that keeps the agent working |
| `decision: "block"` with `reason` | Refuses at a blocking event, like exit 2 |

When several hooks match, any refusal wins and `ask` outranks `allow`. A permission hook cannot
bypass an explicit pre-tool `ask` or a tool's mandatory confirmation. Tool-input rewrites and
persistent permission changes are unsupported; an allow decision requesting them leaves approval
to you.
A `PermissionRequest` denial still refuses the call when it includes `interrupt: true`, but
that flag does not end the agent's turn.

Only the events, input fields and output fields listed here are supported. In particular, hooks
do not receive a Claude transcript file or an environment-persistence file. Hook commands run
in the foreground; async, HTTP, prompt and agent handlers are not supported.
