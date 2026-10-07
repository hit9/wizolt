"""Agent-local ownership of plugin interactions; no terminal dependency.

Explicit retirement dismisses human interaction without cancelling unrelated plugin actions.
Ordinary request cancellation still propagates. The frontend owns the actual modal cleanup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from wizolt.sdk import PluginError

InteractionHandler = Callable[[str, str, dict], Awaitable[dict]]


@dataclass
class Interaction:
    owner: str
    task: asyncio.Future
    dismissed: bool = False


class Interactions:
    def __init__(self):
        self.handler: InteractionHandler | None = None
        self.pending: list[Interaction] = []

    async def call(self, owner: str, service: str, arguments: dict) -> dict:
        if self.handler is None:
            raise PluginError("Interactive UI requires a running TUI session")
        task = asyncio.ensure_future(self.handler(owner, service, arguments))
        entry = Interaction(owner, task)
        self.pending.append(entry)
        try:
            return await task
        except asyncio.CancelledError:
            if not entry.dismissed:
                raise
            return {"text": None} if service == "ui.edit" else {"result": None}
        finally:
            self.pending.remove(entry)

    def dismiss(self, owner: str) -> None:
        for entry in self.pending:
            if entry.owner == owner:
                entry.dismissed = True
                entry.task.cancel()
