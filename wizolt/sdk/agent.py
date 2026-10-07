"""Read-only facts about the agent a plugin runs in, for policies decided outside a turn."""

from __future__ import annotations

from dataclasses import dataclass

from wizolt.sdk import PluginError
from wizolt.sdk.models import HostCall


@dataclass(frozen=True)
class ToolInfo:
    """One tool a turn can offer: ``kind`` is ``builtin`` or ``plugin`` (named ``plugin.tool``, as
    `tools.offer` adds it); ``offered`` says whether the last turn offered it."""

    name: str
    description: str
    kind: str
    offered: bool


@dataclass(frozen=True)
class Directive:
    """One block a setting appends to the system text: ``setting`` names it (``runtime.reactions``)."""

    setting: str
    text: str


class AgentFacts:
    """The worker binds transport; nothing here changes the agent."""

    def __init__(self):
        self.call: HostCall | None = None

    async def tools(self) -> tuple[ToolInfo, ...]:
        """The built-in tools a turn starts from and the plugin tools `tools.offer` may add, each
        with the first line of its description. Works before the first turn."""
        result = await self._request("agent.tools")
        return tuple(ToolInfo(**item) for item in result["tools"])

    async def system_prompt(self) -> str:
        """The system text a `context.compose` handler receives as the ``system`` block, before
        any plugin changes it."""
        return (await self._request("agent.system_prompt"))["text"]

    async def system_directives(self) -> tuple[Directive, ...]:
        """The fixed blocks wizolt's settings append after the ``system`` block in every request,
        in sent order, whatever text replaced it. Turned on and off by their settings only."""
        return tuple(Directive(**item) for item in (await self._request("agent.system_prompt"))["directives"])

    async def _request(self, operation: str) -> dict:
        if self.call is None:
            raise PluginError("Agent facts are unavailable")
        return await self.call(operation, {})
