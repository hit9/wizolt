"""The model's one plugin gateway: apply saved choices, and use tools that plugins register."""

import json
from typing import ClassVar

from wizolt.base import ApprovalView, Json, ToolError
from wizolt.tools.base import Tool


class PluginTool(Tool):
    """One fixed schema, always present, with plugin tools disclosed progressively.

    `reload` reconciles saved plugin choices into this agent. `list` returns plugin tool names and
    one-line descriptions, `describe` one tool's parameters, and `call` runs it. The schema never
    changes as plugins come and go, so enabling one cannot invalidate the provider's prompt cache,
    and a plugin with many tools costs nothing until asked for. Reloads and calls execute trusted
    plugin code with the user's permissions, so they confirm like any other mutating tool.
    """

    NAME = "Plugin"
    DESCRIPTION = "Plugins: reload applies saved plugin choices (name: one plugin); list/describe/call use plugin tools"
    MUTATES = True
    ACTIONS: ClassVar[tuple[str, ...]] = ("reload", "list", "describe", "call")

    @classmethod
    def params_schema(cls) -> Json:
        # fmt: off
        return cls.object_schema({
            "action": {"type": "string", "enum": list(cls.ACTIONS)},
            "name": {"type": "string", "description": "Plugin"},
            "tool": {"type": "string"},
            "arguments": {"type": "object"},
        }, ["action"])
        # fmt: on

    def payload(self) -> Json:
        return self.single_dict_arg("Plugin requires named fields")

    def needs_confirmation(self) -> bool:
        return self.payload().get("action") in ("reload", "call")

    def approval_view(self) -> ApprovalView | None:
        """For a reload, what each plugin would do, so approving it is an informed choice."""
        payload = self.payload()
        if payload.get("action") != "reload" or self.session.plugins is None:
            return None
        name = payload.get("name", "")
        runtime = self.session.plugins
        plan = runtime.reload_plan(name if isinstance(name, str) else "")
        changed = [f"- {plugin}: {change}" for plugin, change in plan if change != runtime.UNCHANGED]
        restarted = [plugin for plugin, change in plan if change == runtime.UNCHANGED]
        lines = [*changed] or ["No changes."] if plan else ["Nothing to reload: no saved plugin choices match."]
        if restarted:
            lines.append(f"Restart without changes, resetting what they keep in memory: {', '.join(restarted)}")
        text = "Run saved plugin code in this agent, with your permissions.\n\n" + "\n".join(lines)
        return ApprovalView("plugin reload", text, rows=[("scope", name or "all saved plugins"), ("changes", f"{len(changed)} of {len(plan)}")])

    def short_args(self) -> list[str]:
        payload = self.payload()
        target = ".".join(str(payload[key]) for key in ("name", "tool") if payload.get(key))
        return [part for part in (str(payload.get("action") or ""), target) if part]

    def operation(self, payload: Json):
        plugin, tool = str(payload.get("name") or ""), str(payload.get("tool") or "")
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
        if action not in self.ACTIONS:
            raise ToolError(f"Unknown action {action!r}; choose {', '.join(self.ACTIONS)}")
        name = payload.get("name", "")
        if not isinstance(name, str):
            raise ToolError("Plugin name must be text")
        try:
            if action == "reload":
                return json.dumps(await runtime.hot_reload(name), ensure_ascii=False)
            if action == "list":
                tools = [
                    {"name": plugin, "tool": tool, "description": operation.description}
                    for plugin, entry in runtime.entries.items()
                    if not entry.active.error
                    for tool, operation in entry.active.plugin.tools.items()
                ]
                return json.dumps({"tools": tools}, ensure_ascii=False)
            plugin, tool, operation = self.operation(payload)
            if action == "describe":
                return json.dumps({"name": plugin, "tool": tool, "description": operation.description, "parameters": operation.parameters}, ensure_ascii=False)
            arguments = payload.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ToolError("Plugin tool arguments must be an object")
            return await runtime.invoke(plugin, "tool", tool, arguments)
        except ValueError as error:
            raise ToolError(str(error)) from error
