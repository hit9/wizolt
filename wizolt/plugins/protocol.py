"""Private worker wire values, shared by activation and offline trials.

Bound the transport independently of UI clipping: a renderer must never receive an unbounded
plugin response. These are host budgets, not extension points or a security sandbox.
"""

from dataclasses import dataclass, field
from typing import Any

from wizolt.sdk import Line, Panel, PluginError, Text

MAX_FRAME = 1024 * 1024  # A worker's reply: plugin output, bounded before any renderer sees it.
# A host request carries host data sized by the conversation: a compaction span can approach the
# context window, and JSON escapes each non-ASCII character to six bytes. Bounded, but far larger.
MAX_REQUEST = 64 * 1024 * 1024
MAX_PANEL_ROWS = 12
MAX_ROW_CHARACTERS = 4096
MAX_ROW_SPANS = 256
MAX_FIELDS = 64  # The SDK's per-registry limit.


@dataclass(frozen=True)
class Operation:
    description: str
    parameters: dict[str, Any]
    during_turn: bool = False


@dataclass(frozen=True)
class Capabilities:
    """Host-side descriptions contain no executable callbacks or imported plugin objects."""

    name: str
    fields: tuple[str, ...]
    components: dict[str, int]  # slot -> declared gap; callbacks never cross the process boundary.
    observers: tuple[str, ...]
    commands: dict[str, Operation]
    tools: dict[str, Operation]
    themes: dict[str, dict[str, Any]]
    presets: dict[str, dict[str, str]]
    intercepts: dict[str, "InterceptSpec"] = field(default_factory=dict)

    @classmethod
    def decode(cls, name: str, value: dict) -> "Capabilities":
        return cls(
            name,
            tuple(value["fields"]),
            dict(value["slots"]),
            tuple(value["events"]),
            {key: Operation(**item) for key, item in value["commands"].items()},
            {key: Operation(**item) for key, item in value["tools"].items()},
            value["themes"],
            value["presets"],
            {key: InterceptSpec.decode(key, item) for key, item in value.get("intercepts", {}).items()},
        )


@dataclass(frozen=True)
class InterceptSpec:
    """A registration's host-side prefilter and response mode; never a plugin predicate."""

    match: dict[str, frozenset[str]]
    response: str = ""

    @classmethod
    def decode(cls, operation: str, value: dict) -> "InterceptSpec":
        from wizolt.sdk.operations import OPERATIONS

        spec = OPERATIONS.get(operation)
        match = value.get("match", {})
        if spec is None or not isinstance(match, dict) or not set(match) <= spec.match:
            raise PluginError(f"Invalid interceptor registration for {operation}")
        if any(not isinstance(items, list) or not items or any(not isinstance(item, str) for item in items) for items in match.values()):
            raise PluginError(f"Invalid {operation} matcher")
        response = value.get("response", "")
        if response not in (("preserve", "replace") if spec.response_modes else ("",)):
            raise PluginError(f"Invalid {operation} response mode")
        return cls({key: frozenset(items) for key, items in match.items()}, response)

    def matches(self, value: object) -> bool:
        return all(getattr(value, key, None) in allowed for key, allowed in self.match.items())


@dataclass(frozen=True)
class Snapshot:
    fields: dict[str, Any]
    panels: dict[str, Panel]

    @classmethod
    def decode(cls, value: dict) -> "Snapshot":
        """Decode only data. The parent never imports classes supplied by plugin Python."""
        try:
            fields = value["fields"]
            panels = {
                slot: Panel(tuple(Line(tuple(Text(**span) for span in row["spans"])) if "spans" in row else Text(**row) for row in panel["rows"]))
                for slot, panel in value["panels"].items()
            }
            # Bars sanitize field text on every paint: bound it here like a panel row, so a
            # long string cannot make each frame of the host's UI slow.
            if not isinstance(fields, dict) or len(fields) > MAX_FIELDS:
                raise TypeError(f"fields must be an object with at most {MAX_FIELDS} entries")
            if any(type(value) not in (str, int, float, bool) or (isinstance(value, str) and len(value) > MAX_ROW_CHARACTERS) for value in fields.values()):
                raise TypeError(f"fields must be scalars; text at most {MAX_ROW_CHARACTERS} characters")
            for panel in panels.values():
                cls.check_panel(panel)
            return cls(fields, panels)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise PluginError(f"Invalid plugin snapshot: {error}") from error

    @staticmethod
    def check_panel(panel: Panel) -> None:
        if not isinstance(panel, Panel) or len(panel.rows) > MAX_PANEL_ROWS:
            raise PluginError(f"Component must return Panel with at most {MAX_PANEL_ROWS} rows")
        for row in panel.rows:
            spans = row.spans if isinstance(row, Line) else (row,)
            if len(spans) > MAX_ROW_SPANS or any(
                not isinstance(span, Text) or not isinstance(span.text, str) or not isinstance(span.role, str) for span in spans
            ):
                raise PluginError(f"Panel rows must contain at most {MAX_ROW_SPANS} Text spans")
            if sum(len(span.text) for span in spans) > MAX_ROW_CHARACTERS:
                raise PluginError(f"Panel rows must contain at most {MAX_ROW_CHARACTERS} characters")
