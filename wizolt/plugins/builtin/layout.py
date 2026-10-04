"""Optional layout manager: ordinary SDK tools, with policy and persistence owned by the host."""

import json
from dataclasses import asdict

SDK_VERSION = 1


def setup(plugin):
    async def list_shortcuts(ctx, args):
        return json.dumps(await plugin.ui.shortcuts.list())

    async def bind_shortcut(ctx, args):
        return json.dumps(await plugin.ui.shortcuts.bind(args["key"], args["command"], replace=args.get("replace", False)))

    async def unbind_shortcut(ctx, args):
        return json.dumps(await plugin.ui.shortcuts.unbind(args["key"]))

    plugin.tool(
        "list_shortcuts",
        "List user shortcuts and protected keys. Plugin commands are targets; disabled plugins keep dormant bindings.",
        {"type": "object", "additionalProperties": False},
        list_shortcuts,
    )
    plugin.tool(
        "bind_shortcut",
        "Bind a key to plugin.command. Ask before replacing an occupied key; protected input keys cannot be replaced.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}, "command": {"type": "string"}, "replace": {"type": "boolean"}},
            "required": ["key", "command"],
            "additionalProperties": False,
        },
        bind_shortcut,
    )
    plugin.tool(
        "unbind_shortcut",
        "Remove a saved key override, restoring the original host binding.",
        {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"], "additionalProperties": False},
        unbind_shortcut,
    )

    async def listing(ctx, args):
        return json.dumps([asdict(item) for item in await plugin.ui.components.list()])

    async def move(ctx, args):
        items = await plugin.ui.components.move(args["component"], before=args.get("before", ""), after=args.get("after", ""))
        return json.dumps([asdict(item) for item in items])

    async def reset(ctx, args):
        return json.dumps([asdict(item) for item in await plugin.ui.components.reset_order(args.get("slot", ""))])

    async def spacing(ctx, args):
        return json.dumps([asdict(item) for item in await plugin.ui.components.set_gap(args["component"], args["gap_before"])])

    plugin.tool(
        "set_gap",
        "Save empty rows before a component in its slot. Use null to restore its default. First visible components have no leading gap; short terminals shrink gaps.",
        {
            "type": "object",
            "properties": {"component": {"type": "string"}, "gap_before": {"type": ["integer", "null"], "minimum": 0}},
            "required": ["component", "gap_before"],
            "additionalProperties": False,
        },
        spacing,
    )

    plugin.tool(
        "list_components",
        "List components in visual order, with their height allocation and visibility.",
        {"type": "object", "additionalProperties": False},
        listing,
    )
    plugin.tool(
        "move_component",
        "Move a component before or after another in the same slot. Saves the user's layout; specify exactly one anchor.",
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
