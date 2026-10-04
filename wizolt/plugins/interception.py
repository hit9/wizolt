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
from wizolt.sdk.operations import OPERATIONS, Refusal, Value, check_read_only

if TYPE_CHECKING:
    from wizolt.plugins.runtime import Generation, PluginRuntime

Core = Callable[[Value], Awaitable[Value]]
# (value received, candidate passed to next, acting plugin) -> a normalized candidate, or None to
# keep it; raises PluginError for an invalid change. The plugin name lets adapters namespace.
Transition = Callable[[Value, Value, str], Value | None]
# (input, result, downstream result or None if next was not called, acting plugin) -> a normalized
# result, or None; raises when invalid. Downstream lets adapters accept what inner steps added.
ResultCheck = Callable[[Value, Value, Value | None, str], Value | None]


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

    def would_match(self, operation: str, **fields: str) -> bool:
        """Whether any registration's prefilter accepts these read-only fields, before building
        a value. Adapters use it to keep nonmatching work on the unchanged fast path."""
        probe = type("Probe", (), fields)()
        return any(link.spec.matches(probe) for link in self.chain(operation))

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
        trace: dict | None = None,
    ) -> Value:
        """Run the chain around ``core``. ``trace`` (optional) receives provenance for receipts:
        ``chain`` (registrations invoked), ``origin`` (the one that produced the result without
        calling next, if any) and ``shaped`` (those that changed the downstream result)."""
        links = self.chain(operation)
        parent = scope or CURRENT.get() or InvocationScope(agent=self.runtime.context().agent_id)
        skip = parent.active if parent.auxiliary else frozenset()
        if not any(link.spec.matches(value) and link.id not in skip for link in links):
            return await core(value)
        if parent.depth >= MAX_DEPTH:
            raise PluginError(f"Plugin operations nest deeper than {MAX_DEPTH} levels")
        own = parent.child(uuid.uuid4().hex)
        trace = {} if trace is None else trace
        trace.setdefault("chain", [])
        task = asyncio.current_task()
        for link in links:
            link.generation.invocations += 1  # Pin participants for this operation's lifetime.
            if task is not None:
                link.generation.operations.add(task)
        try:

            async def step(index: int, current: Value) -> Value:
                for position in range(index, len(links)):
                    link = links[position]
                    if link.id in skip or not link.spec.matches(current):
                        continue
                    if reason := link.generation.failure(link.health):
                        raise PluginError(f"{link.owner}'s {operation} interceptor is blocked until it is reloaded or disabled: {reason}")
                    trace["chain"].append(link.id)
                    return await self.call(link, position, current, step, own, transition, result_check, trace)
                restore = CURRENT.set(own)  # Core descendants enter their own full chains.
                try:
                    return await core(current)
                finally:
                    CURRENT.reset(restore)

            return await step(0, value)
        finally:
            for link in links:
                link.generation.invocations -= 1
                link.generation.operations.discard(task)  # type: ignore[arg-type]
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
        trace: dict,
    ) -> Value:
        spec = OPERATIONS[link.operation]
        state: dict = {"used": False, "settled": False, "result": None, "error": None, "invalid": None}

        async def resume(raw: object) -> dict:
            if state["used"]:
                raise PluginError("next() is single-use")
            state["used"] = True
            try:
                candidate = operations.decode(raw, (spec.input,))
                check_read_only(value, candidate)
                if transition is not None:
                    candidate = transition(value, candidate, link.owner) or candidate
            except PluginError as error:
                state["invalid"] = error  # The handler's fault, whatever it does with the error.
                raise
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
        data = operations.encode(value)  # A host value over the bounds fails the operation, never blocks this plugin.
        raw, failure = None, None
        try:
            raw = await worker.request(
                "intercept",
                timeout=self.HANDLER_SECONDS,
                scope=scope.entering(link.id, auxiliary=True),
                on_start=lambda identity: worker.continuations.register(token, identity, resume),
                name=link.operation,
                token=token,
                input=data,
                context=asdict(self.runtime.facts()),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - resolved below, after what next() recorded.
            failure = error
        finally:
            worker.continuations.tokens.pop(token, None)
        # An invalid next() is the handler's fault whatever it did with the error. A downstream
        # failure wins over the handler's outcome: relaying it is not this handler's failure, and
        # catching it cannot turn a blocked inner policy into success.
        if state["invalid"] is not None:
            raise self.block(link, str(state["invalid"])) from None
        if state["error"] is not None:
            raise state["error"]
        if failure is not None:
            raise self.block(link, str(failure)) from failure
        if state["used"] and not state["settled"]:
            raise self.block(link, "next() must be awaited inside the handler before it returns")
        try:
            result = operations.decode(raw, spec.results)
            if isinstance(result, Refusal) and state["used"]:
                raise PluginError("A refusal must come before next(): execution cannot be denied retroactively")
            if link.spec.response == "preserve" and not isinstance(result, Refusal) and result != state["result"]:
                raise PluginError('response="preserve" handlers must return the downstream response unchanged; declare "replace"')
            if result_check is not None:
                result = result_check(value, result, state["result"] if state["used"] else None, link.owner) or result
        except PluginError as error:
            raise self.block(link, str(error)) from error
        if not state["used"]:
            trace.setdefault("origin", link.id)  # The innermost producer returns first.
        elif result != state["result"]:
            trace.setdefault("shaped", []).append(link.id)
        return result

    @staticmethod
    def block(link: Link, reason: str) -> PluginError:
        """Record the registration's failure and stop the operation. Never bypass it later."""
        link.generation.fail(link.health, reason)
        return PluginError(f"{link.owner}'s {link.operation} interceptor failed: {reason}")
