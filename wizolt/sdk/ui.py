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
    """Last allocation: rows exclude spacing; gap_before is requested, rendered_gap is used."""

    id: str
    plugin: str
    slot: str
    rows: int
    available_rows: int
    visibility: str
    gap_before: int = 0
    rendered_gap: int = 0


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
        """Move within one slot, specifying exactly one anchor. Save for your user."""
        return await self._request("move", component=component, before=before, after=after)

    async def reset_order(self, slot: str = "") -> tuple[Component, ...]:
        """Restore name order for one slot, or all slots when omitted."""
        return await self._request("reset_order", slot=slot)

    async def set_gap(self, component: str, gap_before: int | None) -> tuple[Component, ...]:
        """Save empty rows before a component; None restores its declared default.

        Gaps separate visible components in the same slot. The first has no leading gap;
        limited height can shrink a gap to leave room for content.
        """
        return await self._request("set_gap", component=component, gap_before=gap_before)


class UI:
    def __init__(self):
        self.components = Components()
