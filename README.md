<h1 align="center">
  <img src="https://raw.githubusercontent.com/hit9/wizolt/master/docs/_static/wizolt-logo.png" alt="Wizolt logo: a blue hooded terminal mage holding a lightning staff" width="112"><br>
  wizolt
</h1>

<p align="center">
  <img src="https://raw.githubusercontent.com/hit9/wizolt/master/snapshots/wizolt3.gif" alt="wizolt working through a long session with background jobs, an edit preview, and a live status bar" width="600">
</p>

<p align="center">
  A terminal coding agent I use, maintain, and customize, shipped as a self-contained Python package.
</p>

## Safety

**Use at your own risk.** wizolt can edit files and run shell commands in the environment where it starts. It does not provide sandbox isolation; use a container or VM when needed.

## What it is

wizolt does not introduce a new kind of coding agent. It combines familiar features — reading and editing files, running commands, follow-ups, sessions, diffs, MCP, and skills — into a tool I use personally.

It works on real repositories, including its own: I use wizolt to build and maintain wizolt. Everything ships in one self-contained Python package, so I can change the behavior directly whenever I want the workflow to work differently.

Wizolt is the former minacode, which began as the single-file nanocode. The implementation outgrew both earlier names; the project history remains continuous.

<p align="center">
  <img src="https://raw.githubusercontent.com/hit9/wizolt/master/snapshots/wizolt2.gif" alt="wizolt resuming a saved session" width="600">
</p>
<p align="center"><sub>Resuming a saved session with its conversation and tool history.</sub></p>

## Highlights

- **Prompt caching:** stable request prefixes help supported providers reuse earlier work, including during compaction. Check reported cache usage with `/status`.
- **Continuity for long tasks:** automatic compaction carries working notes and recent tool activity forward, with older details available for recall. Resume saved conversation, tool history, and diffs with `-c` or `--resume`.
- **Code navigation:** find definitions, callers, references, and implementations through a searchable code index.
- **Precise edits:** target a numbered source view or an exact, unique text match. Stale or ambiguous targets are rejected before the edit is applied.
- **Review changes in place:** `/diff` shows both the latest round's changes and the net result of the session, without leaving the terminal.
- **Steer work as it happens:** send a follow-up with `Enter`, hold a separate task with `Tab`, or interrupt with `Ctrl-C` once the draft is empty.
- **Background commands:** let long-running commands continue as jobs, inspect their output, and wait for or stop them when needed.
- **Worker delegation:** give a bounded task to a worker with its own provider and reusable context. Configure it with `/worker`; the agent delegates through `Delegate`.
- **Choose your provider:** use OpenAI-compatible Chat Completions or Responses APIs, or Anthropic Messages. Optional provider-side web search shows searches and sources in the transcript.
- **MCP and skills:** connect external tools through MCP and load reusable Markdown instructions on demand.

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
key = "sk-..."
model = "deepseek-v4-flash"
```

Then run:

```sh
wizolt
```

Upgrade with `uv tool upgrade wizolt`.

## Links

- [Documentation](https://wizolt.readthedocs.io/en/latest/) — full usage guide and reference.
- [Blog post](https://hit9.dev/post/nanocode) — why and how it was built.
- [code-symbol-index](https://github.com/hit9/code-symbol-index) — the code index library wizolt uses.
