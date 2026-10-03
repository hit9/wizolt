# Interaction

wizolt runs as a conversation in your terminal. You type a request, the agent works
through it with [tools](tools.md), and you stay in the loop the whole time — steering,
answering questions, and reviewing changes.

## Follow-ups

You can keep typing while wizolt works. `Enter` submits a follow-up that joins the current task if
another model step begins; otherwise it becomes the next task. `Tab` holds the draft back for a task
of its own after this one, and each `Tab` starts one turn, in order. A draft still in the editor is
never submitted by interrupting — the first `Ctrl-C` discards it instead.

```{figure} _static/usage-followups.svg
:alt: Two queued messages: Enter follows up on this task; Tab holds a separate task for later.

Enter follows up now; Tab starts a task later.
```

A `+` joins the current task at the next model step. A `↪ next turn` starts a separate task
after this one. Sent messages move above the divider into the conversation; if the request
fails, they return to the queue so you can retry.

Input belongs to the agent selected with [`/agents`](agents.md). Enter adds steering input
while that agent is running, or starts its next turn while idle. Tab holds a message for that
agent's next turn. Switching keeps their drafts and queues separate.

| Key | When | Effect |
|---|---|---|
| `Enter` | While the agent works | Queue a follow-up for the next model step |
| `Tab` | While the agent works | Hold the draft for the next task instead of the next model step |
| `Ctrl-C` | While the agent works | Discard a draft in the editor; with the editor empty, interrupt the task — retracting the message if the agent has not answered yet, or recording the interrupt once it has |
| `Ctrl-C` | Idle prompt | Clear input line |
| `Ctrl-U` | Any prompt | Clear the whole input line, leaving the turn running |
| `Up` / `Ctrl-P` | While working, with an empty editor | Recall the newest queued message |

Interrupting splits two ways. If the agent has not answered yet, `Ctrl-C` *retracts* the
message: it is discarded and never reaches the conversation record or the saved session, as
if it was never sent (your input history still recalls it with `Ctrl-P`). Once the agent has
spoken or run a tool, `Ctrl-C` *interrupts*: the work already shown stays, and the turn is
marked as interrupted so wizolt knows it ended early.

Bash and Job live previews stay next to their command header. The input and statusbar
stay at the bottom while output arrives.

## Streaming model output

Model output streams by default in the interactive terminal over OpenAI-compatible Chat
Completions, OpenAI Responses, and Anthropic Messages. Text appears as it arrives above the
divider, and the divider names the phase: `thinking` while the model reasons, if it exposes
reasoning at all, then `responding` while it writes the answer.

<div class="term-shot" role="img" aria-label="The same divider line at three moments of one turn: while reasoning streams above it the label reads thinking, while the answer streams it reads responding, and when the turn completes the preview is replaced by the final answer above the idle prompt."><span class="fs-dim">  I should inspect the existing implementation first.</span><span> </span><span><span class="fs-i fs-rule">───</span><span class="fs-i" style="color:"> </span><span class="fs-i fs-add">● </span><span class="fs-i fs-working">thinking (4s)</span><span class="fs-i" style="color:"> </span><span class="fs-i" style="color:#4b5563">─────────────────────</span><span class="fs-i" style="color:#4f6977">─</span><span class="fs-i" style="color:#67e8f9">─</span><span class="fs-i" style="color:#61cbdb">─</span><span class="fs-i" style="color:#5eb7c7">─</span><span class="fs-i" style="color:#5cadbd">─</span><span class="fs-i" style="color:#589aa9">─</span><span class="fs-i" style="color:#56909f">─</span><span class="fs-i" style="color:#548695">─</span><span class="fs-i" style="color:#527c8b">─</span></span><span> </span><span class="fs-dim">  ⋮</span><span class="fs-dim">  I found the issue in the request path.</span><span> </span><span><span class="fs-i fs-rule">───</span><span class="fs-i" style="color:"> </span><span class="fs-i fs-add">● </span><span class="fs-i fs-working">responding (7s)</span><span class="fs-i" style="color:"> </span><span class="fs-i" style="color:#527c8b">─</span><span class="fs-i" style="color:#548695">─</span><span class="fs-i" style="color:#56909f">─</span><span class="fs-i" style="color:#589aa9">─</span><span class="fs-i" style="color:#5cadbd">─</span><span class="fs-i" style="color:#5eb7c7">─</span><span class="fs-i" style="color:#61cbdb">─</span><span class="fs-i" style="color:#67e8f9">─</span><span class="fs-i" style="color:#4f6977">─</span><span class="fs-i" style="color:#4b5563">─────────────────────</span></span><span> </span><span class="fs-dim">  ⋮</span><span>The retry loop reused a closed client. Reconnecting per attempt fixes it.</span><span class="fs-rule">────────────────────────────────────────────────────────────</span><span class="fs-prompt">&gt; <span class="fs-caret">▏</span></span></div>

When the response finishes, its live preview becomes the final formatted answer.
Streaming is on by default. If your endpoint rejects it, set `stream = false` in its provider
block, or use `/set provider.stream off` for this session.

## Bash output

While Bash runs, its live output stays above the `working` divider. When the command
finishes, up to three lines from each stream stay in the transcript, and the complete result
is stored under its `tr.N` key.

Press **Ctrl-O** to browse results, newest first. Use `j`/`k` or the arrows to select,
`/` to search and **Enter** to open. **Ctrl-O** or `q` closes the list.

| Result | What opens |
| --- | --- |
| Bash | The command and its output |
| Edit | The file's full diff, syntax-highlighted with the added and removed lines banded |
| ToolScript | The complete script and its printed result; a running script appears first |
| Job | Its live log, or the saved result after resuming a session |
| Subagents | Select `/agents` to preview a task and its recent answer |

This viewer also works under `--yolo`, when there is no approval preview.
Very large results show their beginning and end, with a notice when text was shortened.
The stored tool result remains complete.

<div class="term-shot" role="img" aria-label="A completed Bash command with its output tail under the call line, its stored key cited at the end of that row, then the Ctrl-O sheet of recent results: its title on a line of its own over a full-width rule, a blank row under it, the rows lined up in verdict, key, and tool-name columns with the selected row highlighted whole, and the key legend under the last row. Below it, the read-only viewer one of them opens, framed the same way: the title over a rule, then a labeled rule opening each section -- the fields, the output, and the result."><span class="fs-tool">  Bash  pytest -q</span><span class="fs-output">    └ 708 passed in 14.84s</span><span class="fs-dim"> · tr.18 [auto]</span><span> </span><span class="fs-title">  Tool output · latest 4</span><span class="fs-rule">  ────────────────────────────────────────────────────────────────────────</span><span> </span><span><span class="fs-i fs-dim">   1.   </span><span class="fs-i fs-working">running  </span><span class="fs-i fs-tool">ToolScript  </span><span class="fs-i">call 24 lines (938 chars)</span></span><span class="fs-selected">   2. ✓ tr.18    Bash        pytest -q                  </span><span><span class="fs-i fs-dim">   3. </span><span class="fs-i fs-ok">✓ </span><span class="fs-i fs-dim">tr.17    </span><span class="fs-i fs-tool">Bash        </span><span class="fs-i">git diff --check</span></span><span><span class="fs-i fs-dim">   4. </span><span class="fs-i fs-ok">✓ </span><span class="fs-i fs-dim">tr.16    </span><span class="fs-i fs-tool">Bash        </span><span class="fs-i">git status --short</span></span><span> </span><span class="fs-dim">  j/k/Tab move · Ctrl-D/U page · / search · Enter open · Esc/q close</span><span> </span><span class="fs-title">  Output · tr.18 · read-only</span><span class="fs-rule">  ────────────────────────────────────────────────────────────────────────</span><span> </span><span class="fs-dim">  key   tr.18</span><span class="fs-dim">  exit  0</span><span> </span><span><span class="fs-i fs-rule">  ── </span><span class="fs-i fs-title">output</span><span class="fs-i fs-rule"> ──────────────────────────────────────────────────────────────</span></span><span> </span><span>  1   pytest -q</span><span> </span><span><span class="fs-i fs-rule">  ── </span><span class="fs-i fs-title">result</span><span class="fs-i fs-rule"> ──────────────────────────────────────────────────────────────</span></span><span> </span><span class="fs-dim">  stdout:</span><span class="fs-output">    708 passed in 14.84s</span><span class="fs-dim">  ↑/↓ scroll · Ctrl-D/U half-page · PgUp/PgDn page · g/G top/bottom · Esc/q back · Ctrl-O close</span></div>

## Status bar

A single line beneath the prompt summarizes the session. The default layout is:
`[agent] [yolo] provider/model · level | mcp N · skills N | ctx N% · cache N%`.
`[yolo]` appears only when enabled.

Use `/theme`’s StatusBar tab to preview other layouts in that bottom row. Its Divider tab
lets you choose the line above the input and its animation. Enter saves all changes; Esc
cancels them. See [Statusbar and divider](appearance.md#statusbar-and-divider)
for presets, custom templates and sweep formulas.

The statusbar names the selected agent and shows its model, context and cache usage. Its
divider, queue and `/status` use the same agent's statistics. Background agents keep running
without mixing their output into the selected conversation.

Context and cache figures update after each request. A spinner beside `mcp` means connections
are still opening; `mcp 0` without a spinner means none are connected.
Use `/status` for more detail.

The working divider above the prompt names the current phase — `thinking`, `responding`, or
`web search` while a [provider-side tool](tools.md#provider-side-tools) runs inside the request —
with the time spent so far beside it, and an estimated output speed while text is arriving:
`responding (12s · ↓ 48 tok/s)`. The `↓` marks the speed as the model's incoming stream; it is
still an estimate, and it disappears between requests and on providers that do not stream.

<div class="term-shot" role="img" aria-label="The main agent statusbar: selected agent, yolo, model, reasoning, connected services and independent context and cache usage."><span><span class="fs-i sb-agent">[main] </span><span class="fs-i sb-yolo">[yolo] </span><span class="fs-i sb-provider">dashscope/</span><span class="fs-i sb-model"><b>qwen3.7-plus</b></span><span class="fs-i sb-sep"> · </span><span class="fs-i sb-reason">high</span><span class="fs-i sb-sep"> | </span><span class="fs-i sb-mcp">mcp 2 · skills 3</span><span class="fs-i sb-sep"> | </span><span class="fs-i sb-ctx">ctx 23%</span><span class="fs-i sb-cache"> · cache 98%</span></span></div>

## Quick hints

After an answer, the model may suggest two or three next steps as chips under the prompt. They are
suggestions, not commands: ignore them and keep typing, or take one. Chips flow left to right,
up to three per line, and wrap to new lines when the terminal is too narrow. A suggestion too long
for a chip is drawn shortened; picking it puts the whole suggestion in the input.

<div class="term-shot" role="img" aria-label="The idle prompt after an answer: the answer text, an empty prompt with a caret, and one row of three suggestion chips separated by grey bars, the middle one highlighted in reverse."><span>Everything is ready to review.</span><span> </span><span class="fs-rule">────────────────────────────────────────────────────────────</span><span class="fs-prompt">&gt; <span class="fs-caret">▏</span></span><span> </span><span><span class="fs-i fs-sel"> run the tests </span><span class="fs-i fs-dim"> │ </span><span class="fs-i fs-tab-on"> show the diff </span><span class="fs-i fs-dim"> │ </span><span class="fs-i fs-sel"> commit the work </span></span></div>

Press **Tab** to focus a suggestion, then **Enter** to put it in your draft. You can combine
several suggestions before pressing Enter in the input to send. A `✓` marks a selected chip;
select it again to remove its text. You can also ignore the chips and type your own request.

While completing a command or mention, Tab keeps its usual completion behavior.

## Commands

Type `/` commands at the prompt to inspect state, switch models, manage the
session, or configure runtime behavior on the fly. As you type, the first match is highlighted,
so `/st` and `Enter` runs `/status`. The command word is coloured as you type: the theme's
accent once it names a command, red once nothing matches it. A command that needs an argument,
such as `/set`, fills in instead and lists its arguments, down to the value. See the
[command reference](commands.md) for the full list.

## Mentions

Mentions pull something into the turn. Type `@` at the prompt for a list of the four kinds;
picking one opens its candidates, which narrow as you keep typing, and picking a connected MCP
server opens its tools. The first candidate is highlighted as you type; `↑`/`↓` or
`Ctrl-N`/`Ctrl-P` move the highlight and `Tab` fills in the highlighted row. `Enter` commits it
into the input without sending, and a second `Enter` sends. `Esc` closes the menu and puts back
what you typed. A mention you have typed in full,
such as `$release` or a whole `@mcp:server`, has nothing highlighted, and `Enter` sends it.

<div class="term-shot" role="img" aria-label="The mention menu in two moments. After typing an at sign the prompt lists the four kinds with a one-line description each. After choosing at-file the file picker opens between two rules, with its own query line, a match counter, a key hint, and two ranked file paths, the first one pointed at."><span class="fs-prompt">&gt; add tests for @<span class="fs-caret">▏</span></span><span><span class="fs-i">                </span><span class="fs-i"> @file:   </span><span class="fs-i fs-dim"> files in this repo </span></span><span><span class="fs-i">                </span><span class="fs-i"> @mcp:    </span><span class="fs-i fs-dim"> MCP servers and tools </span></span><span><span class="fs-i">                </span><span class="fs-i"> @skill:  </span><span class="fs-i fs-dim"> installed skills </span></span><span><span class="fs-i">                </span><span class="fs-i"> @agents.md: </span><span class="fs-i fs-dim"> AGENTS.md instructions </span></span><span> </span><span class="fs-prompt">&gt; add tests for @file:<span class="fs-caret">▏</span></span><span class="fs-divider">  ──── file picker ─────────────────────────────────</span><span><span class="fs-i fs-sel">  files&gt; </span><span class="fs-i">mention</span><span class="fs-i fs-caret">▏</span></span><span class="fs-dim">      3/812</span><span class="fs-dim">      Ctrl-N/P or ↑/↓ move · Enter select · Esc close</span><span class="fs-sel">  &gt;   tests/test_mentions.py</span><span>      wizolt/mentions.py</span><span class="fs-divider">  ──────────────────────────────────────────────────</span></div>

| Mention | Also written | Effect |
|---|---|---|
| `@file:path` | — | Points the agent at a file in the project |
| `@mcp:server`, `@mcp:server.tool` | `@server`, `@server.tool` | Connects an [MCP](mcp.md) server on demand; a `.tool` suffix connects the whole server too, and its tools join the request index. <span class="marker">The connection remains active until you disconnect it.</span> |
| `@skill:name` | `$name` | Points the agent at a [skill](skills.md); it loads the instructions when they matter |
| `@agents.md:` | — | Cites your AGENTS.md instructions for this request: everything, your own or the project's (`global`/`project`), or one section by heading |

**Instructions.** Put your preferences in `~/.wizolt/AGENTS.md` and project rules in the
project's `AGENTS.md`. Start a new session after editing them. Type `@agents.md:` to pick a
file or section for one request. See [agents.md](https://agents.md) for the file convention.

**Files.** Type `@file:` to open the file picker, or press `Tab` while editing a file mention.
It includes hidden and untracked files, respects Git ignores, and quotes paths with spaces
for you. The picker uses your chosen colorscheme. A mention points at the file; the agent
reads the parts it needs.

Mentions are expanded in follow-ups queued while the agent is working, too.

## Keys and input editing

**Interactive selectors** (model picker, MCP manager, diff viewer) support:

- `j` / `k` or arrow keys to move
- `g` / `G` to jump to top / bottom
- `/` to search, `Enter` to accept, `Esc` to cancel

Long inline lists scroll as you move, leaving a few rows of recent output visible above them.

**The input line** supports:

- `Enter` — send what you typed
- `Ctrl-J` — insert a newline, so a draft can span several lines; `Esc` then `Enter` still
  inserts one too
- history recall and completion
- `Ctrl-C` — clear the current input; with the input empty while running, interrupt the turn (retracting it if the agent has not answered yet)
- `Ctrl-U` — clear the whole input line, in the idle prompt and the follow-up editor alike
- `Ctrl-D` — exit from an empty prompt
- `Ctrl-R` — reverse-search your history; `Enter` puts the match in the input to edit, a second
  `Enter` sends it
- `Ctrl-O` — browse the selected agent's recent Bash outputs, ToolScript scripts, background
  Job logs and edit diffs; press it again to close
- `Ctrl-X Ctrl-E` or `Ctrl-G` — edit the current input in `$VISUAL` / `$EDITOR` (falls back to
  vim), as a temporary Markdown file

```{figure} ../snapshots/wizolt-working-input-editor.png
:alt: Editing a follow-up message in an external editor
:width: 600px
:align: center

Typing a follow-up message in an external editor.
```

When you open the editor in reply to the agent, its most recent reply is appended below a git-style scissors line, so you can read what you are answering while you compose (the full-screen editor hides that scrollback):

<div class="term-shot" role="img" aria-label="The message open in vim: numbered lines holding the draft with the cursor at its end, a blank line, the scissors line with its unique marker, two comment lines explaining that everything below is stripped — the first wrapping at the window edge — then the agent's most recent reply, filler tildes, a status line naming the modified temporary Markdown file with the cursor position, and the INSERT mode message."><span><span class="fs-i fs-vim-fill">  1 </span><span class="fs-i">yes, add the reconnect test and cap the backoff at 30s</span><span class="fs-i fs-caret">▏</span></span><span><span class="fs-i fs-vim-fill">  2 </span><span class="fs-i">&nbsp;</span></span><span><span class="fs-i fs-vim-fill">  3 </span><span class="fs-i fs-dim"># ------------------------ &gt;8 ------------------------ (4f2a9c1b77d0)</span></span><span><span class="fs-i fs-vim-fill">  4 </span><span class="fs-i fs-dim"># Reference only: everything below the scissors line is stripped before </span></span><span><span class="fs-i fs-vim-fill">    </span><span class="fs-i fs-dim">your</span></span><span><span class="fs-i fs-vim-fill">  5 </span><span class="fs-i fs-dim"># message is sent. The agent&#x27;s most recent reply follows for reference.</span></span><span><span class="fs-i fs-vim-fill">  6 </span><span class="fs-i">&nbsp;</span></span><span><span class="fs-i fs-vim-fill">  7 </span><span class="fs-i">I split McpManager into StdioTransport and HttpTransport.</span></span><span><span class="fs-i fs-vim-fill">  8 </span><span class="fs-i">Want me to add a test for the reconnect path?</span></span><span><span class="fs-i fs-vim-fill">    </span><span class="fs-i fs-vim-fill">~</span></span><span><span class="fs-i fs-vim-fill">    </span><span class="fs-i fs-vim-fill">~</span></span><span class="fs-vim-status">wizolt-input-a1b2c3d4.md [+]                  markdown    1,55     All</span><span class="fs-dim">-- INSERT --</span></div>

Everything from the scissors line down is stripped before the message is sent; a scissors line you type yourself is left untouched. Long replies are capped to their most recent lines.

### Image input

Paste or type the path of an existing local image directly into the prompt. wizolt replaces
the path with an inline label such as `[Image #1 · screenshot.png]`, so you can see exactly which
images will be submitted while continuing to edit the surrounding text. Relative paths resolve
from the workspace; quoted paths and backslash-escaped spaces are accepted.

<div class="term-shot" role="img" aria-label="The input prompt after recognizing a local screenshot path as an editable inline image label."><span class="fs-prompt">&gt; explain <span class="fs-i fs-sel">[Image #1 · screenshot.png]</span> and fix the layout<span class="fs-caret">▏</span></span></div>

PNG, JPEG, WebP and single-frame GIF files are supported. Images normally go to your active
model. If it cannot accept images, a configured [vision model](configuration.md#vision-model)
can describe them for it, at the cost of an extra request. You can also ask the agent to inspect
a local image with `ViewImage`.

## Sessions

<span class="marker">Your work is saved automatically</span> — the conversation, edits, and diffs
are tied to the project directory you started in, so an interrupted session picks up where it
stopped. Sessions untouched for seven days are removed by default, swept in the background when
wizolt starts; it reports how many it removed. Resuming a session resets its clock, so one you
keep returning to is never removed. Set `runtime.session_retention_days = 0` to keep them
indefinitely.

Resume from the command line:

```sh
wizolt -c              # resume the latest session in this project
wizolt --resume        # same, explicit
wizolt --resume UID    # resume a specific session by id, from any directory
wizolt --resume "fd leak"   # or by name, or by the first few characters of an id
```

Sessions are stored per project, so `-c` and a bare `--resume` never reach into another project's
history — even when your most recent session anywhere was somewhere else. A `UID` is looked up
across every project, so you can resume one by id from wherever you are.

A name or id prefix is searched in the current project first, then everywhere — you can resume a
session by name after moving directories. When a query matches more than one session, wizolt
<span class="marker">lists the candidates instead of guessing</span> between them.

Resuming replays the conversation into your scrollback, including the diff each edit made. Long
diffs are trimmed there.

A session can be open in only one wizolt at a time. If it is already in use, close the other
instance or choose another session.

### Names

Every session has a name, so you have something to recognize it by later. It starts as the first
line you typed, becomes the agent's current goal once it has one, and stays whatever you set with
`/name`:

```text
/name                 # show the current name and where it came from
/name auth refactor   # set your own; nothing overwrites it afterwards
```

A name you set stays until you change it. Sessions may share a name; when several match,
wizolt lists them so you can choose.

### Switching sessions

`/sessions` — or `/resume`, the same command — lists what is saved, newest first, with how long ago
each was touched and how many rounds it ran. Type to filter across names and opening lines, and
press Enter to re-enter one:

<div class="term-shot" role="img" aria-label="The session picker: a searchable list of saved sessions, each showing its name, age, and round count, with the current session marked, above a preview of the highlighted session's id, opening message, and directory."><span class="fs-divider">──── Sessions ─────────────────────────────</span><span class="fs-sel">&gt; port the tool runner to asyncio<span class="fs-i fs-dim">  ·  2h ago · 14 rounds</span></span><span class="fs-dim">  split the large test modules<span class="fs-i fs-dim">  ·  yesterday · 31 rounds</span></span><span class="fs-dim">  fix the fd leak in MCPFileTokenStore<span class="fs-i fs-dim">  ·  3d ago · 1 round</span></span><span class="fs-dim">  what I am doing right now<span class="fs-i fs-dim">  ·  just now · 2 rounds · current</span></span><span> </span><span class="fs-dim">  uid   20260728074943-e22e69e8-070</span><span class="fs-dim">  start port the tool runner to asyncio, starting with Bash</span><span class="fs-dim">  where ~/dev/github/wizolt</span><span> </span><span class="fs-hint">  ↑/↓ or j/k move · / search · Enter open · Esc close</span></div>

`/sessions all` widens the list past the current project, adding each session's directory to its
row. Choosing a session ends the current one — it is saved first — and starts the next in its
place, exactly as if you had launched with `--resume`. Choosing the session you are already in, or
pressing `Esc`, changes nothing.

Run it from the prompt between turns. While the agent is working, wizolt says so and asks you to
press `Ctrl-C` first: switching sessions mid-turn would abandon a request already in flight.

Sessions saved before names existed list under their id until the next time they are saved.

### Reviewing changes

Each edit appears in the transcript with the diff it made, and a resumed session replays those
diffs from its log rather than from the files — trimmed to a readable window, with the whole diff
one `Ctrl-O` away. That record is per agent: it says what this agent changed. For the workspace as
a whole, including work outside wizolt, use Git.

### Long sessions

wizolt keeps long conversations within a working budget on its own, summarizing older
context as needed so a session can run indefinitely. Run `/compact` to trim it now, or
`/status` to see current context and token usage.
