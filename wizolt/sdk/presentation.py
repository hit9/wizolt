"""Public view models for the named presentation sites.

A presenter receives one immutable view model and returns a Panel -- the same declaration
components use, never ANSI or widgets. Sites name host-owned seams: a panel replaces only
that site's rows. Approvals, stored-result citations, tags and queue facts stay host-owned.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from wizolt.sdk import PluginError, ToolActivity, ToolCounts
from wizolt.sdk.operations import thaw

MAX_TEXT = 512 * 1024  # Matches operation values; the worker frame bound still applies.

# site -> its read-only match fields. ``activity`` matches whenever it is registered.
SITES: dict[str, frozenset[str]] = {
    "tool.call": frozenset({"tool"}),
    "tool.result": frozenset({"tool"}),
    "activity": frozenset(),
}


def _text(value: object, name: str, limit: int = 300) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise PluginError(f"{name} must be text of at most {limit} characters")
    return value


def _arguments(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PluginError("arguments must be an object")
    return value


def _elapsed(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise PluginError("elapsed must be a nonnegative number")
    return float(value)


@dataclass(frozen=True)
class ToolCard:
    """The ``tool.call`` site: how one settled invocation is identified on screen.

    The running card above a live preview and every approval display stay host-owned.
    ``status`` is ``ok``, ``failed`` or ``refused``; a refused call never ran.
    """

    id: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    status: str = "ok"

    def check(self) -> None:
        _text(self.id, "id")
        _text(self.tool, "tool")
        _arguments(self.arguments)
        if self.status not in ("ok", "failed", "refused"):
            raise PluginError("Unknown tool card status")


@dataclass(frozen=True)
class ToolSummary:
    """The ``tool.result`` site: one settled call and its model-visible output.

    ``output`` is the retained text the model received. ``key`` names the stored result
    (``tr.N``); the citation itself stays host-owned, so a panel cannot forge or drop it.
    """

    id: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    status: str = "ok"
    output: str = ""
    elapsed: float | None = None
    key: str = ""

    def check(self) -> None:
        _text(self.id, "id")
        _text(self.tool, "tool")
        _arguments(self.arguments)
        if self.status not in ("ok", "failed", "refused"):
            raise PluginError("Unknown tool summary status")
        _text(self.output, "output", MAX_TEXT)
        _text(self.key, "key")
        _elapsed(self.elapsed)


@dataclass(frozen=True)
class ActivityStatus:
    """The ``activity`` site: what the agent is doing right now.

    ``status`` is the live status word, ``stream_kind``/``stream_text`` the current model
    stream (empty between requests), and ``active_tools``/``counts`` this turn's executions.
    """

    status: str = ""
    elapsed: float = 0.0
    stream_kind: str = ""
    stream_text: str = ""
    active_tools: tuple[ToolActivity, ...] = ()
    counts: ToolCounts = field(default_factory=ToolCounts)

    def check(self) -> None:
        _text(self.status, "status")
        _text(self.stream_kind, "stream_kind")
        _text(self.stream_text, "stream_text", MAX_TEXT)
        if not isinstance(self.active_tools, tuple) or len(self.active_tools) > 1000:
            raise PluginError("active_tools must be a list of at most 1000 tools")
        for item in self.active_tools:
            if not isinstance(item, ToolActivity):
                raise PluginError("active_tools must contain tool activity facts")
        if not isinstance(self.counts, ToolCounts):
            raise PluginError("counts must be tool counts")
        if _elapsed(self.elapsed) is None:
            raise PluginError("elapsed must be a nonnegative number")


VIEWS: dict[str, type] = {"tool.call": ToolCard, "tool.result": ToolSummary, "activity": ActivityStatus}


def encode(view: ToolCard | ToolSummary | ActivityStatus) -> dict:
    """Checked JSON data for the worker boundary."""
    view.check()
    # thaw, not asdict: frozen mappings (arguments) are not copyable or JSON-serializable.
    return thaw(view)


def decode(site: str, data: object) -> object:
    """Reconstitute the site's view model from worker-boundary JSON."""
    cls = VIEWS.get(site)
    try:
        if cls is None or not isinstance(data, dict):
            raise TypeError(f"expected a {site} view")
        values = {key: value for key, value in data.items() if key not in ("active_tools", "counts")}
        if cls is ActivityStatus:
            return cls(
                **values,
                active_tools=tuple(ToolActivity(**item) for item in data.get("active_tools", ())),
                counts=ToolCounts(**data.get("counts", {})),
            )
        return cls(**values)
    except (TypeError, ValueError) as error:
        raise PluginError(f"Invalid {site} view: {error}") from error
