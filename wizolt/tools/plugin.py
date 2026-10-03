"""The one operation a standalone authoring command cannot perform: live activation."""

import json

from wizolt.base import Json, ToolError
from wizolt.tools.base import Tool


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
