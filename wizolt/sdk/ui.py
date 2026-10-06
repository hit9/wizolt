"""Host-owned component layout, controllable from explicit plugin commands and tools.

The service stores preferences independently of the managing plugin. Observers and renderers
cannot change layout; disabling a manager never discards the user's chosen order.
"""

from __future__ import annotations

from dataclasses import dataclass

from wizolt.sdk import PluginError
from wizolt.sdk.models import HostCall
from wizolt.sdk.views import Choice, Field, Form, OpenView, Selection, View, ViewResult


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


class Shortcuts:
    """User preferences for global keys targeting plugin.command, managed by explicit actions."""

    def __init__(self):
        self.call: HostCall | None = None

    async def _request(self, action: str, **arguments) -> dict:
        if self.call is None:
            raise PluginError("Shortcut management requires a live TUI")
        return await self.call("ui.shortcuts." + action, arguments)

    async def list(self) -> dict:
        """Return bindings (key, command, active), protected keys and any preference error."""
        return await self._request("list")

    async def bind(self, key: str, command: str, *, replace: bool = False) -> dict:
        """Save a key for plugin.command. Explicit replacement never overrides protected keys."""
        return await self._request("bind", key=key, command=command, replace=replace)

    async def unbind(self, key: str) -> dict:
        """Remove a saved override and restore the host's original binding; idempotent."""
        return await self._request("unbind", key=key)


class UI:
    def __init__(self):
        self.components = Components()
        self.shortcuts = Shortcuts()
        self.call: HostCall | None = None

    def open(self, view: View) -> OpenView:
        """Open a live action-owned view with ``async with``; never from an observer."""
        if self.call is None:
            raise PluginError("Interactive UI is unavailable")
        return OpenView(self.call, view)

    async def show(self, view: View) -> ViewResult | None:
        """Await one action, or None on cancellation. Esc always belongs to the host."""
        async with self.open(view) as opened:
            return await opened.result()

    async def input(self, title: str, *, default: str = "", multiline: bool = False, required: bool = False) -> str | None:
        result = await self.show(View(title, Form((Field("value", title, default, required, multiline),))))
        return result.values["value"] if result else None

    async def edit(self, text: str) -> str | None:
        """Hand the terminal to the user's editor (``$VISUAL``, ``$EDITOR``) on ``text``; the saved
        text, or None when the editor could not start or exited without success. Writes nothing
        anywhere: what to do with the result is yours."""
        if self.call is None:
            raise PluginError("Interactive UI is unavailable")
        if not isinstance(text, str):
            raise PluginError("The editor takes text")
        return (await self.call("ui.edit", {"text": text}))["text"]

    async def confirm(self, title: str) -> bool:
        """Default to cancellation. This business choice never grants tool approval."""
        return await self.select(title, items=(Choice("no", "Cancel"), Choice("yes", "Confirm"))) == "yes"

    async def select(self, title: str, *, items: tuple[Choice, ...], default: str = "") -> str | None:
        result = await self.show(View(title, Selection(items, (default,) if default else ())))
        return result.selected[0] if result and result.selected else None

    async def select_many(self, title: str, *, items: tuple[Choice, ...], defaults: tuple[str, ...] = ()) -> tuple[str, ...] | None:
        result = await self.show(View(title, Selection(items, defaults, multiple=True)))
        return result.selected if result else None

    async def notify(self, message: str, *, level: str = "info") -> None:
        """Emit a themed session-local notice during an explicit action."""
        if self.call is None:
            raise PluginError("Interactive UI is unavailable")
        await self.call("ui.notify", {"message": message, "level": level})
