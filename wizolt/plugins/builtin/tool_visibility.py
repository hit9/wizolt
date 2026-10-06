"""Choose which tools the model is offered, with /tools.

Hide built-in tools you never want the model to reach for, and offer a plugin's tool to the model
directly instead of only through the `Plugin` gateway. Your choice is saved and applies from the
next turn. Changing it costs the provider cache once; after that every request offers the same
tools again. It makes no model calls.

## Use

- Enable **tool_visibility** in `/plugins`.
- Send a message, then type `/tools`: checked built-in tools are offered, unchecked ones are
  hidden; a checked plugin tool is offered directly. **Enter** saves.
- The choice lives in `[plugins.tool_visibility]` as `hidden` and `resident` lists.
"""

# Built only on the public SDK: `tools.offer` applies the saved choice once per turn, and the
# command edits it with a multi-select and `plugin.settings`. The handler decides from the saved
# choice alone, never from the conversation, so an unchanged choice keeps the request's tools
# byte-identical turn after turn.

from collections.abc import Mapping
from dataclasses import dataclass, field

from wizolt.sdk import Context, Plugin, PluginError
from wizolt.sdk.operations import ToolOffer
from wizolt.sdk.views import Choice

SDK_VERSION = 1


@dataclass
class Choices:
    """The saved choice, and the names the last turn's offer held (the list `/tools` shows)."""

    hidden: tuple[str, ...]
    resident: tuple[str, ...]
    seen: ToolOffer | None = field(default=None)


def setup(plugin: Plugin) -> None:
    """Default enablement belongs to the installation catalog, never to plugin source code."""
    names = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    plugin.configure(
        {"type": "object", "properties": {"hidden": names, "resident": names}, "additionalProperties": False},
        defaults={"hidden": [], "resident": []},
    )
    choices = Choices(tuple(plugin.config["hidden"]), tuple(plugin.config["resident"]))

    async def offer(context: Context, tools: ToolOffer, next):
        choices.seen = tools
        # A resident tool whose plugin is not running this turn is simply not offered.
        resident = (name for name in choices.resident if name in tools.available)
        return await next(tools.without(*choices.hidden).adding(*resident))

    async def pick(context: Context, arguments: Mapping[str, object]) -> str:
        seen = choices.seen
        if seen is None:
            return "Send a message first: the tool list arrives with the first turn."
        items = (
            *(Choice(name, name) for name in seen.tools),
            *(Choice(name, f"{name} (plugin tool, offered directly)") for name in seen.available),
        )
        offered = (*(name for name in seen.tools if name not in choices.hidden), *(name for name in choices.resident if name in seen.available))
        picked = await plugin.ui.select_many("Tools offered to the model", items=items, defaults=offered)
        if picked is None:
            return "Tools unchanged"
        hidden = tuple(name for name in seen.tools if name not in picked)
        # Names this turn did not list (a stopped plugin, a disconnected family) keep their choice.
        hidden += tuple(name for name in choices.hidden if name not in seen.tools)
        resident = tuple(name for name in seen.available if name in picked)
        resident += tuple(name for name in choices.resident if name not in seen.available)
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
