# Configuration

wizolt reads `~/.wizolt/config.toml` by default, and your API keys from `secrets.toml` beside
it. Generate a commented starter of both with `wizolt --init-config`, or point at another config
with `--config <path>`.

<span class="marker">Only the `[provider]` block is required.</span> Every other key falls back to
a built-in default, so a minimal config is just a provider. Inspect the resolved configuration
at any time with `/config`.

| I want to… | Start here |
| --- | --- |
| Connect a model | [Providers](#providers): URL, key and model |
| Change colors or input symbols | [Appearance](appearance.md): `/theme` |
| Adjust limits and session retention | [Runtime](#runtime) |
| Use another model for summaries | [Compaction model](#compaction-model) |
| Add external tools | [MCP](mcp.md) |
| Run a formatter automatically | [Hooks](hooks.md#configuring-hooks) |

## Providers

wizolt supports OpenAI-compatible Chat Completions and Responses APIs, plus the Anthropic
Messages API. Define one or more `[provider.<name>]` blocks and select one with
`[provider] active`:

```toml
[provider]
active = "default"

[provider.default]
url = "https://api.deepseek.com"
model = "deepseek-flash"
```

With the key from [API keys](#api-keys), these fields are enough for most endpoints. Use `/config` to check the active settings.
If an endpoint needs an adjustment, set the relevant option below; your settings take precedence.

Define additional blocks to use more providers. Switch between them with `/provider [NAME]`, and
switch the active model with `/model [MODEL]`.

### API keys

Keys live in `secrets.toml`, next to the config, one line per provider entry:

```toml
default = "sk-..."
deepseek = "sk-..."
```

`config.toml` then holds no secrets and can be shared or committed. `--init-config` creates
`secrets.toml` readable only by you; keep it that way, and keep it out of version control. With
`--config <path>`, wizolt reads the `secrets.toml` in that file's directory. A line naming an
entry the config does not define is ignored.

A `key` written in a `[provider.<name>]` block still works, and wins over `secrets.toml`. At
startup wizolt offers to move such keys into `secrets.toml` (`[Y/n]`). If you answer `n`, it only
prints a reminder until you add or change a key. `wizolt --migrate-secrets` moves them without
asking.

### API protocol

Leave `api = "auto"` unless your endpoint needs an explicit protocol:

| Value | Meaning |
|---|---|
| `auto` | Infer the protocol when possible; otherwise use Chat Completions |
| `chat` | OpenAI-compatible Chat Completions |
| `responses` | OpenAI-compatible Responses |
| `anthropic` | Anthropic-compatible Messages |

A URL ending in `/chat/completions`, `/responses`, or `/messages` also selects that protocol.

### Optional provider settings

Most users can leave these unset.

| Key | Default | Meaning |
|---|---|---|
| `api` | `auto` | API protocol shown above |
| `stream` | `true` | Stream model output; disable for endpoints that reject streaming or Chat `stream_options` |
| `reasoning` | `medium` | Reasoning effort: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`; change it during a session with `/reason` |
| `available_models` | — | Additional models shown by `/model` |
| `temperature` | — | Sampling temperature; omitted by default |
| `max_tokens` | `0` | Maximum answer length, including reasoning; `0` uses the provider default. A larger cap leaves less room for conversation |
| `max_context_tokens` | `0` | How much of *this* entry's model window to use; `0` inherits `runtime.max_context_tokens`. Set it per entry when entries point at models with different windows |
| `timeout` | `120` | Transport inactivity timeout in seconds |
| `response_timeout` | `600` | Total generation limit in seconds; `0` disables it |
| `prompt_cache_key` | `auto` | Stable prompt-cache key; set `off` to omit it |
| `strict_tools` | `false` | Request strict function schemas where supported; toggle with `/strict` |
| `headers` | `{}` | Extra HTTP headers sent with every request to this entry; see below |
| `omit_body` | `[]` | Request fields this endpoint rejects; see below |
| `extra_body` | `{}` | Extra request fields required by your endpoint; nested objects are merged |
| `builtin_tools` | `[]` | Tools the provider runs itself, passed through verbatim; see below |
| `chat_reasoning` | `auto` | Provider-specific Chat reasoning format; normally leave on `auto` |
| `reasoning_history` | `auto` | How much earlier reasoning to send: `auto`, `all`, `current_turn`, or `tool_calls`; usually leave on `auto` |

### Extra HTTP headers

Some features live in the header rather than the request body, where `extra_body` cannot reach
them. `headers` sends whatever the provider documents alongside every request from that entry:

```toml
[provider.cmd]
url = "https://api.commandcode.ai/provider/v1"
model = "deepseek/deepseek-flash"
headers = { x-cmd-zdr = "1" }   # Command Code: route only to zero-retention upstreams
```

Values are ASCII strings or plain integers. The entry's key still supplies authentication, so a header is
only needed for what the provider documents separately — zero-retention routing, a gateway's
tenant or routing key. The same headers are used when `/model` asks the endpoint for its model
list. `/config` lists the headers in effect.

### Fields an endpoint rejects

Some endpoints answer `400` for a field wizolt sends. Name it in `omit_body` and it is left out:

```toml
[provider.gw]
omit_body = ["reasoning_effort", "stream_options"]
```

`extra_body` is the other half — it adds fields, `omit_body` removes them. A name is dropped
wherever it sits in the request. `model`, `messages`, `input`, and `stream` cannot be removed;
use `stream = false` when an endpoint does not stream. `/config` lists what is being omitted.

Streaming is enabled by default for all three protocols. If a compatible endpoint does not
support it, set `stream = false` in that provider block, or use `/set provider.stream off` for
the current session.

`timeout` detects a connection that stops delivering data. Streaming reasoning can keep that
timer active indefinitely, so `response_timeout` separately limits the complete model response to
ten minutes by default. Reaching the total limit cancels the request without automatic retries;
set it to `0` only when deliberately allowing unbounded generations.

`/reason` offers the levels the active model documents, and sends the one you pick. DeepSeek
models offer `off, low, high, max`; a model wizolt has no evidence about keeps the full scale
(`minimal, low, medium, high, xhigh, max`). Unknown OpenAI-compatible endpoints and model names
stay on the generic path rather than an allowlist; set `api` and `chat_reasoning` explicitly if
automatic selection is wrong. `/config` lists the levels in `provider.supported_reasoning`, while
`/status` shows the active model and cache usage reported by the provider.

When the list is shorter than the full scale, `/reason` shows why underneath it, with the page it
came from:

```
Reasoning effort
   1. off - disable reasoning
   2. low
   3. high
   4. max
  ──────────────────────────────────
  │ Why these levels
  │ DeepSeek documents low/high/max; medium and xhigh are served as high
  │ https://api-docs.deepseek.com/guides/thinking_mode/
```

Switching model or provider can leave an effort the new model has no level for. wizolt moves it
to the nearest one and says so once:

```
Reasoning medium is not offered by deepseek-flash, using high
```

### Effort levels a model accepts

When wizolt's list is wrong for your model, say what the model accepts. Write the levels weakest
first, under a model name or a glob:

```toml
[provider.gw.models]
"gpt-5.6*" = { reasoning = ["low", "medium", "high", "ultra"] }
```

Those levels become what `/reason` offers for models the glob matches, replacing wizolt's own.
They can include names wizolt does not know — `ultra` above — since they are sent as written.
Each list must contain unique levels and must not include `off`; wizolt adds `off` unless the
catalog documents that the model always reasons. Subagent and compaction reasoning overrides use
the scale declared for their effective model too.

## Provider-side tools

Some providers can run web search themselves; see
[Provider-side tools](tools.md#provider-side-tools) for what that looks like in a session. List
the ones you want in `builtin_tools`, written the way your provider documents them:

```toml
[provider]
url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen3-max"
api = "responses"
builtin_tools = [{ type = "web_search" }, { type = "web_extractor" }]
```

| Provider | Entry |
|---|---|
| OpenAI (Responses) | `{ type = "web_search" }`, optionally with `search_context_size` or `filters` |
| Qwen (Responses) | `{ type = "web_search" }`; also `web_extractor` |
| Anthropic | `{ type = "web_search_20250305", name = "web_search", max_uses = 5 }` |
| Z.AI / BigModel | `{ type = "web_search", web_search = { enable = "True" } }` |
| Kimi / Moonshot | `{ type = "builtin_function", function = { name = "$web_search" } }` |
| OpenRouter | `{ type = "openrouter:web_search" }`; also `openrouter:web_fetch`, `openrouter:datetime` |

One provider configures search elsewhere, through [`extra_body`](#optional-provider-settings):
Qwen's Chat Completions endpoint takes `enable_search`. DeepSeek has no web search.

Builtin tools only work with the APIs shown in the table. If you switch to another API, wizolt
keeps the setting but does not send those tools; switching back enables them again. Use `/config`
to check whether they are active. If wizolt reports an unsupported entry, compare it with the
example for your provider.

## Vision model

An optional `[vision]` entry gives image input a fallback when the active model is text-only:

```toml
[vision]
provider = "vision"

[provider.vision]
url = "https://api.deepseek.com"
model = "deepseek-flash"
```

Images normally go to the active model. When it is known to be text-only, or rejects an image
request, this provider describes the image and the active model receives the description.
`ViewImage` uses the same fallback. Each description costs one extra model request.
Without a vision provider, an incompatible model reports its own error.

## Hooks

Use `[hooks]` to run shell commands at session, tool, turn, subagent and compaction boundaries.
For example, record failed Bash calls:

```toml
[[hooks.PostToolUseFailure]]
matcher = "Bash"
hooks = [{ type = "command", command = "~/bin/log-tool-failure.sh", timeout = 10 }]
```

Each hook has its own `timeout` in seconds (default 60), independent of `runtime.shell_timeout`.
wizolt waits for matching commands to finish. See [Hooks](hooks.md) for all 13 events and their
input and output formats; `/status` shows the number of active hooks.

## Runtime

Optional; the defaults shown are used when omitted.

| Key | Default | Meaning |
|---|---|---|
| `yolo` | `false` | Skip ordinary tool confirmations; creating a subagent or sending it a task still requires approval |
| `max_context_tokens` | `262144` (256K) | Default for every provider entry that does not set its own `max_context_tokens`. How much of the model's context window to use, which sets the automatic-compaction budget — a budget, not the window's size: raise it for a 1M-window model, lower it for a smaller one |
| `max_agent_steps` | `400` | Maximum tool steps in one turn |
| `shell_timeout` | `60` | Maximum shell-command lifetime, in seconds |
| `bash_wait_timeout` | `10` | Foreground wait before a running command becomes a background job; `0` disables promotion |
| `bash_output_tokens` | `6000` | Bash output the agent sees inline, `1000`–`6000`; the rest stays in a file it can open. Lower saves tokens on every later request ([context](context.md)) |
| `max_parallel_tools` | `4` | Maximum read-only tool calls executed concurrently; `1` disables parallelism |
| `max_subagents` | `3` | Retained children across the whole agent group, excluding main; `0` disables creation, maximum `32`. Change it from main with `/set runtime.max_subagents NUMBER` |
| `session_retention_days` | `7` | Delete saved sessions untouched for this many days, swept in the background at startup; `0` keeps them indefinitely |
| `theme` | `auto` | Color theme: `auto`, `light`, `dark`, or a named theme (see [Color themes](appearance.md#color-themes)); overridden by `--theme`, and set for you by `/theme`. `auto` asks the terminal for its background color, then reads `COLORFGBG`, and falls back to `dark` |
| `language` | `auto` | Force the reply language (`auto` follows your messages and injects nothing); set a name like `Chinese` to append a fixed `LANGUAGE OVERRIDE` block to the system prompt. Change for the current session with `/language` |
| `attribution` | `true` | Ask the model to end every commit message and pull-request body it writes with `Generated with [wizolt](https://wizolt.readthedocs.io).` — a prompt-level request, not a guarantee. `/set runtime.attribution off` stops it for the session, `false` for good |
| `agents_md` | `true` | Inject your instructions into every request, as one block of their own ahead of the skills and MCP indexes: the global `~/.wizolt/AGENTS.md` followed by the project's `AGENTS.md` files (each falling back to `CLAUDE.md`) from the repository root down to the working directory, together bounded to about 8,000 tokens |

Selected tuning values can be changed for the current session with `/set` (Tab completion
lists the supported keys). `/yolo` toggles `yolo`.

### Color themes

Use `/theme` to preview and select colors. See [Appearance](appearance.md#color-themes) for
built-in themes. Define your own colors under `[ui.themes.NAME]` in this file, or keep them in
`<data_dir>/themes/NAME.toml`. Both appear in `/theme`; the config wins if names match.
See [Custom appearance](appearance-reference.md#define-a-theme-in-your-config) for an example.

## Statusbar and divider

Use `/theme`’s StatusBar and Divider tabs to preview and save layouts and animations. Configure them
with `[ui.statusbar] format` and `[ui.divider] format` / `sweep`.
See [Appearance](appearance.md#statusbar-and-divider) for presets, or
[Custom appearance](appearance-reference.md) for templates and sweep formulas.

The Input tab also offers prompt symbols and editable prefixes, saved as `[ui.input] prompt`
and `running`. See [Input symbols](appearance.md#input-symbols).

## Subagents

[Subagents](agents.md) are available by default, with up to three children per session. Each
starts with the creating agent's model and settings. Select one with `/agents` and use `/model`,
`/provider` or `/reason` to change its model. Each agent makes its own model requests.

## Compaction model

Summaries use your active model by default. Set `[compaction]` to use a different model, such
as a cheaper one. Leave a field empty to inherit it from the selected provider.

| Key | Default | Meaning |
|---|---|---|
| `[compaction] provider` | inherit | Base provider entry key; empty = the active provider |
| `[compaction] model` | inherit | Override the entry's model; empty inherits |
| `[compaction] reasoning` | inherit | Override the entry's reasoning effort; empty inherits |
| `[compaction] api` | inherit | Override the entry's wire protocol; empty inherits |

To choose a summarizer for one provider, add `[provider.NAME.compaction]`. It accepts `model`,
`reasoning` and `api`, and takes precedence over `[compaction]`:

```toml
[provider.anthropic]
model = "claude-..."

  [provider.anthropic.compaction]
  model = "claude-haiku-..."
```

Use a named provider entry for this form, as shown above.

To send summaries to another provider:

```toml
[provider]
active = "anthropic"

[provider.anthropic]
url = "https://api.anthropic.com/v1"
model = "claude-..."

[provider.deepseek]
url = "https://api.deepseek.com/v1"
model = "deepseek-..."

[compaction]
provider = "deepseek"
model = "deepseek-...-flash"
reasoning = "off"
```

Two things to check when picking a summarizer:

- **Its window must fit the span being compacted.** A small-window model asked to summarize a
  large conversation fails every time, and the context is then trimmed without a summary.
- **Its `response_timeout` applies to the summary.** A slow summarizer holds up the turn for that
  long before giving up. Lower it on that entry if you would rather trim early than wait.

`/config` shows the effective `compaction.*` values, and `/compact log` records which model
produced each stored segment. Summary
tokens are counted apart from the conversation, under **Compaction** on the Usage tab of
`/status`, so each group can be read against one model's price.

## Data location

```toml
[paths]
data_dir = "~/.wizolt"   # sessions, input history, OAuth tokens, user skills, themes, update cache
```

Sessions live under `<data_dir>/projects/`, grouped by working directory. `wizolt -c` resumes
the latest session in your current project. See [Sessions](usage.md#sessions) for retention
settings and other ways to resume.

`<data_dir>/history.txt` holds the main conversation's input history that Up and Ctrl-P recall,
across sessions and projects. Each child agent recalls only its own inputs, saved beside its
session log. These histories are capped at 512 KB, keeping the most recent entries.
