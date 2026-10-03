# Commands

## Looking around

**`/status`** — Check the selected agent at a glance. **Overview** shows its state, model,
how full its context is and what it has used so far; when other agents share the session, it
counts them too. Press **h**/**l** (or **1**–**4**) for the rest:

| Tab | Shows |
| --- | --- |
| Context | What the next request holds, by part, and where compaction starts |
| Usage | Tokens and cache hits across every request, compaction, and activity counts |
| Session | Workspace, session ID, permissions, limits and instruction files |

Context figures marked `~` and **estimated** are wizolt's own count; others come from the
provider's report of the last request. Without an interactive terminal, `/status` prints every
tab, one after another.

<div class="term-shot" role="img" aria-label="The /status Overview tab inside its frame: labeled rows for the selected agent and its state, the model in the statusbar's colors, a context meter beside the reading, and the usage totals with counts and the cache share highlighted, with the Context, Usage and Session tabs one key away."><span class="fs-user">• /status</span><span> </span><span class="fs-goal">  ╭────────────────────────────────────────────────────────────────────╮</span><span><span class="fs-i fs-goal">  │</span> <span class="fs-i fs-tab-on"> Overview </span><span class="fs-i fs-dim"> │ </span><span class="fs-i fs-tab-off"> Context </span><span class="fs-i fs-dim"> │ </span><span class="fs-i fs-tab-off"> Usage </span><span class="fs-i fs-dim"> │ </span><span class="fs-i fs-tab-off"> Session </span>                       <span class="fs-i fs-goal">│</span></span><span><span class="fs-i fs-goal">  │</span>                                                                    <span class="fs-i fs-goal">│</span></span><span><span class="fs-i fs-goal">  │</span> <span class="fs-i fs-dim">agent    </span><span class="fs-i" style="color:#e7ac80;font-weight:700">main</span><span class="fs-i fs-dim"> · completed</span>                                          <span class="fs-i fs-goal">│</span></span><span><span class="fs-i fs-goal">  │</span> <span class="fs-i fs-dim">model    </span><span class="fs-i" style="color:#67d4e8">openai</span><span class="fs-i fs-dim"> / </span><span class="fs-i" style="color:#d7b0ff;font-weight:700">gpt-5.6</span><span class="fs-i fs-dim"> · </span>responses<span class="fs-i fs-dim"> · effort </span><span class="fs-i" style="color:#f0c77f">medium</span>              <span class="fs-i fs-goal">│</span></span><span><span class="fs-i fs-goal">  │</span> <span class="fs-i fs-dim">context  </span><span class="fs-i" style="color:#8dd6a1;font-weight:700">██████</span><span class="fs-i fs-dim">░░░░░░░░░░░░░░░░░░</span> <span class="fs-i" style="color:#8dd6a1;font-weight:700">62.4K</span><span class="fs-i fs-dim"> / </span><span class="fs-i" style="color:#d2a8ff">240.5K</span><span class="fs-i fs-dim"> · </span><span class="fs-i" style="color:#8dd6a1;font-weight:700">25%</span>             <span class="fs-i fs-goal">│</span></span><span><span class="fs-i fs-goal">  │</span> <span class="fs-i fs-dim">usage    </span><span class="fs-i" style="color:#d2a8ff">215</span><span class="fs-i fs-dim"> requests · </span><span class="fs-i" style="color:#d2a8ff">13.6M</span><span class="fs-i fs-dim"> in · </span><span class="fs-i" style="color:#d2a8ff">182.4K</span><span class="fs-i fs-dim"> out · </span><span class="fs-i" style="color:#80b8ef">93%</span><span class="fs-i fs-dim"> cached</span>         <span class="fs-i fs-goal">│</span></span><span><span class="fs-i fs-goal">  ╰────────────────────────────────</span><span class="fs-i fs-dim"> h/l tabs · j/k scroll · Esc close </span><span class="fs-i fs-goal">─╯</span></span></div>

**`/ps`** — Lists active background jobs (see [Tools](tools.md#built-in-tools)).
Each row shows job id, state, command, and elapsed time.

**`/skills [list|reload|trust|untrust]`** — Lists every installed [skill](skills.md) with
its source, folder and description, followed by skills held back as untrusted, overridden
names, warnings, and files that did not load. `reload` rescans the skill folders now. `trust`
lets this repository's skills that run commands or carry hooks be used, and `untrust` takes
that back ([Trusting a repository](skills.md#trusting-a-repository)). While the agent works,
only the list is available.

**`/name [arguments]`** — Starts the [skill](skills.md#using-skills) of that name.

**`/config`** — Shows the active configuration: provider blocks, runtime settings,
and their resolved values.

**`/catalog [status|sync]`** — Check the provider compatibility data and its last update.
`sync` checks for an update now. See [Compatibility catalog](catalog.md).

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

If the new model does not offer your selected effort level, wizolt chooses the nearest one
and tells you. If a model rejects requests, check `/api` against your provider's settings.
You can switch models during a conversation.

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

**`/compact log [seg.N]`** — Browse past summaries, or print one by its segment name.
See [Keeping context manageable](context.md#keeping-context-manageable).

**`/agents`** — Browse agents, their states and context usage. Move the cursor to preview a task
and live reply in a bordered window; Enter switches to that agent. Its conversation, input and
statusbar become active. **x** stops the highlighted agent after confirmation; **Shift+X** stops
it immediately. Background agents keep running. See [Subagents](agents.md).

**`/agents stop-all`** — Stop all running agents. Ctrl-C stops only the selected agent.

**`/language [NAME]`** — Show or force the session's reply language. `auto`, the default,
follows the language you write in; a name like `Chinese` fixes it. The setting lives in the
session rather than the config file, and new subagents inherit it.

## Settings and toggles

**`/set KEY VALUE`** — Set `provider.*` or `runtime.*` for the session; tab-completes both keys
and, where the values are a fixed set, the values. Example: `/set provider.response_timeout 900`.

| Command | Effect |
|---|---|
| `/yolo` | Toggle confirmation prompts — read [Safety](safety.md) before leaving them off |
| `/strict` | Toggle strict tool-call schemas (OpenAI / DeepSeek) |

**`/theme [NAME]`** — Open one appearance picker with Colorscheme, Diff, StatusBar, Divider and Input
tabs. Use `h`/`l` or the left/right arrows to switch tabs, `j`/`k` or up/down to move, and `/`
to search the current list. Moving previews each choice; Enter saves the changes across all
tabs, and Esc cancels them. While searching, Esc leaves search first and then clears the filter.
The StatusBar tab previews in the bottom row; Divider includes both layouts and sweeps, with
idle, running and queued examples. In Divider, Space chooses a layout or sweep and Tab jumps
between the two groups. Changing colors redraws the output already on screen.
Input offers five prompt symbols; `e` edits the ordinary and running prefixes. Enter in the
editor applies the edit and returns to the list; Enter in the list saves.
Choices are saved in your config file.

Choose directly with `/theme NAME`, `/theme diff NAME`, `/theme statusbar NAME`,
`/theme divider NAME`, `/theme sweep NAME` or `/theme input NAME`. See [Appearance](appearance.md) for colors,
layouts and custom templates.

## While a turn runs

Commands that only read the session answer without interrupting it: `/status`, `/ps`,
`/skills`, `/config`, `/catalog`, `/agents`, and `/mcp`'s tool list, along with the `/yolo` toggle.
Any other one waits for the turn to end.

A request that fails transiently — transport, timeout, a 5xx — is retried on its own, up to five
times with a backoff. The divider names the attempt and the cause, `retrying 2/6 · timeout`, and
returns to `working` once the replacement request is running:

<div class="term-shot" role="img" aria-label="The same divider line at three moments of one turn: working with its green waiting pulse and elapsed timer, retrying while a failed request is replaced, then working again as the replacement request runs."><span><span class="fs-i fs-rule">───</span><span class="fs-i" style="color:"> </span><span class="fs-i fs-add">● </span><span class="fs-i fs-working">working (11s)</span><span class="fs-i" style="color:"> </span><span class="fs-i" style="color:#4b5563">───────</span><span class="fs-i" style="color:#61cbdb">─</span><span class="fs-i" style="color:#65deef">─</span><span class="fs-i" style="color:#60c1d1">─</span><span class="fs-i" style="color:#5cadbd">─</span><span class="fs-i" style="color:#5aa3b3">─</span><span class="fs-i" style="color:#56909f">─</span><span class="fs-i" style="color:#548695">─</span><span class="fs-i" style="color:#527c8b">─</span><span class="fs-i" style="color:#517281">──</span><span class="fs-i" style="color:#4f6977">──</span><span class="fs-i" style="color:#4d5f6d">──────</span><span class="fs-i" style="color:#4b5563">─────</span></span><span><span class="fs-i fs-rule">───</span><span class="fs-i" style="color:"> </span><span class="fs-i fs-add">● </span><span class="fs-i fs-working">retrying (12s)</span><span class="fs-i" style="color:"> </span><span class="fs-i" style="color:#67e8f9">─</span><span class="fs-i" style="color:#4b5563">─────────────────────────────</span></span><span><span class="fs-i fs-rule">───</span><span class="fs-i" style="color:"> </span><span class="fs-i fs-add">● </span><span class="fs-i fs-working">working (14s)</span><span class="fs-i" style="color:"> </span><span class="fs-i" style="color:#4b5563">─────</span><span class="fs-i" style="color:#4d5f6d">──────</span><span class="fs-i" style="color:#4f6977">──</span><span class="fs-i" style="color:#517281">──</span><span class="fs-i" style="color:#527c8b">─</span><span class="fs-i" style="color:#548695">─</span><span class="fs-i" style="color:#56909f">─</span><span class="fs-i" style="color:#5aa3b3">─</span><span class="fs-i" style="color:#5cadbd">─</span><span class="fs-i" style="color:#60c1d1">─</span><span class="fs-i" style="color:#65deef">─</span><span class="fs-i" style="color:#61cbdb">─</span><span class="fs-i" style="color:#4b5563">───────</span></span></div>

## MCP

**`/mcp`** — Manage [MCP](mcp.md) server connections. Sub-commands:

| Usage | Effect |
|---|---|
| `/mcp` | List servers and connection status |
| `/mcp connect <server> [server ...]` | Connect servers now |
| `/mcp disconnect <server>` | Disconnect a server |
| `/mcp tools [server]` | List tools from a connected server |

## Help and exit

The prompt lists and completes every command as you type, and the Session tab of `/status` ends
with the link to the [documentation](https://wizolt.readthedocs.io).

**`/exit`, `/quit`** — Leave wizolt. Your session is saved automatically and can
be resumed with `-c` or `--resume`.
