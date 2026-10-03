"""Optional layout manager: ordinary SDK tools, with policy and persistence owned by the host."""

import json
from dataclasses import asdict

SDK_VERSION = 1


def setup(plugin):
    async def listing(ctx, args):
        return json.dumps([asdict(item) for item in await plugin.ui.components.list()])

    async def move(ctx, args):
        items = await plugin.ui.components.move(args["component"], before=args.get("before", ""), after=args.get("after", ""))
        return json.dumps([asdict(item) for item in items])

    async def reset(ctx, args):
        return json.dumps([asdict(item) for item in await plugin.ui.components.reset_order(args.get("slot", ""))])

    plugin.tool(
        "list_components",
        "List components in visual order, with their height allocation and visibility.",
        {"type": "object", "additionalProperties": False},
        listing,
    )
    plugin.tool(
        "move_component",
        "Move a component before or after another in the same slot. Saves the project's layout; specify exactly one anchor.",
        {
            "type": "object",
            "properties": {key: {"type": "string"} for key in ("component", "before", "after")},
            "required": ["component"],
            "additionalProperties": False,
        },
        move,
    )
    plugin.tool(
        "reset_order",
        "Restore default name order for one slot, or all slots when omitted.",
        {
            "type": "object",
            "properties": {"slot": {"type": "string"}},
            "additionalProperties": False,
        },
        reset,
    )
