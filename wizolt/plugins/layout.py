"""Host-owned layout policy shared by live sampling, offline trials and management.

Order changes affect presentation only, never callback/lifecycle admission. Each slot stores an
atomic preference independently; agents keep their own loaded policy until an explicit reload.
Unknown component IDs are retained so disabling a plugin does not forget its placement.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from wizolt.plugins.files import read_regular
from wizolt.plugins.protocol import MAX_PANEL_ROWS
from wizolt.sdk import Context, Layout, Panel, PluginError, Viewport

INPUT_SLOTS = ("above_divider", "above_input", "below_input")
SLOTS = (*INPUT_SLOTS, "status")


class LayoutBudget:
    """One shared prompt budget, allocated in visual order; status is a scrollable surface."""

    def __init__(self, viewport: Viewport):
        self.viewport = viewport
        self.remaining = self.input_rows(viewport.rows)

    @staticmethod
    def input_rows(rows: int) -> int:
        return min(6, max(0, rows // 4 - 2))

    def context(self, context: Context, slot: str) -> Context:
        rows = self.remaining if slot in INPUT_SLOTS else MAX_PANEL_ROWS
        return replace(context, columns=self.viewport.columns, viewport=self.viewport, layout=Layout(slot, self.viewport.columns, rows))

    def consume(self, slot: str, panel: Panel) -> Panel:
        rows = self.remaining if slot in INPUT_SLOTS else MAX_PANEL_ROWS
        result = Panel(panel.rows[:rows])
        if slot in INPUT_SLOTS:
            self.remaining -= len(result.rows)
        return result


class LayoutOrder:
    def __init__(self, directory: Path | None = None):
        self.directory = directory
        self.orders: dict[str, tuple[str, ...]] = {}

    def load(self) -> None:
        if self.directory is None:
            return
        orders = {}
        for slot in SLOTS:
            path = self.directory / (slot + ".json")
            if not path.exists():
                continue
            values = json.loads(read_regular(path, 64 * 1024))
            if not isinstance(values, list) or any(not isinstance(item, str) or not item.endswith("." + slot) for item in values):
                raise PluginError(f"Invalid component order in {path}")
            orders[slot] = tuple(dict.fromkeys(values))
        self.orders = orders

    def ordered(self, slot: str, identities) -> list[str]:
        ranks = {identity: index for index, identity in enumerate(self.orders.get(slot, ()))}
        return sorted(identities, key=lambda identity: (ranks.get(identity, len(ranks)), identity))

    def move(self, component: str, before: str, after: str, available: dict[str, str]) -> None:
        if bool(before) == bool(after):
            raise PluginError("Specify exactly one of before or after")
        anchor = before or after
        if component not in available or anchor not in available:
            raise PluginError("Unknown active component; list components first")
        slot = available[component]
        if slot != available[anchor]:
            raise PluginError("Components can only move within the same slot")
        if component == anchor:
            return
        identities = set(self.orders.get(slot, ())) | {key for key, value in available.items() if value == slot}
        order = self.ordered(slot, identities)
        order.remove(component)
        order.insert(order.index(anchor) + bool(after), component)
        self._save(slot, tuple(order))

    def reset(self, slot: str = "") -> None:
        if slot and slot not in SLOTS:
            raise PluginError(f"Unknown slot: {slot}")
        for key in (slot,) if slot else SLOTS:
            self._save(key, ())

    def _save(self, slot: str, order: tuple[str, ...]) -> None:
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".order-")
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(order, output)
                os.replace(temporary, self.directory / (slot + ".json"))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        self.orders[slot] = order
