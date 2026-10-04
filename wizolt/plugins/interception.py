"""Ordered interceptor chains over live plugin generations, with scoped single-use continuations.

One executor serves every operation. An adapter supplies the core implementation and its own
transition/result rules; this module owns chain order, matching, leases, continuation tokens,
per-registration health and the failure policy. An empty or nonmatching chain calls the core
directly: no worker invocation, no payload encoding, no copies.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from wizolt.plugins.preferences import PluginPreferences
from wizolt.plugins.protocol import InterceptSpec
from wizolt.plugins.scope import CURRENT, MAX_DEPTH, InvocationScope
from wizolt.sdk import PluginError, operations
from wizolt.sdk.operations import OPERATIONS, Refusal, Value

if TYPE_CHECKING:
    from wizolt.plugins.runtime import Generation, PluginRuntime

Core = Callable[[Value], Awaitable[Value]]
# (value received, candidate passed to next) -> None, raising PluginError for an invalid change.
Transition = Callable[[Value, Value], None]
# (input, result, called_next) -> None, raising PluginError for an invalid result.
ResultCheck = Callable[[Value, Value, bool], None]


@dataclass(frozen=True)
class Link:
    owner: str
    generation: Generation
    spec: InterceptSpec
    operation: str

    @property
    def id(self) -> str:
        return f"{self.owner}/{self.operation}"

    @property
    def health(self) -> str:
        return f"intercept:{self.operation}"


class InterceptionOrder:
    """One user-level plugin order for every operation: listed names first, the rest by name.

    Stored as ``[plugin_manager.interception] order``, separate from plugin settings and
    component layout. Agents keep their loaded order until an explicit reload. Unknown names are
    retained, so disabling a plugin does not forget its place.
    """

    def __init__(self, preferences: PluginPreferences | None = None):
        self.preferences = preferences
        self.names: tuple[str, ...] = ()

    def load(self) -> None:
        if self.preferences is None:
            return
        order = self.preferences.read("interception").get("order", [])
        if not isinstance(order, list) or any(not isinstance(name, str) or not name.isidentifier() for name in order):
            raise PluginError("plugin_manager.interception.order must be a list of plugin names")
        self.names = tuple(dict.fromkeys(order))

    def ordered(self, names) -> list[str]:
        ranks = {name: index for index, name in enumerate(self.names)}
        return sorted(names, key=lambda name: (ranks.get(name, len(ranks)), name))

    def save(self, names: list[str]) -> None:
        if self.preferences is not None:
            self.preferences.save("interception", "order", list(names))
        self.names = tuple(names)


def check_read_only(previous: Value, candidate: Value) -> None:
    """Host-owned fields never change between steps, whatever ``replace`` was bypassed with."""
    for name in type(previous).READ_ONLY:
        if getattr(previous, name) != getattr(candidate, name):
            raise PluginError(f"{type(previous).__name__}.{name} is read-only")


class Interception:
    HANDLER_SECONDS = 60.0  # Each handler's own execution, excluding next() and human interaction.

    def __init__(self, runtime: PluginRuntime):
        self.runtime = runtime

    def chain(self, operation: str) -> list[Link]:
        """Snapshot the registrations in interception order. A stopping plugin admits no new work;
        an unhealthy one stays in place, so reaching it blocks instead of silently skipping it."""
        entries = {name: entry for name, entry in self.runtime.entries.items() if operation in entry.active.plugin.intercepts and not entry.disabling}
        return [
            Link(name, entries[name].active, entries[name].active.plugin.intercepts[operation], operation)
            for name in self.runtime.interception_order.ordered(entries)
        ]

    def active(self, operation: str) -> bool:
        return any(operation in entry.active.plugin.intercepts and not entry.disabling for entry in self.runtime.entries.values())

    def replaces(self, operation: str, value: Value) -> bool:
        """Whether a response-replacing registration could see this value. Match keys are
        read-only, so a later rewrite cannot change the answer."""
        return any(link.spec.response == "replace" and link.spec.matches(value) for link in self.chain(operation))

    async def run(
        self,
        operation: str,
        value: Value,
        core: Core,
        *,
        transition: Transition | None = None,
        result_check: ResultCheck | None = None,
        scope: InvocationScope | None = None,
    ) -> Value:
        links = self.chain(operation)
        parent = scope or CURRENT.get() or InvocationScope(agent=self.runtime.context().agent_id)
        skip = parent.active if parent.auxiliary else frozenset()
        if not any(link.spec.matches(value) and link.id not in skip for link in links):
            return await core(value)
        if parent.depth >= MAX_DEPTH:
            raise PluginError(f"Plugin operations nest deeper than {MAX_DEPTH} levels")
        own = parent.child(uuid.uuid4().hex)
        for link in links:
            link.generation.invocations += 1  # Pin participants for this operation's lifetime.
        try:

            async def step(index: int, current: Value) -> Value:
                for position in range(index, len(links)):
                    link = links[position]
                    if link.id in skip or not link.spec.matches(current):
                        continue
                    if reason := link.generation.failure(link.health):
                        raise PluginError(f"{link.owner}'s {operation} interceptor is blocked until it is reloaded or disabled: {reason}")
                    return await self.call(link, position, current, step, own, transition, result_check)
                restore = CURRENT.set(own)  # Core descendants enter their own full chains.
                try:
                    return await core(current)
                finally:
                    CURRENT.reset(restore)

            return await step(0, value)
        finally:
            for link in links:
                link.generation.invocations -= 1
            self.runtime.after_lease()

    async def call(
        self,
        link: Link,
        position: int,
        value: Value,
        step: Callable[[int, Value], Awaitable[Value]],
        scope: InvocationScope,
        transition: Transition | None,
        result_check: ResultCheck | None,
    ) -> Value:
        spec = OPERATIONS[link.operation]
        state: dict = {"used": False, "settled": False, "result": None, "error": None}

        async def resume(raw: object) -> dict:
            if state["used"]:
                raise PluginError("next() is single-use")
            state["used"] = True
            candidate = operations.decode(raw, (spec.input,))
            check_read_only(value, candidate)
            if transition is not None:
                transition(value, candidate)
            try:
                result = await step(position + 1, candidate)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                state["settled"], state["error"] = True, error
                raise
            state["settled"], state["result"] = True, result
            return operations.encode(result)

        token = uuid.uuid4().hex
        worker = link.generation.worker
        try:
            raw = await worker.request(
                "intercept",
                timeout=self.HANDLER_SECONDS,
                scope=scope.entering(link.id, auxiliary=True),
                on_start=lambda identity: worker.continuations.register(token, identity, resume),
                name=link.operation,
                token=token,
                input=operations.encode(value),
                context=asdict(self.runtime.facts()),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if state["error"] is not None:
                raise state["error"] from None  # Downstream failed; this handler only relayed it.
            raise self.block(link, str(error)) from error
        finally:
            worker.continuations.tokens.pop(token, None)
        if state["error"] is not None:
            # A handler cannot turn a downstream failure, such as a blocked inner policy, into success.
            raise state["error"]
        if state["used"] and not state["settled"]:
            raise self.block(link, "next() must be awaited inside the handler before it returns")
        try:
            result = operations.decode(raw, spec.results)
            if isinstance(result, Refusal) and state["used"]:
                raise PluginError("A refusal must come before next(): execution cannot be denied retroactively")
            if link.spec.response == "preserve" and not isinstance(result, Refusal) and result != state["result"]:
                raise PluginError('response="preserve" handlers must return the downstream response unchanged; declare "replace"')
            if result_check is not None:
                result_check(value, result, state["used"])
        except PluginError as error:
            raise self.block(link, str(error)) from error
        return result

    @staticmethod
    def block(link: Link, reason: str) -> PluginError:
        """Record the registration's failure and stop the operation. Never bypass it later."""
        link.generation.fail(link.health, reason)
        return PluginError(f"{link.owner}'s {link.operation} interceptor failed: {reason}")
