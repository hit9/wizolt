"""Bounded reverse RPC: a worker may call host services only inside an admitted action.

The read loop dispatches rather than awaits services: the result may depend on that same pipe.
Calls are children of a host request, never freestanding background work. Cancellation, EOF and
retirement cancel/join them; a late response cannot revive a finished action.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from wizolt.plugins.scope import CURRENT, InvocationScope
from wizolt.sdk import PluginError
from wizolt.sdk.models import HostCall


@dataclass
class PendingCall:
    parent: int
    task: asyncio.Task


class RequestDeadline:
    """Execution time excludes host-owned human interaction, not arbitrary worker waits."""

    def __init__(self, seconds: float):
        self.timer = asyncio.timeout(seconds)
        self.pauses = 0
        self.remaining = seconds
        self.closed = False

    async def __aenter__(self):
        await self.timer.__aenter__()
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return await self.timer.__aexit__(*exc)

    @contextmanager
    def pause(self):
        if not self.pauses:
            when = self.timer.when()
            self.remaining = max(0, when - asyncio.get_running_loop().time()) if when is not None else self.remaining
            self.timer.reschedule(None)
        self.pauses += 1
        try:
            yield
        finally:
            self.pauses -= 1
            if not self.pauses and not self.closed and not self.timer.expired():
                self.timer.reschedule(asyncio.get_running_loop().time() + self.remaining)


class HostCalls:
    MAX_CONCURRENT = 4
    TIMEOUT = 50

    def __init__(self, send: Callable[[dict], None], admitted: Callable[[int], bool]):
        self.send = send
        self.admitted = admitted
        self.handler: HostCall | None = None
        self.pending: dict[int, PendingCall] = {}
        self.suspend: Callable = lambda _parent: nullcontext()
        # Which services the parent request's operation may use; an admitted interceptor does
        # not gain every host service. Defaults deny UI and settings outside explicit actions.
        self.permits: Callable[[int, str], bool] = lambda _parent, service: not service.startswith(("ui.", "settings."))
        self.scope_of: Callable[[int], InvocationScope | None] = lambda _parent: None

    def dispatch(self, message: dict) -> None:
        identity, parent = message["service_id"], message["parent"]
        if type(identity) is not int or type(parent) is not int or identity in self.pending:
            raise PluginError("Invalid plugin host call identity")
        if not self.admitted(parent):
            self.send({"service_result": identity, "error": "Host services require an active explicit action"})
        elif not isinstance(message["service"], str) or not self.permits(parent, message["service"]):
            self.send({"service_result": identity, "error": f"{message['service']} is not available to this operation"})
        elif self.handler is None:
            self.send({"service_result": identity, "error": "Host services unavailable in offline trials"})
        elif len(self.pending) >= self.MAX_CONCURRENT:
            self.send({"service_result": identity, "error": "Too many concurrent host calls"})
        else:
            task = asyncio.create_task(self.run(identity, parent, message))
            self.pending[identity] = PendingCall(parent, task)

    async def run(self, identity: int, parent: int, message: dict) -> None:
        # The host-owned scope of the request this call belongs to, never one a worker supplies.
        CURRENT.set(self.scope_of(parent))
        try:
            assert self.handler is not None
            try:
                interactive = message["service"] == "ui.views.show"
                with self.suspend(parent) if interactive else nullcontext():
                    async with asyncio.timeout(None if interactive else self.TIMEOUT):
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


Resume = Callable[[object], Awaitable[dict]]


class Continuations:
    """Reserved reverse traffic: an interceptor's ``next()`` resumes its own chain on the host.

    Not a host service: it takes none of the four service permits and no 50-second deadline,
    because it waits for downstream work the chain owns. Each token is registered by the host for
    one request and is single-use; a reused, foreign or expired token is refused. The parent
    request's own deadline pauses only while that downstream work runs.
    """

    def __init__(self, send: Callable[[dict], None], admitted: Callable[[int], bool]):
        self.send = send
        self.admitted = admitted
        self.suspend: Callable = lambda _parent: nullcontext()
        self.tokens: dict[str, tuple[int, Resume]] = {}
        self.running: dict[str, tuple[int, asyncio.Task]] = {}

    def register(self, token: str, parent: int, resume: Resume) -> None:
        self.tokens[token] = (parent, resume)

    def dispatch(self, message: dict) -> None:
        token, parent = message["continue"], message["parent"]
        if type(token) is not str or type(parent) is not int:
            raise PluginError("Invalid plugin continuation")
        entry = self.tokens.pop(token, None)  # Single use, whatever happens next.
        if entry is None or entry[0] != parent or not self.admitted(parent):
            self.send({"continue_result": token, "error": "next() is single-use and belongs to its own handler call"})
            return
        task = asyncio.create_task(self.run(token, parent, entry[1], message.get("input")))
        self.running[token] = (parent, task)

    async def run(self, token: str, parent: int, resume: Resume, value: object) -> None:
        try:
            try:
                with self.suspend(parent):
                    result = {"value": await resume(value)}
            except Exception as error:  # noqa: BLE001 - downstream failure is reported to the handler.
                result = {"error": f"{type(error).__name__}: {error}"[:8192]}
            if self.admitted(parent):
                self.send({"continue_result": token, **result})
        except Exception as error:  # noqa: BLE001 - degrade to a small error frame instead of hanging the handler.
            from wizolt.plugins.process import FrameLimitError

            if self.admitted(parent) and isinstance(error, FrameLimitError):
                # The result exists but the host cannot send it: next() fails at once, not at its deadline.
                with contextlib.suppress(PluginError, BrokenPipeError, ConnectionResetError):
                    self.send({"continue_result": token, "error": str(error)[:8192]})
        except (PluginError, BrokenPipeError, ConnectionResetError):
            pass  # A departing worker cannot receive its continuation's result.
        finally:
            self.running.pop(token, None)

    async def cancel(self, parent: int | None = None) -> None:
        """Revoke a request's tokens and cancel/join its downstream work."""
        for token in [token for token, (owner, _) in self.tokens.items() if parent is None or owner == parent]:
            self.tokens.pop(token, None)
        tasks = [task for owner, task in self.running.values() if parent is None or owner == parent]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first step never enters run()'s finally block, and its
        # token is long gone: nothing else would ever clear it.
        for token in [token for token, (owner, _) in self.running.items() if parent is None or owner == parent]:
            self.running.pop(token, None)
