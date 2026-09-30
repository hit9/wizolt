(worker-delegation)=
# Worker delegation

A **worker** takes a focused part of your task, such as reviewing a file or checking a fix.
You approve the order, the worker returns a report, and your main conversation continues.

```{figure} _static/worker-handoff.svg
:alt: The main conversation sends an approved order to a worker, receives its report and continues the original task.

One task, a focused second opinion.
```

The worker shares your workspace but has its own model and conversation. It remembers earlier
orders until you reset it. Choose a cheaper model for routine work, or another model for a
different perspective.

## Quick start

The parent orchestrates, so it usually runs the larger model; the worker runs bounded tasks,
so a faster, cheaper entry is enough for it — ideally from a **different vendor** than
`provider.active`, so its reviews cross-validate the parent's. Point `[worker] provider` at
that entry and turn on `[runtime] worker`:

```toml
[provider]
active = "default"

[provider.default]
url = "https://api.openai.com"
key = "sk-..."
model = "gpt-5.5"

# A faster, cheaper entry for the worker, from a different vendor.
[provider.deepseek]
url = "https://api.deepseek.com"
key = "sk-..."
model = "deepseek-flash"

[worker]
provider = "deepseek"    # worker provider key; unset disables delegation
model = ""               # optional: override the entry's model (inherit by default)
reasoning = ""           # optional: override the entry's reasoning effort (inherit by default)
api = ""                 # optional: override the entry's wire protocol (inherit by default)

[runtime]
worker = true            # or /worker on
```

The model decides when to delegate. When it does, it writes the `order` itself and asks you to
approve the send; the worker then takes over the terminal until it answers, and its answer goes
back to the parent, which decides what to do next.

| Command | Effect |
|---|---|
| `/worker` | Show its provider, model, state, and rounds |
| `/worker on` · `/worker off` | Allow or stop delegation for this session |
| `/worker reset` | Clear the worker's context |
| `/worker provider NAME` | Point it at another provider entry |
| `/worker model` · `reason` · `api` | Override one field of that entry; `default` restores it |

[Commands](commands.md) has the full syntax.

## What you see

### The delegation bracket

Two full-width rules bracket a delegation: a yellow `worker start` rule with the worker's live
provider and model plus the order's title (or first line), the worker's streamed lines, then a
yellow `worker done` rule with the step count, elapsed time, tokens in and out, and the files it
touched.

<div class="term-shot" role="img" aria-label="The delegation bracket: a full-width yellow worker start rule naming the worker's provider, model, and order title, a few worker tool lines beneath it, and a yellow worker done rule with step count, elapsed time, token counts, and touched files."><span class="fs-worker">──── worker start · deepseek/deepseek-flash · Review the parser refactor ───────</span><span class="fs-tool">  ├ Read wizolt/loop.py</span><span class="fs-tool">  ├ Read tests/test_edit_tool.py</span><span class="fs-tool">  └ Bash uv run pytest tests/ -q</span><span class="fs-worker">──── worker done · steps 7 · 43.2s · 12.4K in / 1.1K out · wizolt/loop.py, tests/ ────</span></div>

### The send approval brief

Every `Delegate send` asks for approval, even under `yolo` — the confirmation prints the title, a
one-line excerpt of the order, any explicit `language` or `max_steps`, and the effective worker
provider/model/effort/api, with `(inherit)` marking a field that inherits the provider entry's
value. A refused send feeds your reason back to the model.

The actions sit in a row above the input line, `Approve` selected to begin with. `Tab` and the
arrows move the highlight — above, two presses have landed on `View order` — and `Enter` fires
whatever is highlighted:

| Key | Action |
| --- | --- |
| `Enter` | do the selected action — `Approve` unless you moved |
| `Tab` / `→` / `Shift-Tab` / `←` | move along the row |
| `Esc` | refuse without a reason |
| anything printable | start writing a refusal reason |

Typing always goes to the reason, so no letter is ever a shortcut and no reason is unwritable. While
you are typing, the row dims and `Enter` sends the reason instead; `Esc` takes the reason back and
returns to the row, and refuses only once there is nothing left to take back.

Every tool's approval works this way — non-`Delegate` ones simply offer `Approve` and `Refuse`.
Without a TUI (piped input, a headless run) the row is gone and the same actions are typed out:
`y`, `n`, `v`, `c`, each followed by Enter.

<div class="term-shot" role="img" aria-label="The Delegate send approval brief: field rows for the title, an order excerpt, and the worker's effective provider, model, effort and api with inherit markers, then the action row in yellow with View order on the selection band after two presses of Tab, the key hints beside it, and the reason input line below."><span class="fs-tool">Delegate send</span><span> </span><span><span class="fs-i fs-sel">title    </span>Review the parser refactor</span><span><span class="fs-i fs-sel">order    </span>Extract parser.py from loop.py, keep the CLI surface unchanged (… 3 more lines)</span><span><span class="fs-i fs-sel">provider </span>(inherit) deepseek</span><span><span class="fs-i fs-sel">model    </span>(inherit) deepseek-flash</span><span><span class="fs-i fs-sel">effort   </span>(inherit) medium</span><span><span class="fs-i fs-sel">api      </span>(inherit) chat</span><span><span class="fs-i fs-dim">    │</span></span><span><span class="fs-i fs-dim">    │ </span><span class="fs-i fs-approve"> Approve </span><span class="fs-i fs-dim">  </span><span class="fs-i fs-selected"> View order </span><span class="fs-i fs-dim">  </span><span class="fs-i fs-approve"> Worker config </span><span class="fs-i fs-dim">  </span><span class="fs-i fs-approve"> Refuse </span><span class="fs-i fs-dim">    Enter runs it · Tab to move · Esc refuses</span></span><span class="fs-dim">  reason › </span></div>

`View order` opens the full order in a read-only viewer — the same field rows, then the order
itself rendered as markdown. Scroll it, then `Esc` back to the actions. After the delegation has
run, `Ctrl-O` reopens the same order with the worker's answer below it, which is where you judge
one against the other: the transcript keeps only the `Delegate send` line.

<div class="term-shot" role="img" aria-label="The order viewer opened from View order: a read-only header, the same field rows as the brief with cyan labels, a rule, then the full order rendered as markdown with an underlined heading, inline code, and a bullet list, above a line of scrolling keys."><span class="fs-dim">  Delegate order · read-only</span><span><span class="fs-i fs-sel">  title   </span>  Review the parser refactor</span><span><span class="fs-i fs-sel">  language</span>  python</span><span><span class="fs-i fs-sel">  provider</span>  (inherit) deepseek</span><span><span class="fs-i fs-sel">  model   </span>  (inherit) deepseek-flash</span><span class="fs-dim">  ────────────────────────────────────────────────────────────────────────</span><span>  <span class="fs-i fs-md-h">Review the parser refactor</span></span><span>&nbsp;</span><span>  Extract <span class="fs-i fs-md-code">parser.py</span> from <span class="fs-i fs-md-code">loop.py</span> and keep the CLI surface unchanged.</span><span>&nbsp;</span><span>   • keep <span class="fs-i fs-md-code">tokenize()</span> public</span><span>   • add a test for empty input</span><span>&nbsp;</span><span class="fs-dim">  ↑/↓ · Ctrl-D/U · g/G · Esc/q close</span></div>

## When nothing delegates

Delegation is the model's call, not a command you run — there is no way to force one. If a session
never delegates, check in this order:

1. `/worker` — it reports the configured entry, or `worker: no active session` with the
   `[worker] provider` it would use.
2. `[worker] provider` and `[runtime] worker` in your config, or `/worker on` for this session.
3. **Whether delegation was available when the session started.** If `[worker] provider` was unset
   at startup, the model was never offered the tool, and turning it on now applies to the next
   session. Changing the provider mid-session is fine — that only re-targets a delegation the
   model already has.

Otherwise the model simply saw no task worth handing over, which is the normal case for short work.

## Good to know

| Question | Answer |
| --- | --- |
| Does reset undo edits? | No. `/worker reset` clears its conversation; file changes remain. |
| What if a request fails? | Its conversation and completed edits remain, so the agent can retry. |
| Does yolo skip the order? | No. Every delegation still asks you to approve the order. |
| What is shared? | Your workspace, available skills, MCP servers and language setting. Each session loads its own skills. |
| Where can I see usage? | `/status` shows separate worker model, context and cache figures. |
| Can I guide it while it works? | Enter sends a follow-up to the worker. An unanswered message returns to the main conversation. Tab holds a task for the main conversation. |
| How is it saved? | Alongside your main session. It has no separate entry in `/sessions`. |

[Hooks](hooks.md#events) can add guidance when a worker starts or ask it to keep working before
it finishes. `SubagentStop` can redirect it up to five times per turn.
