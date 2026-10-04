<h1 align="center">
  <img src="https://raw.githubusercontent.com/hit9/wizolt/master/docs/_static/wizolt-logo.png" alt="Wizolt logo: a blue hooded terminal mage holding a lightning staff" width="112"><br>
  wizolt
</h1>

<p align="center">
  A terminal coding agent. Read code, edit files, run commands — and pick up where you left off.
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/hit9/wizolt/master/snapshots/wizolt3.gif" alt="wizolt working through a long session with background jobs, an edit preview, and a live status bar" width="600">
</p>

## Highlights

- **Keep long tasks going.** Automatic context compaction, prompt caching on supported providers, and saved sessions. Resume conversation, tool history, and diffs with `wizolt -c`.
- **Review every change.** Edits reject stale or ambiguous targets. `/diff` shows the latest round or the whole session.
- **Steer while it works.** Send follow-ups, queue the next task, and let slow commands run as background jobs.
- **Mix models.** Connect OpenAI-compatible Chat Completions, Responses, or Anthropic Messages APIs. Run parallel subagents with independent conversations and model settings.
- **Bring your tools.** MCP servers, reusable Markdown skills, and shell hooks for checks and formatters.
- **Make it yours.** Preview themes, diff colors, statusbars, dividers, and input symbols in `/theme`; customize further in your config.

## Install

Requires macOS or Linux, Python 3.11+, and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install wizolt
wizolt --init-config
```

Add your provider to `~/.wizolt/config.toml`:

```toml
[provider]
active = "default"

[provider.default]
url = "https://api.deepseek.com"
model = "deepseek-flash"
```

and its key to `~/.wizolt/secrets.toml`, so the config stays shareable:

```toml
default = "sk-..."
```

Start in your project directory:

```sh
wizolt
```

Upgrade with `uv tool upgrade wizolt`.

**No built-in sandbox.** wizolt can edit files and run commands. Use a container or VM when you need isolation.

## Links

- [Documentation](https://wizolt.readthedocs.io/en/latest/) — full usage guide and reference.
- [Maintainer design notes](design/README.md) — architecture, state ownership and known trade-offs.
- [Blog post](https://hit9.dev/post/nanocode) — why and how it was built.
