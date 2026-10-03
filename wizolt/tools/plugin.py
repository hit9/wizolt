"""Model access to plugins: live activation, and tools that enabled plugins register."""

import json
from typing import ClassVar

from wizolt.base import Json, ToolError
from wizolt.tools.base import Tool


class PluginTool(Tool):
    """Progressive disclosure over plugin tools: one fixed schema, details on request.

    Listing returns names and one-line descriptions; describe returns one tool's parameters;
    call runs it. Enabling plugins never reshapes the tool block, and a plugin with many tools
    costs the prompt nothing until the model asks. Calls execute trusted plugin code with the
    user's permissions, so they confirm like any other mutating tool.
    """

    NAME = "Plugin"
    DESCRIPTION = "Use tools that enabled wizolt plugins provide: list them, describe one's parameters, then call it"
    MUTATES = True
    ACTIONS: ClassVar[tuple[str, ...]] = ("list", "describe", "call")

    @classmethod
    def params_schema(cls) -> Json:
        # fmt: off
        return cls.object_schema({
            "action": {"type": "string", "enum": list(cls.ACTIONS)},
            "plugin": {"type": "string", "description": "Plugin name (describe/call)"},
            "tool": {"type": "string", "description": "Tool name within the plugin (describe/call)"},
            "arguments": {"type": "object", "description": "Arguments matching the described parameters (call)"},
        }, ["action"])
        # fmt: on

    @staticmethod
    def available(session) -> bool:
        """Offered only while an active plugin registers a tool."""
        return session.plugins is not None and any(entry.active.plugin.tools for entry in session.plugins.entries.values())

    def payload(self) -> Json:
        return self.single_dict_arg("Plugin requires named fields")

    def needs_confirmation(self) -> bool:
        return self.payload().get("action") == "call"

    def short_args(self) -> list[str]:
        payload = self.payload()
        target = ".".join(str(payload[key]) for key in ("plugin", "tool") if payload.get(key))
        return [part for part in (str(payload.get("action") or ""), target) if part]

    def operation(self, payload: Json):
        plugin, tool = str(payload.get("plugin") or ""), str(payload.get("tool") or "")
        entry = self.session.plugins.entries.get(plugin) if self.session.plugins is not None else None
        operation = entry.active.plugin.tools.get(tool) if entry is not None else None
        if operation is None:
            raise ToolError(f"Unknown plugin tool {plugin}.{tool}; use action=list")
        return plugin, tool, operation

    async def call(self) -> str:
        payload = self.payload()
        action = payload.get("action")
        runtime = self.session.plugins
        if runtime is None:
            raise ToolError("Plugin runtime is unavailable")
        if action == "list":
            tools = [
                {"plugin": name, "tool": tool, "description": operation.description}
                for name, entry in runtime.entries.items()
                if not entry.active.error
                for tool, operation in entry.active.plugin.tools.items()
            ]
            return json.dumps({"tools": tools}, ensure_ascii=False)
        if action not in self.ACTIONS:
            raise ToolError(f"Unknown action {action!r}; choose {', '.join(self.ACTIONS)}")
        plugin, tool, operation = self.operation(payload)
        if action == "describe":
            return json.dumps({"plugin": plugin, "tool": tool, "description": operation.description, "parameters": operation.parameters}, ensure_ascii=False)
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ToolError("Plugin tool arguments must be an object")
        try:
            return await runtime.invoke(plugin, "tool", tool, arguments)
        except ValueError as error:
            raise ToolError(str(error)) from error


class PluginHotReload(Tool):
    NAME = "PluginHotReload"
    DESCRIPTION = (
        "Reload saved plugin code/settings in this agent. Optional name selects one plugin; otherwise all. Use plugin-workshop and wizolt plugin for authoring."
    )
    MUTATES = True

    @classmethod
    def params_schema(cls) -> Json:
        return cls.object_schema({"name": {"type": "string"}}, [])

    async def call(self) -> str:
        if len(self.args) != 1 or not isinstance(self.args[0], dict):
            raise ToolError("PluginHotReload expects one object")
        name = self.args[0].get("name", "")
        if not isinstance(name, str):
            raise ToolError("Plugin name must be text")
        if self.session.plugins is None:
            raise ToolError("Plugin runtime is unavailable")
        try:
            return json.dumps(await self.session.plugins.hot_reload(name), ensure_ascii=False)
        except ValueError as error:
            raise ToolError(str(error)) from error
