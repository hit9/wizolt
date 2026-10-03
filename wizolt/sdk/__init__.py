"""Public Python plugin contracts. No engine, session, or terminal objects cross this boundary.

SDK 1 is experimental. Callbacks run in the plugin's managed worker process.
UI callbacks return data; the host owns clipping, colors, scheduling, and terminal output.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

SDK_VERSION = 1
Value = str | int | float | bool


class PluginError(ValueError):
    """An actionable plugin registration, invocation, or lifecycle error."""


@dataclass(frozen=True)
class Context:
    """Read-only facts for one callback, not a handle into the live session.

    ``now`` is monotonic time for animation, not a wall-clock timestamp. Components receive
    their available cell width in ``columns``; they must not query or resize the terminal.
    Adding a fact here is preferable to exposing a mutable host object to plugins.
    """

    agent_id: str
    agent_name: str
    cwd: str
    status: str
    context_percent: float
    elapsed: float
    model: str
    now: float
    columns: int = 80


@dataclass(frozen=True)
class Text:
    """Plain text with a semantic theme role, never markup or terminal escape sequences."""

    text: str
    role: str = "text"


@dataclass(frozen=True)
class Panel:
    """A bounded stack of text rows. Empty rows are allowed; empty panels occupy no space."""

    rows: tuple[Text, ...] = ()


@dataclass(frozen=True)
class Event:
    name: str
    context: Context


Field = Callable[[Context], Value]
Component = Callable[[Context], Panel]
Observer = Callable[[Event], Awaitable[None]]
Handler = Callable[[Context, Mapping[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class Action:
    """Describe an explicit operation; rendering and observation never invoke it implicitly."""

    description: str
    handler: Handler
    parameters: Mapping[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    during_turn: bool = False


class Plugin:
    """One registration scope, rebuilt on reload and independently instantiated per agent.

    Keep mutable state in this scope's callbacks, not shared imported modules. Construction only
    registers capabilities; external effects belong in explicitly invoked handlers. Names are
    local to the plugin; the host supplies their namespace. Worker processes isolate crashes and
    imported module state, but have the user's filesystem/network permissions: not a sandbox.
    """

    EVENTS = frozenset(("turn.started", "turn.finished"))
    SLOTS = frozenset(("above_input", "status"))
    MAX_REGISTRATIONS = 64
    IDENTIFIER = r"[A-Za-z_][A-Za-z_0-9]*"
    COMMAND_NAME = r"[A-Za-z_][A-Za-z_0-9-]*"

    def __init__(self, name: str):
        self.name = name
        self.fields: dict[str, Field] = {}
        self.components: dict[str, Component] = {}
        self.commands: dict[str, Action] = {}
        self.tools: dict[str, Action] = {}
        self.observers: dict[str, list[Observer]] = {}

    @classmethod
    def _register(cls, registry: dict, name: str, value: object, *, pattern: str = IDENTIFIER) -> None:
        if not re.fullmatch(pattern, name):
            raise PluginError(f"Invalid registration {name!r}: use an ASCII Python identifier")
        if name in registry:
            raise PluginError(f"Duplicate registration: {name}")
        if len(registry) >= cls.MAX_REGISTRATIONS:
            raise PluginError(f"At most {cls.MAX_REGISTRATIONS} registrations per capability are supported")
        registry[name] = value

    @staticmethod
    def _callback(callback: Callable, *, asynchronous: bool) -> None:
        if not callable(callback) or inspect.iscoroutinefunction(callback) != asynchronous:
            raise PluginError(f"Expected {'async' if asynchronous else 'synchronous'} callback")

    def field(self, name: str, callback: Field) -> None:
        """Expose a cheap scalar read as ``plugins.<plugin>.<name>`` in bar templates."""
        self._callback(callback, asynchronous=False)
        self._register(self.fields, name, callback)

    def component(self, slot: str, callback: Component) -> None:
        """Register a pure projection; repeated paints must not advance application state."""
        if slot not in self.SLOTS:
            raise PluginError(f"Unknown UI slot {slot!r}; choose {', '.join(sorted(self.SLOTS))}")
        self._callback(callback, asynchronous=False)
        self._register(self.components, slot, callback)

    def command(self, name: str, description: str, handler: Handler, *, during_turn: bool = False) -> None:
        """Register ``/name``; native invocations pass raw trailing text as arguments['input'].

        Set during_turn only for operations safe alongside an active turn, such as changing
        plugin-owned UI state. This grants no access to mutate the host's model context.
        """
        self._callback(handler, asynchronous=True)
        self._register(self.commands, name, Action(description, handler, during_turn=during_turn), pattern=self.COMMAND_NAME)

    def tool(self, name: str, description: str, parameters: Mapping[str, Any], handler: Handler) -> None:
        """Register a model-invoked operation behind the core tool approval boundary."""
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        self._callback(handler, asynchronous=True)
        try:
            Draft202012Validator.check_schema(dict(parameters))
        except SchemaError as error:
            raise PluginError(f"Invalid tool schema for {name}: {error.message}") from error
        if parameters.get("type") != "object":
            raise PluginError("Tool parameters must describe an object")
        self._register(self.tools, name, Action(description, handler, parameters))

    def on(self, event: str, observer: Observer) -> None:
        """Observe a lifecycle event; observers cannot replace or cancel the host operation."""
        if event not in self.EVENTS:
            raise PluginError(f"Unknown event {event!r}; choose {', '.join(sorted(self.EVENTS))}")
        self._callback(observer, asynchronous=True)
        if sum(map(len, self.observers.values())) >= self.MAX_REGISTRATIONS:
            raise PluginError(f"At most {self.MAX_REGISTRATIONS} observers are supported")
        self.observers.setdefault(event, []).append(observer)
