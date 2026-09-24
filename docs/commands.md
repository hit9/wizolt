# Commands

## Looking around

**`/status`** — Shows everything about the runtime at a glance: workspace path,
session id, active provider and model, calculated compaction-budget fill percentage,
conversation history, prompt-cache hit ratio, the AGENTS.md state, background jobs, and whether an
update is available. Its last row links to the [documentation](https://wizolt.readthedocs.io).

<div class="term-shot" role="img" aria-label="The /status command: a boxed two-column table of the workspace, session, yolo, step limit, AGENTS.md state, model, a context fill bar, cache hit ratios, request usage, and worker state, with the documentation link as its last row."><span class="fs-user">• /status</span><span> </span><span><span class="fs-i">  </span><span class="fs-i fs-dim">╭──────────────────────────────────────────────────────────╮</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> workspace </span><span class="fs-i fs-dim"> </span><span class="fs-i fs-sel">~/dev/github/wizolt</span><span class="fs-i">                           </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> session   </span><span class="fs-i fs-dim"> </span><span class="fs-i fs-sel">20260923101532-4c64ec94-a1f</span><span class="fs-i">                   </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> yolo      </span><span class="fs-i fs-dim"> </span><span class="fs-i">off                                           </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> steps     </span><span class="fs-i fs-dim"> </span><span class="fs-i">400                                           </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> agents.md </span><span class="fs-i fs-dim"> </span><span class="fs-i">on (./AGENTS.md; global active)               </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> model     </span><span class="fs-i fs-dim"> </span><span class="fs-i fs-sel">openai/gpt-5.6</span><span class="fs-i"> · responses · reasoning medium </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> context   </span><span class="fs-i fs-dim"> </span><span class="fs-i">[███▋░░░░░░░░░░] </span><span class="fs-i fs-sel">~62.4K / 240.5K</span><span class="fs-i"> (26%)        </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> cache     </span><span class="fs-i fs-dim"> </span><span class="fs-i">total </span><span class="fs-i fs-sel">92.7%</span><span class="fs-i"> · last </span><span class="fs-i fs-sel">97.2%</span><span class="fs-i">                      </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> usage     </span><span class="fs-i fs-dim"> </span><span class="fs-i">calls </span><span class="fs-i fs-sel">215</span><span class="fs-i"> · total </span><span class="fs-i fs-sel">13.8M</span><span class="fs-i">                       </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> worker    </span><span class="fs-i fs-dim"> </span><span class="fs-i">off — </span><span class="fs-i fs-sel">[worker] provider</span><span class="fs-i"> unset                 </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">│</span><span class="fs-i"> docs      </span><span class="fs-i fs-dim"> </span><span class="fs-i">https://wizolt.readthedocs.io                 </span><span class="fs-i fs-dim">│</span></span><span><span class="fs-i">  </span><span class="fs-i fs-dim">╰──────────────────────────────────────────────────────────╯</span></span></div>

**`/diff`** — Review changes from the latest turn or the whole session. See
[Reviewing changes](usage.md#reviewing-changes).

<div class="term-shot" role="img" aria-label="The diff viewer: a Latest and Session tab above a list of changed files, each with added and removed line counts, and a key hint along the bottom."><span><span class="fs-i fs-tab-on"> Latest </span><span class="fs-i fs-dim"> │ </span><span class="fs-i fs-tab-off"> Session </span></span><span> </span><span class="fs-selected">&gt; +45 -12 docs/usage.md</span><span class="fs-dim">  <span class="fs-i fs-add">+12</span> <span class="fs-i fs-del">- 3</span> wizolt.py</span><span class="fs-dim">  <span class="fs-i fs-add">+ 4</span> <span class="fs-i fs-del">- 0</span> tests/test_mcp.py</span><span> </span><span class="fs-dim">  [list] ↑/↓ or j/k move · ←/→ or h/l tab · Enter open · r refresh · Esc/q close [1/3]</span></div>

The two tabs pick the range; each row is one changed file with its added and removed line
counts. `Enter` opens the selected file's diff.

**`/ps`** — Lists active background jobs (see [Tools](tools.md#built-in-tools)).
Each row shows job id, state, command, and elapsed time.

**`/skills`** — Lists every installed [skill](skills.md) by name, source, and
description.

**`/config`** — Shows the active configuration: provider blocks, runtime settings,
and their resolved values.

**`/catalog [status|sync]`** — Shows the active compatibility catalog, including its version,
publication date, maintenance scope, source, bundled/cached versions, and the last sync result.
`status` is an explicit alias for the default view. `sync` checks GitHub immediately instead of
waiting for the automatic check, which runs at most once every 72 hours. See
[Compatibility catalog](catalog.md) for source selection, activation, and failure behavior.

## Switching models

Each takes an optional value: given one it switches straight away, given none it opens a picker.
`/provider` and `/model` chain onward, so picking a provider walks you through the model, its
protocol, and the effort.

| Command | Sets | Values |
|---|---|---|
| `/provider [NAME]` | The active provider entry | Any [configured provider](configuration.md#providers) |
| `/model [MODEL]` | The model for that entry | Configured and discovered models |
| `/reason [EFFORT]` | Reasoning effort — `/effort` is the same command | the levels the active model offers, plus `off` |
| `/api [API]` | The protocol used to reach the model | `auto`, `chat`, `responses`, `anthropic` |

Effort is mapped to the nearest level a known model family accepts; unrecognized models keep what
you picked. A model that `/model` offered can still come back unsupported, because one endpoint
often serves several families over different protocols — set the right one with `/api`, or `auto`
to re-infer it. Switching mid-session is safe either way, since the history is protocol-neutral.

```{figure} ../snapshots/wizolt-demo-switching-providers-models.gif
:alt: Switching providers and models interactively during a session
:width: 600px
:align: center

Switching providers and models mid-session.
```

## Managing the session

**`/name [TEXT]`** — Show or set this session's name. See [Names](usage.md#names).

**`/sessions [all]`** — Browse saved sessions and re-enter one; `/resume` is the same command.
See [Switching sessions](usage.md#switching-sessions).

**`/compact`** — Summarize and shrink the conversation immediately. wizolt keeps
long sessions within budget on its own, but `/compact` trims on demand.

**`/compact log [seg.N]`** — Review what compaction kept: the stored segments newest
first, and the summary of the one you open. Naming a segment prints its summary
without the viewer. Each row is a `history.N.md` file the agent can read back, kept
beside the `history.md` index. See
[Keeping context manageable](context.md#keeping-context-manageable).

**`/context [reset]`** — Shows how much of the context window is in use, or clears the model's conversation
and starts a new one with `/context reset`. Here the new window begins at once, because the command
runs outside a turn. Like `/compact`, it is unavailable while the agent is working. See
[Starting a new window](context.md#starting-a-new-window).

**`/worker [SUBCOMMAND]`** — Inspect or control the worker session. Tab completion offers the
subcommands and their values; see [Worker delegation](worker.md#worker-delegation) for what a
worker is.

| Subcommand | Effect |
|---|---|
| `status` (default) | provider/model, reasoning, state, rounds, and context percent — or `worker: no active session` with the configured `[worker] provider` |
| `on` · `off` | Toggle the `runtime.worker` setting |
| `reset` | Clear the worker's context; file changes and merged diffs survive |
| `provider` | Pick an entry, then flow on into the model and reasoning pickers, mirroring `/provider`'s chain; backing out of a stage keeps the earlier ones |
| `provider NAME` · `provider off` | Re-target or clear the entry immediately, without the picker chain |
| `model` · `reason` · `api` | Pick from that entry's models, reasoning efforts, and wire protocols (`auto`, `chat`, `responses`, `anthropic`); `default` restores inheritance |

Changes apply to a live worker and to later spawns alike. The `Delegate` tool block itself is
fixed when the session starts, so a worker turned on mid-session takes effect next time.

Every `Delegate send` asks for approval, even under `yolo` — the order is a spec the model wrote
for itself, so the brief is the one cheap check on it. A refusal carries your reason back to the
model.

**`/language [NAME]`** — Show or force the session's reply language. `auto`, the default,
follows the language you write in; a name like `Chinese` fixes it. The setting lives in the
session rather than the config file, and workers inherit it.

## Settings and toggles

**`/set KEY VALUE`** — Set `provider.*` or `runtime.*` for the session; tab-completes both keys
and, where the values are a fixed set, the values. Example: `/set provider.response_timeout 900`.

| Command | Effect |
|---|---|
| `/yolo` | Toggle confirmation prompts — read [Safety](safety.md) before leaving them off |
| `/strict` | Toggle strict tool-call schemas (OpenAI / DeepSeek) |

## While a turn runs

Commands that only read the session answer without interrupting it: `/status`, `/ps`, `/diff`,
`/skills`, `/config`, `/catalog`, and `/mcp`'s tool list, along with the `/yolo` toggle.
Any other one waits for the turn, and `/resend` is the one that interrupts the request itself.

**`/resend`** — Cancel and re-send the model request in flight, without restarting the turn.
Use it when a response stalls. It only applies while a request is waiting, not while the agent
runs a tool. The divider reports the retry and returns to `working`; automatic retries look the
same, with their attempt and reason, such as `retrying 2/6 · timeout`:

<div class="term-shot" role="img" aria-label="The running divider briefly changes from working to retrying while preserving its green waiting pulse and elapsed timer, then returns to working as the replacement model request continues."><span><span class="fs-i fs-rule">--</span><span class="fs-i fs-glow">-</span><span class="fs-i fs-rule"> </span><span class="fs-i fs-add">●</span><span class="fs-i fs-rule"> </span><span class="fs-i fs-working">working (11s)</span><span class="fs-i fs-rule"> ------------------------------</span></span><span class="fs-prompt">+&gt; /resend</span><span><span class="fs-i fs-rule">--</span><span class="fs-i fs-glow">-</span><span class="fs-i fs-rule"> </span><span class="fs-i fs-add">●</span><span class="fs-i fs-rule"> </span><span class="fs-i fs-working">retrying (12s)</span><span class="fs-i fs-rule"> ------------------------------</span></span><span><span class="fs-i fs-rule">--</span><span class="fs-i fs-glow">-</span><span class="fs-i fs-rule"> </span><span class="fs-i fs-add">●</span><span class="fs-i fs-rule"> </span><span class="fs-i fs-working">working (14s)</span><span class="fs-i fs-rule"> ------------------------------</span></span></div>

## MCP

**`/mcp`** — Manage [MCP](mcp.md) server connections. Sub-commands:

| Usage | Effect |
|---|---|
| `/mcp` | List servers and connection status |
| `/mcp connect <server> [server ...]` | Connect servers now |
| `/mcp disconnect <server>` | Disconnect a server |
| `/mcp tools [server]` | List tools from a connected server |

## Help and exit

The prompt lists and completes every command as you type, and `/status` ends with the link to the
[documentation](https://wizolt.readthedocs.io).

**`/exit`, `/quit`** — Leave wizolt. Your session is saved automatically and can
be resumed with `-c` or `--resume`.
