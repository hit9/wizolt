"""Private worker wire values, shared by activation and offline trials.

Bound the transport independently of UI clipping: a renderer must never receive an unbounded
plugin response. These are host budgets, not extension points or a security sandbox.
"""

from dataclasses import dataclass
from typing import Any

from wizolt.sdk import Panel, PluginError, Text

MAX_FRAME = 1024 * 1024
MAX_PANEL_ROWS = 12
MAX_ROW_CHARACTERS = 4096


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
    components: tuple[str, ...]
    observers: tuple[str, ...]
    commands: dict[str, Operation]
    tools: dict[str, Operation]
    themes: dict[str, dict[str, Any]]
    presets: dict[str, dict[str, str]]

    @classmethod
    def decode(cls, name: str, value: dict) -> "Capabilities":
        return cls(
            name,
            tuple(value["fields"]),
            tuple(value["slots"]),
            tuple(value["events"]),
            {key: Operation(**item) for key, item in value["commands"].items()},
            {key: Operation(**item) for key, item in value["tools"].items()},
            value["themes"],
            value["presets"],
        )


@dataclass(frozen=True)
class Snapshot:
    fields: dict[str, Any]
    panels: dict[str, Panel]

    @classmethod
    def decode(cls, value: dict) -> "Snapshot":
        """Decode only data. The parent never imports classes supplied by plugin Python."""
        try:
            fields = value["fields"]
            panels = {slot: Panel(tuple(Text(**row) for row in panel["rows"])) for slot, panel in value["panels"].items()}
            if not isinstance(fields, dict):
                raise TypeError("fields must be an object")
            for panel in panels.values():
                cls.check_panel(panel)
            return cls(fields, panels)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise PluginError(f"Invalid plugin snapshot: {error}") from error

    @staticmethod
    def check_panel(panel: Panel) -> None:
        if not isinstance(panel, Panel) or len(panel.rows) > MAX_PANEL_ROWS:
            raise PluginError(f"Component must return Panel with at most {MAX_PANEL_ROWS} rows")
        if any(
            not isinstance(row, Text) or not isinstance(row.text, str) or not isinstance(row.role, str) or len(row.text) > MAX_ROW_CHARACTERS
            for row in panel.rows
        ):
            raise PluginError(f"Panel rows must be Text with at most {MAX_ROW_CHARACTERS} characters")
