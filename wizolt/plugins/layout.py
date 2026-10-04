"""Host-owned layout policy shared by live sampling, offline trials and management.

Order changes affect presentation only, never callback/lifecycle admission. Each slot updates
its config table; agents keep their own loaded policy until an explicit reload.
Unknown component IDs are retained so disabling a plugin does not forget its placement.
"""

from __future__ import annotations

from dataclasses import replace

from wizolt.plugins.preferences import PluginPreferences
from wizolt.plugins.protocol import MAX_PANEL_ROWS
from wizolt.sdk import Context, Layout, Panel, PluginError, Text, Viewport

INPUT_SLOTS = ("above_divider", "above_input", "below_input")
SLOTS = (*INPUT_SLOTS, "status")


class LayoutBudget:
    """One shared prompt budget, allocated in visual order; status is a scrollable surface."""

    def __init__(self, viewport: Viewport):
        self.viewport = viewport
        self.remaining = self.input_rows(viewport.rows)
        self.occupied: set[str] = set()

    @staticmethod
    def input_rows(rows: int) -> int:
        return min(6, max(0, rows // 4 - 2))

    def context(self, context: Context, slot: str, gap_before: int = 0) -> Context:
        rows = self.remaining if slot in INPUT_SLOTS else MAX_PANEL_ROWS
        # Spacing separates visible components in one region. Reserve at least one content
        # row, and commit the gap only if the callback actually returns visible content.
        gap = min(gap_before, max(0, rows - 1)) if slot in self.occupied else 0
        return replace(context, columns=self.viewport.columns, viewport=self.viewport, layout=Layout(slot, self.viewport.columns, rows - gap, gap))

    def consume(self, layout: Layout, panel: Panel) -> Panel:
        content = panel.rows[: layout.rows]
        if not content:
            return Panel()
        result = Panel((Text(""),) * layout.gap_before + content)
        self.occupied.add(layout.slot)
        if layout.slot in INPUT_SLOTS:
            self.remaining -= len(result.rows)
        return result


class LayoutPreferences:
    """Keep order and spacing orthogonal; publish a preference only after saving succeeds.

    Explicit edits start from saved preferences, not an agent's older presentation snapshot.
    Otherwise moving a component in one agent silently undoes another agent's gap edits.
    """

    def __init__(self, preferences: PluginPreferences | None = None):
        self.preferences = preferences
        self.orders: dict[str, tuple[str, ...]] = {}
        self.gaps: dict[str, dict[str, int]] = {}

    def load(self) -> None:
        if self.preferences is None:
            return
        orders = {}
        gaps = {}
        for slot, data in self.preferences.read("layout").items():
            if slot not in SLOTS:
                raise PluginError(f"Unknown layout slot: {slot}")
            if not isinstance(data, dict):
                raise PluginError(f"Invalid layout preferences for {slot}")
            values = data.get("order", [])
            spacing = data.get("gaps", {})
            if not isinstance(values, list) or any(not isinstance(item, str) or not item.endswith("." + slot) for item in values):
                raise PluginError(f"Invalid component order for {slot}")
            if not isinstance(spacing, dict) or any(not key.endswith("." + slot) or type(value) is not int or value < 0 for key, value in spacing.items()):
                raise PluginError(f"Invalid component gaps for {slot}")
            orders[slot] = tuple(dict.fromkeys(values))
            gaps[slot] = spacing
        self.orders, self.gaps = orders, gaps

    def gap(self, component: str, slot: str, default: int) -> int:
        return self.gaps.get(slot, {}).get(component, default)

    def set_gap(self, component: str, gap_before: int | None, available: dict[str, str]) -> None:
        if component not in available:
            raise PluginError("Unknown active component; list components first")
        if gap_before is not None and (type(gap_before) is not int or gap_before < 0):
            raise PluginError("gap_before must be a nonnegative integer or null to restore the default")
        self.load()
        slot = available[component]
        gaps = dict(self.gaps.get(slot, {}))
        if gap_before is None:
            gaps.pop(component, None)
        else:
            gaps[component] = gap_before
        self._save(slot, self.orders.get(slot, ()), gaps)

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
        self.load()
        identities = set(self.orders.get(slot, ())) | {key for key, value in available.items() if value == slot}
        order = self.ordered(slot, identities)
        order.remove(component)
        order.insert(order.index(anchor) + bool(after), component)
        self._save(slot, tuple(order))

    def reset(self, slot: str = "") -> None:
        if slot and slot not in SLOTS:
            raise PluginError(f"Unknown slot: {slot}")
        self.load()
        for key in (slot,) if slot else SLOTS:
            self._save(key, ())

    def _save(self, slot: str, order: tuple[str, ...], gaps: dict[str, int] | None = None) -> None:
        gaps = self.gaps.get(slot, {}) if gaps is None else gaps
        if self.preferences is not None:
            self.preferences.save("layout", slot, {"order": list(order), "gaps": gaps})
        self.orders[slot] = order
        self.gaps[slot] = gaps
