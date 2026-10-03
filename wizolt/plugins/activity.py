"""Agent-local execution facts, independent of whether any plugin currently observes them.

Snapshots cover late activation; events describe transitions. Count bodies actually entered,
not model batches or approval attempts. Only the runner writes these facts, on the agent loop.
"""

import time
from contextvars import ContextVar
from dataclasses import replace

from wizolt.sdk import ToolActivity, ToolCounts, Turn


class TurnActivity:
    def __init__(self):
        self.parent: ContextVar[str] = ContextVar("tool_parent", default="")
        self.sequence = 0
        self.counts = ToolCounts()
        self.active: dict[str, ToolActivity] = {}

    def reset(self) -> None:
        if self.active:
            raise RuntimeError("Cannot start a turn while tools are still running")
        self.counts = ToolCounts()

    def start(self, call_id: str, name: str) -> ToolActivity:
        self.sequence += 1
        item = ToolActivity(str(self.sequence), call_id, name, self.parent.get(), started_at=time.monotonic())
        self.active[item.id] = item
        self.counts = replace(self.counts, started=self.counts.started + 1, running=len(self.active))
        return item

    def finish(self, item: ToolActivity, status: str) -> ToolActivity:
        self.active.pop(item.id)
        self.counts = replace(self.counts, **{status: getattr(self.counts, status) + 1}, running=len(self.active))
        return replace(item, status=status, elapsed=max(0, time.monotonic() - item.started_at))

    def snapshot(self) -> Turn:
        now = time.monotonic()
        return Turn(self.counts, tuple(replace(item, elapsed=max(0, now - item.started_at)) for item in self.active.values()))
