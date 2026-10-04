"""Typed values for intercepted operations. Plugins never receive host objects.

Each operation names one input type and the result types a handler may return. Values are
immutable; ``replace`` makes a candidate that the host validates at every step of the chain.
The same encoding crosses worker RPC, offline trials and fixtures, with explicit bounds instead
of silent truncation.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from wizolt.sdk import PluginError
from wizolt.sdk.settings import freeze

MAX_TEXT = 512 * 1024  # One value's text, in characters; the worker frame bound still applies.
MAX_ITEMS = 1000


def thaw(value: Any) -> Any:
    """Plain JSON data from frozen mappings, tuples and nested value dataclasses."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {item.name: thaw(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def _json_data(value: Any, *, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("JSON data is nested too deeply")
    if value is None or type(value) in (str, bool, int) or (type(value) is float and math.isfinite(value)):
        return
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_data(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _json_data(item, depth=depth + 1)
        return
    raise ValueError("Expected finite JSON data")


class Value:
    """Shared encoding and validation for operation values."""

    TYPE: ClassVar[str]
    READ_ONLY: ClassVar[tuple[str, ...]] = ()

    def replace(self, **changes: Any) -> Self:
        """A candidate copy; the host checks it before the next step or core execution."""
        forbidden = set(changes) & set(self.READ_ONLY)
        if forbidden:
            raise PluginError(f"{type(self).__name__} fields are read-only: {', '.join(sorted(forbidden))}")
        return dataclasses.replace(self, **changes)  # type: ignore[type-var]

    def check(self) -> None:
        """Validate shape and bounds. Subclasses add their own rules."""

    def encode(self) -> dict:
        return {"type": self.TYPE, **thaw(self)}

    @classmethod
    def decode(cls, data: dict) -> Self:
        values = {key: value for key, value in data.items() if key != "type"}
        return cls(**values)  # type: ignore[call-arg]


def _text(value: object, name: str, limit: int = MAX_TEXT) -> None:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} must be text of at most {limit} characters")


def _names(value: object, name: str) -> None:
    if not isinstance(value, tuple) or len(value) > MAX_ITEMS or any(not isinstance(item, str) or len(item) > 300 for item in value):
        raise ValueError(f"{name} must be a list of names")


@dataclass(frozen=True)
class Refusal(Value):
    """An expected, typed refusal before execution. Exceptions mean a failed interceptor."""

    TYPE: ClassVar[str] = "refusal"
    reason: str

    def check(self) -> None:
        _text(self.reason, "reason", 2000)
        if not self.reason.strip():
            raise ValueError("A refusal needs a reason")


@dataclass(frozen=True)
class Prompt(Value):
    """One submitted input. ``text`` reaches the model; ``original`` stays in user history.

    ``attachments`` names already admitted images. A handler may omit some, never add any.
    ``origin`` is ``user``, ``followup`` or ``child`` (a model-origin child inbox input).
    """

    TYPE: ClassVar[str] = "prompt"
    READ_ONLY: ClassVar[tuple[str, ...]] = ("original", "origin")
    text: str
    original: str = ""
    attachments: tuple[str, ...] = ()
    origin: str = "user"

    def check(self) -> None:
        _text(self.text, "text")
        _text(self.original, "original")
        _names(self.attachments, "attachments")
        if self.origin not in ("user", "followup", "child"):
            raise ValueError("Unknown prompt origin")

    @classmethod
    def decode(cls, data: dict) -> Prompt:
        return cls(data["text"], data.get("original", ""), tuple(data.get("attachments", ())), data.get("origin", "user"))


@dataclass(frozen=True)
class Block:
    """One named part of a request's context. Only ``editable`` blocks may change text.

    Plugin blocks are added with a short ID; the host namespaces it as ``plugin:<name>:<id>``.
    """

    id: str
    text: str
    editable: bool = False


@dataclass(frozen=True)
class Blocks(Value):
    """Ordered request-context blocks for one request; ``purpose`` is host-owned."""

    TYPE: ClassVar[str] = "blocks"
    READ_ONLY: ClassVar[tuple[str, ...]] = ("purpose",)
    blocks: tuple[Block, ...]
    purpose: str = "turn"

    def get(self, block_id: str) -> Block | None:
        return next((block for block in self.blocks if block.id == block_id), None)

    def with_text(self, block_id: str, text: str) -> Blocks:
        """Replace an editable block's text, or an own plugin block's."""
        if self.get(block_id) is None:
            raise PluginError(f"Unknown context block: {block_id}")
        return dataclasses.replace(self, blocks=tuple(dataclasses.replace(block, text=text) if block.id == block_id else block for block in self.blocks))

    def add(self, block_id: str, text: str) -> Blocks:
        """Add an own block. The host places plugin blocks after the header, before conversation."""
        if not block_id.isidentifier():
            raise PluginError("Plugin block IDs are ASCII identifiers")
        return dataclasses.replace(self, blocks=(*self.blocks, Block(block_id, text, editable=True)))

    def check(self) -> None:
        if not isinstance(self.blocks, tuple) or len(self.blocks) > 128:
            raise ValueError("blocks must be a list of at most 128 blocks")
        for block in self.blocks:
            if not isinstance(block, Block) or not isinstance(block.id, str) or len(block.id) > 200 or type(block.editable) is not bool:
                raise ValueError("Invalid context block")
            _text(block.text, f"block {block.id}")
        if len({block.id for block in self.blocks}) != len(self.blocks):
            raise ValueError("Duplicate context block IDs")

    @classmethod
    def decode(cls, data: dict) -> Blocks:
        return cls(tuple(Block(**item) for item in data["blocks"]), data.get("purpose", "turn"))


@dataclass(frozen=True)
class ModelToolCall:
    """A tool call in a model response; arguments are a JSON object."""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRequest(Value):
    """One logical model request. Only ``provider``, ``model`` and ``effort`` may change.

    ``purpose`` is ``turn``, ``vision``, ``compaction`` or ``plugin``; ``reason`` refines a turn
    request (``normal``, ``image_fallback``, ``tool_correction``). Empty routing fields mean the
    configured route. ``tools`` and ``message_count`` describe the request; instruction text is
    edited through ``context.compose``, never here.
    """

    TYPE: ClassVar[str] = "model_request"
    READ_ONLY: ClassVar[tuple[str, ...]] = ("id", "retry_of", "purpose", "reason", "tools", "message_count")
    id: str
    purpose: str
    reason: str = "normal"
    provider: str = ""
    model: str = ""
    effort: str = ""
    tools: tuple[str, ...] = ()
    message_count: int = 0
    retry_of: str = ""

    def check(self) -> None:
        for name in ("id", "purpose", "reason", "provider", "model", "effort", "retry_of"):
            _text(getattr(self, name), name, 300)
        _names(self.tools, "tools")
        if type(self.message_count) is not int or self.message_count < 0:
            raise ValueError("message_count must be a nonnegative integer")

    @classmethod
    def decode(cls, data: dict) -> ModelRequest:
        values = {key: value for key, value in data.items() if key != "type"}
        values["tools"] = tuple(values.get("tools", ()))
        return cls(**values)


@dataclass(frozen=True)
class ModelResponse(Value):
    """A complete model response: text and tool calls. ``provider``/``model`` are host-filled."""

    TYPE: ClassVar[str] = "model_response"
    text: str
    tool_calls: tuple[ModelToolCall, ...] = ()
    provider: str = ""
    model: str = ""

    def check(self) -> None:
        _text(self.text, "text")
        if not isinstance(self.tool_calls, tuple) or len(self.tool_calls) > 128:
            raise ValueError("tool_calls must be a list of at most 128 calls")
        for call in self.tool_calls:
            if not isinstance(call, ModelToolCall) or not isinstance(call.id, str) or not isinstance(call.name, str) or not isinstance(call.arguments, Mapping):
                raise TypeError("Invalid tool call")
            _json_data(call.arguments)

    @classmethod
    def decode(cls, data: dict) -> ModelResponse:
        calls = tuple(ModelToolCall(item["id"], item["name"], freeze(dict(item.get("arguments", {})))) for item in data.get("tool_calls", ()))
        return cls(data["text"], calls, data.get("provider", ""), data.get("model", ""))


@dataclass(frozen=True)
class ToolCall(Value):
    """A tool call before validation and approval. Only ``arguments`` may change."""

    TYPE: ClassVar[str] = "tool_call"
    READ_ONLY: ClassVar[tuple[str, ...]] = ("id", "tool")
    id: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def check(self) -> None:
        _text(self.id, "id", 300)
        _text(self.tool, "tool", 300)
        if not isinstance(self.arguments, Mapping):
            raise TypeError("arguments must be an object")
        _json_data(self.arguments)

    @classmethod
    def decode(cls, data: dict) -> ToolCall:
        return cls(data["id"], data["tool"], freeze(dict(data.get("arguments", {}))))


@dataclass(frozen=True)
class ToolResult(Value):
    """The model-visible result. ``status`` is ``ok`` or ``failed``; origin is recorded by the host."""

    TYPE: ClassVar[str] = "tool_result"
    content: str
    status: str = "ok"

    def check(self) -> None:
        _text(self.content, "content")
        if self.status not in ("ok", "failed"):
            raise ValueError("status must be ok or failed")


@dataclass(frozen=True)
class Compaction(Value):
    """The conversation span core selected for summary; read-only. ``trigger`` is ``auto`` or ``manual``."""

    TYPE: ClassVar[str] = "compaction"
    READ_ONLY: ClassVar[tuple[str, ...]] = ("text", "trigger")
    text: str
    trigger: str = "auto"

    def check(self) -> None:
        _text(self.text, "text", 16 * 1024 * 1024)
        if self.trigger not in ("auto", "manual"):
            raise ValueError("Unknown compaction trigger")


@dataclass(frozen=True)
class Summary(Value):
    """Summary text for the selected span; core validates it and owns the checkpoint."""

    TYPE: ClassVar[str] = "summary"
    text: str

    def check(self) -> None:
        _text(self.text, "text", 16000)
        if not self.text.strip():
            raise ValueError("A summary must not be empty")


@dataclass(frozen=True)
class Operation:
    """One interceptable boundary: its input, allowed results and match keys."""

    name: str
    input: type[Value]
    results: tuple[type[Value], ...]
    match: frozenset[str]
    response_modes: bool = False  # model.request declares preserve or replace.


OPERATIONS: dict[str, Operation] = {
    item.name: item
    for item in (
        Operation("prompt.submit", Prompt, (Prompt, Refusal), frozenset({"origin"})),
        Operation("context.compose", Blocks, (Blocks,), frozenset({"purpose"})),
        Operation("model.request", ModelRequest, (ModelResponse, Refusal), frozenset({"purpose", "reason"}), response_modes=True),
        Operation("tool.call", ToolCall, (ToolResult, Refusal), frozenset({"tool"})),
        Operation("context.compact", Compaction, (Summary,), frozenset({"trigger"})),
    )
}
TYPES: dict[str, type[Value]] = {cls.TYPE: cls for cls in (Refusal, Prompt, Blocks, ModelRequest, ModelResponse, ToolCall, ToolResult, Compaction, Summary)}


def encode(value: Value) -> dict:
    """Checked JSON data. Field bounds apply here; the transport's frame limit applies to the whole."""
    try:
        value.check()
    except (TypeError, ValueError, AttributeError) as error:
        raise PluginError(f"Invalid {type(value).__name__}: {error}") from error
    return value.encode()


def decode(data: object, allowed: tuple[type[Value], ...]) -> Value:
    """Decode only the expected types, rejecting malformed or oversized data explicitly."""
    try:
        if not isinstance(data, dict):
            raise TypeError("expected an object")
        cls = TYPES.get(str(data.get("type")))
        if cls is None or cls not in allowed:
            raise ValueError(f"expected {' or '.join(item.__name__ for item in allowed)}")
        value = cls.decode(data)
        value.check()
        return value
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise PluginError(f"Invalid operation value: {error}") from error
