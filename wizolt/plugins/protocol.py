"""Private worker wire values, shared by activation and offline trials.

Bound the transport independently of UI clipping: a renderer must never receive an unbounded
plugin response. These are host budgets, not extension points or a security sandbox.
"""

from dataclasses import dataclass
from typing import Any

from wizolt.sdk import Line, Panel, PluginError, Text

MAX_FRAME = 1024 * 1024  # A worker's reply: plugin output, bounded before any renderer sees it.
# A host request carries host data sized by the conversation: a compaction span can approach the
# context window, and JSON escapes each non-ASCII character to six bytes. Bounded, but far larger.
MAX_REQUEST = 64 * 1024 * 1024
MAX_PANEL_ROWS = 12
MAX_ROW_CHARACTERS = 4096
MAX_ROW_SPANS = 256


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
    summarizer: bool = False

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
            value.get("summarizer", False),
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
            panels = {
                slot: Panel(tuple(Line(tuple(Text(**span) for span in row["spans"])) if "spans" in row else Text(**row) for row in panel["rows"]))
                for slot, panel in value["panels"].items()
            }
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
        for row in panel.rows:
            spans = row.spans if isinstance(row, Line) else (row,)
            if len(spans) > MAX_ROW_SPANS or any(
                not isinstance(span, Text) or not isinstance(span.text, str) or not isinstance(span.role, str) for span in spans
            ):
                raise PluginError(f"Panel rows must contain at most {MAX_ROW_SPANS} Text spans")
            if sum(len(span.text) for span in spans) > MAX_ROW_CHARACTERS:
                raise PluginError(f"Panel rows must contain at most {MAX_ROW_CHARACTERS} characters")
