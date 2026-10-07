"""Choose which tools the model is offered, with /tools.

Hide built-in tools you never want the model to reach for, and offer a plugin's tool to the model
directly instead of only through the `Plugin` gateway. Your choice is saved and applies from the
next turn. Changing it costs the provider cache once; after that every request offers the same
tools again. It makes no model calls.

## Use

- Enable **tool_visibility** in `/plugins`.
- Type `/tools`: checked built-in tools are offered, unchecked ones are hidden; a checked plugin
  tool is offered directly. **Enter** saves.
- The choice lives in `[plugins.tool_visibility]` as `hidden` and `resident` lists.
"""

# Built only on the public SDK: `tools.offer` applies the saved choice once per turn, and the
# command edits it with a multi-select over `plugin.agent.tools()` and `plugin.settings`. The
# handler decides from the saved choice alone, never from the conversation, so an unchanged choice
# keeps the request's tools byte-identical turn after turn.

from collections.abc import Mapping
from dataclasses import dataclass

from wizolt.sdk import Context, Plugin, PluginError
from wizolt.sdk.agent import ToolInfo
from wizolt.sdk.operations import ToolOffer
from wizolt.sdk.views import Choice

SDK_VERSION = 1


@dataclass
class Choices:
    """The saved choice: built-in tools to hide, plugin tools to offer directly."""

    hidden: tuple[str, ...]
    resident: tuple[str, ...]


LABEL_LIMIT = 300  # A view's text limit: some tools' descriptions run longer on their first line.


def _label(tool: ToolInfo) -> str:
    head = tool.name if tool.kind == "builtin" else f"{tool.name} (plugin, offered directly)"
    label = f"{head} · {tool.description}" if tool.description else head
    return label if len(label) <= LABEL_LIMIT else label[: LABEL_LIMIT - 1] + "…"


def setup(plugin: Plugin) -> None:
    """Default enablement belongs to the installation catalog, never to plugin source code."""
    names = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    plugin.configure(
        {"type": "object", "properties": {"hidden": names, "resident": names}, "additionalProperties": False},
        defaults={"hidden": [], "resident": []},
    )
    choices = Choices(tuple(plugin.config["hidden"]), tuple(plugin.config["resident"]))

    async def offer(context: Context, tools: ToolOffer, next):
        # A resident tool whose plugin is not running this turn is simply not offered.
        resident = (name for name in choices.resident if name in tools.available)
        return await next(tools.without(*choices.hidden).adding(*resident))

    async def pick(context: Context, arguments: Mapping[str, object]) -> str:
        known = await plugin.agent.tools()
        builtin = tuple(tool.name for tool in known if tool.kind == "builtin")
        available = tuple(tool.name for tool in known if tool.kind == "plugin")
        items = tuple(Choice(tool.name, _label(tool)) for tool in known)
        offered = (*(name for name in builtin if name not in choices.hidden), *(name for name in choices.resident if name in available))
        picked = await plugin.ui.select_many("Tools offered to the model", items=items, defaults=offered)
        if picked is None:
            return "Tools unchanged"
        hidden = tuple(name for name in builtin if name not in picked)
        # Names not listed now (a stopped plugin, a disconnected family) keep their choice.
        hidden += tuple(name for name in choices.hidden if name not in builtin)
        resident = tuple(name for name in available if name in picked)
        resident += tuple(name for name in choices.resident if name not in available)
        if (hidden, resident) == (choices.hidden, choices.resident):
            return "Tools unchanged"
        choices.hidden, choices.resident = hidden, resident
        try:
            await plugin.settings.update({"hidden": list(hidden), "resident": list(resident)})
        except PluginError as error:
            return f"Tools changed for this session; saving failed ({error})"
        return f"Tools saved under [plugins.tool_visibility]; {len(hidden)} hidden, {len(resident)} offered directly, from the next turn"

    plugin.intercept("tools.offer", offer)
    plugin.command("tools", "Choose which tools the model is offered", pick)
