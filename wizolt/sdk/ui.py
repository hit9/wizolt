"""Host-owned component layout, controllable from explicit plugin commands and tools.

The service stores preferences independently of the managing plugin. Observers and renderers
cannot change layout; disabling a manager never discards the user's chosen order.
"""

from __future__ import annotations

from dataclasses import dataclass

from wizolt.sdk import PluginError
from wizolt.sdk.models import HostCall


@dataclass(frozen=True)
class Component:
    id: str
    plugin: str
    slot: str
    rows: int
    available_rows: int
    visibility: str


class Components:
    def __init__(self):
        self.call: HostCall | None = None

    async def _request(self, action: str, **arguments) -> tuple[Component, ...]:
        if self.call is None:
            raise PluginError("Host layout service is unavailable")
        result = await self.call("ui.components." + action, arguments)
        return tuple(Component(**item) for item in result["components"])

    async def list(self) -> tuple[Component, ...]:
        """List current-agent components in visual order, including budget-hidden ones."""
        return await self._request("list")

    async def move(self, component: str, *, before: str = "", after: str = "") -> tuple[Component, ...]:
        """Move within one slot, specifying exactly one anchor. Save for this project."""
        return await self._request("move", component=component, before=before, after=after)

    async def reset_order(self, slot: str = "") -> tuple[Component, ...]:
        """Restore name order for one slot, or all slots when omitted."""
        return await self._request("reset_order", slot=slot)


class UI:
    def __init__(self):
        self.components = Components()
