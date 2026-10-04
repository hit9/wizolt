# Getting started

## Install

- wizolt supports <span class="marker">macOS and Linux only</span>
- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) to install and run

```sh
uv tool install wizolt
```

### Upgrade

```sh
uv tool upgrade wizolt
```

wizolt checks PyPI at most once a day and reports an available update at startup and in
`/status`.

## Configure

wizolt needs one thing to start: <span class="marker">a provider to talk to</span>. Generate a
starter config:

```sh
wizolt --init-config
```

This writes `~/.wizolt/config.toml`, plus `~/.wizolt/secrets.toml` for your API keys. Only
the `[provider]` block is required; every other setting has a built-in default, and the file
lists the common ones as comments.

### Point it at a provider

wizolt speaks to any OpenAI-compatible API (and to Anthropic). Open the config and fill in
a provider — for example [DeepSeek](https://api-docs.deepseek.com/):

```toml
[provider]
active = "default"

[provider.default]
url = "https://api.deepseek.com"
model = "deepseek-flash"
```

| Key | Meaning |
|---|---|
| `url` | Base URL of the API |
| `model` | Model name to use |

Then put your API key in `secrets.toml`, under the same entry name:

```toml
default = "sk-..."
```

Keeping keys out of `config.toml` means you can share or commit the config.

You can define several `[provider.<name>]` blocks and switch between them with `active` (or
`/provider` inside a session). See [Configuration](configuration.md#providers) for optional
provider, runtime, and data settings.

## Start a session

```sh
wizolt
```

Type a request in plain language and the agent starts working — reading files, proposing
edits, running commands. Before anything that changes files or runs a command, it asks for
confirmation (unless you pass `--yolo`). You can keep typing while it works; see
[Follow-ups](usage.md#follow-ups).

Exit with `/exit`, `/quit`, or `Ctrl-D`.

## Your first turn

A turn starts with your request and ends with an answer. In between the agent reads what it
needs, asks before it changes anything, and shows you what it did.

```{figure} _static/getting-started-turn.svg
:alt: An illustrative turn: request, file read, proposed diff, approval, test result and final answer.

From your request to a reviewed change.
```

1. **You ask.** Plain language; no special syntax. `Enter` sends.
2. **It works.** Read-only steps — reading, searching, listing — happen without asking. The
   divider counts the elapsed time, and the status bar below shows the model and context fill.
3. **It asks before changing anything.** An edit arrives as a diff with an action row: `Enter`
   approves, `Tab` moves to `Refuse`, and typing anything writes a reason the agent will read.
   Commands work the same way.
4. **It reports.** The answer lands in your scrollback, and the prompt returns.

From here: `/status` shows where the context stands and links to the full documentation. Your
work is saved as you go — close the terminal and `wizolt -c` picks the session back up.

## Command-line flags

| Flag | Effect |
|---|---|
| `-c`, `--last`, `--latest` | Resume the most recent session in this project |
| `--resume [UID]` | Resume a saved session; with no `UID`, resumes this project's latest |
| `--yolo` | Skip confirmation prompts for mutating tools |
| `--theme NAME` | Override the configured color theme: `auto`, `light`, `dark`, or a [named theme](appearance.md#color-themes) |
| `--config <path>` | Use a specific config file instead of `~/.wizolt/config.toml` |
| `--init-config` | Write a starter config file and exit |
| `--migrate-secrets` | Move API keys written in the config into `secrets.toml` and exit |
| `-h`, `--help` | Show command-line help and exit |
| `-v`, `--version` | Print the version and exit |
