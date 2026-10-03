"""Core plugin management and execution gateway; usable even when a plugin fails."""

from __future__ import annotations

import json

from wizolt.base import Json, ToolError
from wizolt.tools.base import Tool


class PluginTool(Tool):
    NAME = "Plugin"
    DESCRIPTION = (
        "Manage trusted local Python plugins or invoke their tools. Load the built-in plugin-workshop skill first when creating plugins. "
        "list/inspect describe actual active versions and available tools; validate executes setup without activation. "
        "enable takes a .py path, reload/disable/rollback take a plugin name. install prepares declared dependencies in a new environment; "
        "it returns an explicit restart command and never modifies the running interpreter. Changes affect this agent; enabling persists for future project agents. "
        "Reload during a turn is pending until it ends. invoke takes plugin, operation and arguments; inspect tools before invoking."
    )
    MUTATES = True

    def data(self) -> Json:
        if len(self.args) != 1 or not isinstance(self.args[0], dict):
            raise ToolError("Plugin expects one object argument")
        return self.args[0]

    @classmethod
    def params_schema(cls) -> Json:
        return cls.object_schema(
            {
                "action": {"type": "string", "enum": ["list", "inspect", "validate", "install", "enable", "reload", "disable", "rollback", "invoke"]},
                "target": {"type": "string", "description": "Plugin name, or Python file path for validate/enable"},
                "operation": {"type": "string"},
                "arguments": {"type": "object"},
            },
            ["action"],
        )

    def needs_confirmation(self) -> bool:
        return self.data().get("action") not in ("list", "inspect")

    async def call(self) -> str:
        runtime = self.session.plugins
        if runtime is None:
            raise ToolError("Plugin runtime is unavailable")
        data = self.data()
        action, target = data.get("action"), data.get("target", "")
        if not isinstance(action, str) or not isinstance(target, str):
            raise ToolError("Plugin action and target must be strings")
        try:
            if action == "invoke":
                await runtime.load()
                operation, arguments = data.get("operation"), data.get("arguments", {})
                if not isinstance(operation, str) or not isinstance(arguments, dict):
                    raise ToolError("invoke requires an operation and object arguments")
                return await runtime.invoke(target, "tool", operation, arguments)
            return json.dumps(await runtime.manage(action, target), ensure_ascii=False)
        except (ValueError, OSError, SyntaxError) as error:
            raise ToolError(str(error)) from error
