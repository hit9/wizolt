"""The host services a plugin calls outside the UI: model requests, and read-only agent facts.

Facts are the inputs a plugin's own policy decides from, available to a command before any turn
has run: which tools this agent can offer (with what each does), and the system text a
`context.compose` handler would receive. Reading them changes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wizolt.agent.plugin_models import PluginModels
from wizolt.sdk import PluginError

if TYPE_CHECKING:
    from wizolt.session import Session


class PluginServices:
    def __init__(self, session: Session):
        self.session = session
        self.models = PluginModels(session)

    async def call(self, operation: str, arguments: dict) -> dict:
        if operation == "model.complete":
            return await self.models.call(operation, arguments)
        if operation in ("agent.tools", "agent.system_prompt") and arguments:
            raise PluginError(f"{operation} takes no arguments")
        if operation == "agent.tools":
            return {"tools": self.tools()}
        if operation == "agent.system_prompt":
            return {"text": self.session.system_prompt.strip()}  # The `system` block, as composed.
        raise PluginError(f"Unknown host service: {operation}")

    def tools(self) -> list[dict]:
        """Every tool the next turn's `tools.offer` would start from, and every plugin tool it may
        add, with the first line of what each does and whether the last turn offered it."""
        from wizolt.tools import Tool
        from wizolt.tools.plugin import PluginTool

        offered = self.session.offered_tools
        rows = [
            {
                "name": schema["function"]["name"],
                "description": schema["function"]["description"].split("\n", 1)[0],
                "kind": "builtin",
                "offered": offered is None or schema["function"]["name"] in offered,
            }
            for schema in Tool.builtin_schemas(self.session)
        ]
        plugins = self.session.plugins
        if plugins is not None:
            available = set(PluginTool.offerable(plugins))
            rows.extend(
                {
                    "name": f"{plugin}.{tool}",
                    "description": operation.description.split("\n", 1)[0],
                    "kind": "plugin",
                    "offered": offered is not None and f"{plugin}.{tool}" in offered,
                }
                for plugin, tool, operation in PluginTool.usable(plugins)
                if f"{plugin}.{tool}" in available
            )
        return rows
