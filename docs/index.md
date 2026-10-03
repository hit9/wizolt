# wizolt

<div class="home-tagline">
  <p>A terminal coding agent</p>
  <iframe
    class="github-star"
    src="https://ghbtns.com/github-btn.html?user=hit9&amp;repo=wizolt&amp;type=star&amp;count=True&amp;size=large&amp;v=2"
    title="Star hit9/wizolt on GitHub"
    width="170"
    height="30"
    scrolling="no"
    frameborder="0"
  ></iframe>
</div>

wizolt works in your terminal: you describe a task, and it reads code, edits files, runs
commands, and reports back. Review changes as it works, add a follow-up at any time, and
<span class="marker">resume where you left off</span> when you return.

Wizolt is the former minacode, which began as the single-file nanocode. The project history remains
continuous across those names.

```{figure} ../snapshots/wizolt3.gif
:alt: wizolt working through a long session: background jobs, an edit preview, and a live status bar
:width: 600px
:align: center

A long session with background jobs, edits, and the live status bar.
```

```{admonition} Use at your own risk
:class: warning
wizolt edits files and runs shell commands in the directory where you start it. It has
**no sandbox of its own**. Run it inside a container, VM, or another isolated environment
when you need isolation. See [Safety](safety.md).
```

## Install and run

```sh
uv tool install wizolt
wizolt --init-config          # write ~/.wizolt/config.toml
# add your provider's url, key, and model to that file
wizolt
```

Full walkthrough: [Getting started](getting-started.md).

## What it does

```{figure} ../snapshots/wizolt2.gif
:alt: wizolt working through a repository task
:width: 600px
:align: center

Working through a repository task in an interactive session.
```

| Area | In short |
|---|---|
| **[Interaction](usage.md)** | Follow-ups, streaming, keys — how you drive the agent. |
| **[Commands](commands.md)** | The `/` command reference: status, models, sessions, MCP. |
| **[Tools](tools.md)** | Read, search, navigate code; edit files; run commands; background jobs; optional provider-side web search. |
| **[Sessions](usage.md#sessions)** | Your work is saved, named, and resumable with `/sessions`, `-c`, or `--resume`. |
| **[MCP](mcp.md)** | Connect external Model Context Protocol servers and use their tools. |
| **[Subagents](agents.md)** | Run parallel agents and switch between their separate conversations. |
| **[Skills](skills.md)** | Load reusable instruction packs on demand, or start one with `/name`. |
| **[Plugins](plugins.md)** | Customize wizolt with Python, or enable the built-in pet. |
| **[Hooks](hooks.md)** | Automatically run your checks, formatters and cleanup commands. |
| **[Appearance](appearance.md)** | Themes, statusbar layouts, dividers and sweep animations. |
| **[Configuration](configuration.md)** | Providers, runtime settings, and data location. |
| **[Compatibility catalog](catalog.md)** | Check provider compatibility and update it. |
| **[Context](context.md)** | Keep long tasks going and understand token usage. |
| **[Safety](safety.md)** | What wizolt can reach, and how to keep that bounded. |
| **[Troubleshooting](troubleshooting.md)** | What a symptom means and what to do about it. |

```{toctree}
:hidden:
:caption: Guide

getting-started
usage
appearance
context
safety
troubleshooting
```

```{toctree}
:hidden:
:caption: Reference

commands
tools
configuration
appearance-reference
catalog
agents
mcp
skills
plugins
hooks
```

```{toctree}
:hidden:
:maxdepth: 1
:titlesonly:

changelog
```
