# Safety

```{admonition} Use at your own risk
:class: warning
wizolt edits files and runs shell commands in the environment where you start it. It does
**not** sandbox itself.
```

Its [tools](tools.md) can reach files beyond the working directory. Review commands and file
paths with the same care you would use when running them yourself.

(built-in-guardrails)=
## Built-in guardrails

- **Confirmations.** File-changing and command-running tools — Edit, Bash, Job, and MCP
  calls — ask before they act. <span class="marker">This is on by default</span>; `--yolo` and
  `/yolo` turn it off. Your hooks and loaded skills can pre-approve covered calls.
  Creating a subagent or sending it a task still requires approval under yolo; inspect its task
  and configure its model before starting it.
- **Checked edits.** An edit is refused if its target changed since it was read, or if the
  target text matches more than one place. The agent must resolve that before trying again.
- **Reviewable changes.** Every edit is shown as the diff it made, and a resumed session replays
  those diffs from the session log. Git remains the record for the workspace as a whole.
- **Repository skills wait for trust.** A cloned repository's skills that run commands, carry
  hooks, or pre-approve tools stay off until you run `/skills trust` there. See
  [Trusting a repository](skills.md#trusting-a-repository).
- **Your own guards.** [Hooks](hooks.md) run your checks before and after tool calls, and can
  block a call or insist on the approval prompt even under yolo.

## Reducing risk

- Work inside a **git repository** so every edit is reviewable and reversible.
- Keep **confirmations on** until you trust a workflow; only reach for `--yolo` when you do.
- Only connect [MCP](mcp.md) servers you trust — local servers run programs on your machine.
- Read a repository's skills before `/skills trust`: trusted skills can run commands and approve
  tool calls on their own. Patterned Bash rules in `allowed-tools` do not cover chained or
  redirected commands; a bare `Bash` rule approves every Bash call.
- For untrusted repositories, or when running unattended, put wizolt inside a **container
  or VM**.
