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


### Session cost in the statusbar

Adds a `cost` field and a statusbar layout that shows it. Prices come from your config.

<!-- figure: plugins-cost -->
```python
SDK_VERSION = 1


def setup(plugin):
    prices = {"input": 3.0, "cached": 0.3, "output": 15.0}  # dollars per million tokens
    plugin.configure({"type": "object", "properties": {name: {"type": "number"} for name in prices}}, defaults=prices)

    def cost(context):
        usage, price = context.usage, plugin.config
        # Input tokens include cache reads, which providers bill at their own, lower rate.
        fresh = usage.input_tokens - usage.cached_tokens
        return (fresh * price["input"] + usage.cached_tokens * price["cached"] + usage.output_tokens * price["output"]) / 1e6

    plugin.field("dollars", cost)
    plugin.preset("statusbar", "cost", "[status.model] {model} [/]{>}[status_context]ctx {context.percent}%[/] [warning]${plugins.cost.dollars:.2f}[/]")
```


Pick **plugins.cost.cost** in `/theme`, or use `{plugins.cost.dollars:.2f}` in your own
[format](APPEARANCE.md). Set your prices:

```toml
[plugins.cost]
input = 3.0    # per million new input tokens
cached = 0.3   # per million tokens read from the provider's cache
output = 15.0
```

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

