"""Public Python plugin contracts. No engine, session, or terminal objects cross this boundary.

SDK 1 is experimental. Callbacks run in the plugin's managed worker process.
UI callbacks return data; the host owns clipping, colors, scheduling, and terminal output.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from wizolt.sdk.services import Service

Resource = TypeVar("Resource")

SDK_VERSION = 1
Value = str | int | float | bool


class PluginError(ValueError):
    """An actionable plugin registration, invocation, or lifecycle error."""


@dataclass(frozen=True)
class Usage:
    """Agent-local cumulative usage and estimated live speed (characters / four / seconds)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    output_rate: float = 0


@dataclass(frozen=True)
class ContextWindow:
    """Reported fill and separately estimated categories from the latest prepared request."""

    used: int = 0
    limit: int = 0
    budget: int = 0
    parts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Viewport:
    """Terminal cells, not pixels. Layout may grant a component less space."""

    columns: int = 80
    rows: int = 24


@dataclass(frozen=True)
class Layout:
    """The current component's allocation; present only during component rendering."""

    slot: str
    columns: int
    rows: int
    gap_before: int = 0


@dataclass(frozen=True)
class ToolActivity:
    """One actual tool execution. IDs distinguish repeated provider call IDs and nesting."""

    id: str
    call_id: str
    name: str
    parent_id: str = ""
    status: str = "running"
    started_at: float = 0
    elapsed: float = 0


@dataclass(frozen=True)
class ToolCounts:
    """Actual executions this turn, excluding refusals and skipped calls."""

    started: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    running: int = 0


@dataclass(frozen=True)
class Turn:
    tools: ToolCounts = field(default_factory=ToolCounts)
    active_tools: tuple[ToolActivity, ...] = ()


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
    usage: Usage = field(default_factory=Usage)
    window: ContextWindow = field(default_factory=ContextWindow)
    viewport: Viewport = field(default_factory=Viewport)
    layout: Layout | None = None
    turn: Turn = field(default_factory=Turn)
    # Whether wizolt runs tools without asking (--yolo / runtime.yolo), so a plugin that asks
    # before risky work can stay quiet while wizolt's own approval is asking anyway.
    yolo: bool = False

    @classmethod
    def decode(cls, value: dict) -> Context:
        """Reconstitute immutable public values at the process boundary."""
        window = value.get("window", {})
        turn = value.get("turn", {})
        return cls(
            **{key: item for key, item in value.items() if key not in ("usage", "window", "viewport", "layout", "turn")},
            usage=Usage(**value.get("usage", {})),
            window=ContextWindow(**{**window, "parts": tuple(tuple(part) for part in window.get("parts", ()))}),
            viewport=Viewport(**value.get("viewport", {"columns": value.get("columns", 80)})),
            layout=Layout(**value["layout"]) if value.get("layout") else None,
            turn=Turn(ToolCounts(**turn.get("tools", {})), tuple(ToolActivity(**item) for item in turn.get("active_tools", ()))),
        )


@dataclass(frozen=True)
class Text:
    """Plain text with a semantic theme role, never markup or terminal escape sequences."""

    text: str
    role: str = "text"


@dataclass(frozen=True)
class Line:
    """One row of differently styled text spans; clipping belongs to the host."""

    spans: tuple[Text, ...] = ()

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)


@dataclass(frozen=True)
class Panel:
    """A bounded stack of text rows. Empty rows are allowed; empty panels occupy no space."""

    rows: tuple[Text | Line, ...] = ()


@dataclass(frozen=True)
class Event:
    name: str
    context: Context
    tool: ToolActivity | None = None
    reason: str = ""


Field = Callable[[Context], Value]
Component = Callable[[Context], Panel]
Observer = Callable[[Event], Awaitable[None]]
Handler = Callable[[Context, Mapping[str, Any]], Awaitable[str]]
# (context, operation value, next) -> result; next: async (value) -> downstream result.
InterceptHandler = Callable[[Context, Any, Callable[[Any], Awaitable[Any]]], Awaitable[Any]]


@dataclass(frozen=True)
class Interceptor:
    """Internal registration: one handler per operation, its prefilter and response mode."""

    handler: InterceptHandler
    match: Mapping[str, tuple[str, ...]]
    response: str = ""


@dataclass(frozen=True)
class PresenterRegistration:
    """Internal registration: one renderer per presentation site and its prefilter."""

    handler: Callable[[Context, Any], Awaitable[Panel]]
    match: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class ComponentRegistration:
    """Internal registration couples a renderer with its declared layout defaults."""

    callback: Component
    gap_before: int = 0


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

    EVENTS = frozenset(("session.started", "session.finished", "turn.started", "turn.finished", "tool.started", "tool.finished", "tick"))
    SLOTS = frozenset(("above_divider", "above_input", "below_input", "status"))
    MAX_REGISTRATIONS = 64
    IDENTIFIER = r"[A-Za-z_][A-Za-z_0-9]*"
    COMMAND_NAME = r"[A-Za-z_][A-Za-z_0-9-]*"

    def __init__(self, name: str, config: Mapping[str, Any] | None = None):
        from wizolt.sdk.models import Models
        from wizolt.sdk.services import Services
        from wizolt.sdk.settings import Settings, freeze
        from wizolt.sdk.ui import UI

        self.name = name
        self._settings = dict(config or {})
        self._config = freeze(self._settings)
        self.settings = Settings()
        self.fields: dict[str, Field] = {}
        self.components: dict[str, ComponentRegistration] = {}
        self.commands: dict[str, Action] = {}
        self.tools: dict[str, Action] = {}
        self.observers: dict[str, list[Observer]] = {}
        self.themes: dict[str, dict[str, Any]] = {}
        self.presets: dict[str, dict[str, str]] = {"statusbar": {}, "divider": {}}
        self.services = Services()
        self.models = Models()
        self.ui = UI()
        self._service_handles: dict[str, Service] = {}
        self.interceptors: dict[str, Interceptor] = {}
        self.presenters: dict[str, PresenterRegistration] = {}

    def service(self, name: str, factory: Callable[[], AbstractAsyncContextManager[Resource]]) -> Service[Resource]:
        """Register a lazy async context manager; disabling/reloading closes its resources.

        Setup and validation do not enter the manager. The first explicit ``get()`` starts it.
        Keep connection tasks inside the manager and cancel/join them in its finally block.
        Teardown is bounded by the host; an unresponsive worker is killed with its process group.
        """
        handle = self.services.register(factory)
        self._register(self._service_handles, name, handle)
        return handle

    @property
    def config(self) -> Mapping[str, Any]:
        """This generation's read-only user settings; nested tables/lists are immutable too."""
        return self._config

    def configure(self, schema: Mapping[str, Any], *, defaults: Mapping[str, Any] | None = None) -> None:
        """Validate settings during setup, before callbacks capture configuration values."""
        from wizolt.sdk.settings import resolve

        self._config = resolve(self._settings, schema, defaults or {})
        from copy import deepcopy

        self.settings.schema = deepcopy(dict(schema))
        self.settings.defaults = deepcopy(dict(defaults or {}))

    def theme(self, name: str, definition: Mapping[str, Any]) -> None:
        """Contribute a theme using the same base/colors/diff/highlights data as theme TOML.

        The host namespaces the choice as plugins.<plugin>.<name>. Registration never selects
        it automatically or overwrites a user's existing theme choice.
        """
        self._register(self.themes, name, dict(definition), pattern=self.COMMAND_NAME)

    def preset(self, kind: str, name: str, source: str) -> None:
        """Contribute a statusbar/divider format string to the existing appearance picker."""
        if kind not in self.presets:
            raise PluginError("Preset kind must be statusbar or divider")
        if not isinstance(source, str) or len(source) > 8192:
            raise PluginError("Preset must be a format string with at most 8192 characters")
        self._register(self.presets[kind], name, source, pattern=self.COMMAND_NAME)

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

    def component(self, slot: str, callback: Component, *, gap_before: int = 0) -> None:
        """Register a pure projection; repeated paints must not advance application state.

        gap_before declares inter-component spacing owned by the host, not panel content.
        Users can override it without editing the plugin; zero preserves compact layouts.
        """
        if slot not in self.SLOTS:
            raise PluginError(f"Unknown UI slot {slot!r}; choose {', '.join(sorted(self.SLOTS))}")
        if type(gap_before) is not int or gap_before < 0:
            raise PluginError("gap_before must be a nonnegative integer")
        self._callback(callback, asynchronous=False)
        self._register(self.components, slot, ComponentRegistration(callback, gap_before))

    def command(self, name: str, description: str, handler: Handler, *, during_turn: bool = False) -> None:
        """Register ``/name``; native invocations pass raw trailing text as arguments['input'].

        Set during_turn only for operations safe alongside an active turn, such as changing
        plugin-owned UI state. This grants no access to mutate the host's model context.
        """
        self._callback(handler, asynchronous=True)
        self._register(self.commands, name, Action(description, handler, during_turn=during_turn), pattern=self.COMMAND_NAME)

    def tool(self, name: str, description: str, parameters: Mapping[str, Any], handler: Handler) -> None:
        """Register an operation the agent can call through its ``Plugin`` tool, with approval.

        This does not add a standalone tool to the model's tool table or ToolScript. Discover
        it with Plugin list, obtain parameters with describe, and execute with call. The name
        alone is not callable. Description should explain what it does and when to use it.

        Offline trials can exercise handlers that need no host RPC. models.complete and
        ui.components require an active session: reload, then test through Plugin call.
        Plugin-owned resources acquired with service() can still be used in offline trials.
        """
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

    def presenter(self, site: str, render: Callable[[Context, Any], Awaitable[Panel]], *, match: Mapping[str, str | tuple[str, ...]] | None = None) -> None:
        """Render one named presentation site: ``async render(context, view) -> Panel``.

        Sites are ``tool.call`` (how a settled invocation is identified), ``tool.result``
        (its result summary) and ``activity`` (what the agent is doing). One registration
        per site. The panel replaces only that site's rows: approvals, citations, tags
        and queue facts stay host-owned. ``match`` prefilters on read-only view fields
        (the tool sites match on ``tool``); nonmatching calls never reach this worker.
        A failed, slow or conflicted presenter falls back to the builtin rendering.
        """
        from wizolt.sdk.presentation import SITES

        allowed = SITES.get(site)
        if allowed is None:
            raise PluginError(f"Unknown presentation site {site!r}; choose {', '.join(sorted(SITES))}")
        self._callback(render, asynchronous=True)
        if site in self.presenters:
            raise PluginError(f"Duplicate presenter for {site}; compose its cases in one handler")
        normalized: dict[str, tuple[str, ...]] = {}
        for key, value in (match or {}).items():
            if key not in allowed:
                raise PluginError(f"{site} matches only {', '.join(sorted(allowed)) or 'no fields'}")
            values = (value,) if isinstance(value, str) else tuple(value)
            if not values or any(not isinstance(item, str) or not item for item in values):
                raise PluginError(f"match[{key!r}] must be a name or a list of names")
            normalized[key] = values
        self.presenters[site] = PresenterRegistration(render, normalized)

    def intercept(
        self, operation: str, handler: InterceptHandler, *, match: Mapping[str, str | tuple[str, ...]] | None = None, response: str | None = None
    ) -> None:
        """Wrap a semantic operation: ``async handler(context, value, next)``.

        ``await next(value)`` runs the rest of the chain and then the host; call it at most once,
        inside the handler. Return its result, a replacement result, or a typed ``Refusal``
        before calling it. One registration per operation: compose cases in ordinary Python.
        ``match`` is a host-side prefilter on read-only input fields; nonmatching operations
        never reach this worker. ``model.request`` declares ``response="preserve"`` (default:
        route and pass the response through, keeping live streaming) or ``"replace"``.
        """
        from wizolt.sdk.operations import OPERATIONS

        spec = OPERATIONS.get(operation)
        if spec is None:
            raise PluginError(f"Unknown operation {operation!r}; choose {', '.join(sorted(OPERATIONS))}")
        self._callback(handler, asynchronous=True)
        if operation in self.interceptors:
            raise PluginError(f"Duplicate interceptor for {operation}; compose its cases in one handler")
        normalized: dict[str, tuple[str, ...]] = {}
        for key, value in (match or {}).items():
            if key not in spec.match:
                raise PluginError(f"{operation} matches only {', '.join(sorted(spec.match))}")
            values = (value,) if isinstance(value, str) else tuple(value)
            if not values or any(not isinstance(item, str) or not item for item in values):
                raise PluginError(f"match[{key!r}] must be a name or a list of names")
            normalized[key] = values
        if spec.response_modes:
            response = response or "preserve"
            if response not in ("preserve", "replace"):
                raise PluginError('response must be "preserve" or "replace"')
        elif response is not None:
            raise PluginError("response applies only to model.request")
        self.interceptors[operation] = Interceptor(handler, normalized, response or "")
