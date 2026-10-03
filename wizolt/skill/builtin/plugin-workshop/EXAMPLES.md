# Examples

Complete examples for plugin authors. Save each as a separate plugin, validate it, then reload.
See [SDK.md](SDK.md) for contracts and [SKILL.md](SKILL.md) for the workflow.

### Context meter

Shows what fills the context window, above the divider.

<!-- figure: plugins-meter -->
```python
from wizolt.sdk import Line, Panel, Text

SDK_VERSION = 1
COLORS = ("accent", "success", "warning", "info")


def draw(context):
    window = context.window
    width = context.columns - 2
    spans, start = [], 0
    for index, (_, tokens) in enumerate(window.parts):
        end = start + round(tokens * width / max(1, window.limit))
        spans.append(Text("█" * (end - start), COLORS[index % len(COLORS)]))
        start = end
    spans.append(Text("░" * max(0, width - start), "muted"))
    names = " · ".join(f"{name} {tokens // 1000}k" for name, tokens in window.parts)
    return Panel((Line(tuple(spans)), Text(names, "muted")))


def setup(plugin):
    plugin.component("above_divider", draw)
```


### Session tokens in the statusbar

Uses the current agent's cumulative usage; no prices or external services are needed.

<!-- figure: plugins-tokens -->
```python
SDK_VERSION = 1


def setup(plugin):
    plugin.field("input_k", lambda context: context.usage.input_tokens / 1000)
    plugin.field("output_k", lambda context: context.usage.output_tokens / 1000)
    plugin.preset(
        "statusbar", "tokens",
        "[status.model] {model} [/]{>}"
        "[info]in {plugins.tokens.input_k:.1f}k[/] "
        "[success]out {plugins.tokens.output_k:.1f}k[/]",
    )
```

Pick **plugins.tokens.tokens** in `/theme`. Counts depend on the provider's usage reports.

### A command for you

Adds `/note TEXT` to keep project notes in `.wizolt/notes.txt`.

```python
from pathlib import Path

SDK_VERSION = 1


def setup(plugin):
    async def note(context, arguments):
        path = Path(context.cwd) / ".wizolt" / "notes.txt"
        path.parent.mkdir(exist_ok=True)
        with path.open("a") as notes:
            notes.write(arguments["input"] + "\n")
        return "Noted."

    plugin.command("note", "Save a project note", note)
```

### An ability for the agent

Lets the agent read those notes when it decides they help. It asks you before each use.

```python
from pathlib import Path

SDK_VERSION = 1


def setup(plugin):
    async def recall(context, arguments):
        path = Path(context.cwd) / ".wizolt" / "notes.txt"
        return path.read_text() if path.exists() else "No notes yet."

    plugin.tool("recall", "Read the user's project notes", {"type": "object", "properties": {}}, recall)
```


The agent discovers and calls `recall` through **Plugin**, not as a standalone model tool or
a bare ToolScript tool name. Details are loaded only when needed. Offline trials can test this
file-reading example; plugins using live model or layout services need a reload and an in-session test.

