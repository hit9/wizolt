# Safety

```{admonition} Use at your own risk
:class: warning
wizolt edits files and runs shell commands in the environment where you start it. It does
**not** sandbox itself.
```

wizolt acts directly in your environment. Through its [tools](tools.md) it can
<span class="marker">read and edit files beyond the working directory and run any shell
command</span>. There is no built-in isolation, so treat it with the same care as running those
commands yourself.

(built-in-guardrails)=
## Built-in guardrails

- **Confirmations.** File-changing and command-running tools — Edit, Bash, Job, and MCP
  calls — ask before they act. <span class="marker">This is on by default</span>; `--yolo` and
  `/yolo` turn it off. Your hooks and loaded skills can pre-approve covered calls.
- **Verified edits.** Every edit says what it expects to change — a numbered source view from
  `Read`, or the exact original text of the target — and is rejected
  if the file no longer matches, or if the text it named appears more than once. The agent can't
  silently patch the wrong lines. See [Tools](tools.md).
- **Reviewable changes.** `/diff` shows exactly what changed this round and across the
  session before you rely on it.
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
