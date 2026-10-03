"""Bounded reverse RPC: a worker may call host services only inside an admitted action.

The read loop dispatches rather than awaits services: the result may depend on that same pipe.
Calls are children of a host request, never freestanding background work. Cancellation, EOF and
retirement cancel/join them; a late response cannot revive a finished action.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from wizolt.sdk import PluginError
from wizolt.sdk.models import HostCall


@dataclass
class PendingCall:
    parent: int
    task: asyncio.Task


class HostCalls:
    MAX_CONCURRENT = 4
    TIMEOUT = 50

    def __init__(self, send: Callable[[dict], None], admitted: Callable[[int], bool]):
        self.send = send
        self.admitted = admitted
        self.handler: HostCall | None = None
        self.pending: dict[int, PendingCall] = {}

    def dispatch(self, message: dict) -> None:
        identity, parent = message["service_id"], message["parent"]
        if type(identity) is not int or type(parent) is not int or identity in self.pending:
            raise PluginError("Invalid plugin host call identity")
        if not self.admitted(parent):
            self.send({"service_result": identity, "error": "Host services require an active explicit action"})
        elif self.handler is None:
            self.send({"service_result": identity, "error": "Host services unavailable in offline trials"})
        elif len(self.pending) >= self.MAX_CONCURRENT:
            self.send({"service_result": identity, "error": "Too many concurrent host calls"})
        else:
            task = asyncio.create_task(self.run(identity, parent, message))
            self.pending[identity] = PendingCall(parent, task)

    async def run(self, identity: int, parent: int, message: dict) -> None:
        try:
            assert self.handler is not None
            try:
                async with asyncio.timeout(self.TIMEOUT):
                    result = {"value": await self.handler(message["service"], message["arguments"])}
            except Exception as error:  # noqa: BLE001 - plugin services are an error boundary.
                result = {"error": f"{type(error).__name__}: {error}"[:8192]}
            if self.admitted(parent):
                try:
                    self.send({"service_result": identity, **result})
                except PluginError as error:
                    self.send({"service_result": identity, "error": str(error)[:8192]})
        except (PluginError, BrokenPipeError, ConnectionResetError):
            pass  # A departing worker cannot receive a service result.
        finally:
            self.pending.pop(identity, None)

    async def cancel(self, parent: int | None = None) -> None:
        calls = {identity: call for identity, call in self.pending.items() if parent is None or call.parent == parent}
        for call in calls.values():
            call.task.cancel()
        await asyncio.gather(*(call.task for call in calls.values()), return_exceptions=True)
        # A task cancelled before its first step never enters run()'s finally block.
        for identity, call in calls.items():
            if self.pending.get(identity) is call:
                self.pending.pop(identity)
