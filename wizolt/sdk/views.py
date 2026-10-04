"""Declarative interaction values. Plugins never receive a terminal or a live widget.

The same validated values cross worker RPC and feed offline authoring previews. Stable
IDs identify choices and actions; displayed labels are never interpreted as commands.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Literal, Self

from wizolt.sdk import PluginError
from wizolt.sdk.models import HostCall


@dataclass(frozen=True)
class Choice:
    id: str
    label: str
    preview: str = ""


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    key: str = ""


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    default: str = ""
    required: bool = False
    multiline: bool = False
    choices: tuple[Choice, ...] = ()


@dataclass(frozen=True)
class Document:
    text: str
    lexer: str = "text"
    kind: Literal["document"] = field(default="document", init=False)


@dataclass(frozen=True)
class Selection:
    items: tuple[Choice, ...]
    selected: tuple[str, ...] = ()
    multiple: bool = False
    kind: Literal["selection"] = field(default="selection", init=False)


@dataclass(frozen=True)
class Form:
    fields: tuple[Field, ...]
    kind: Literal["form"] = field(default="form", init=False)


@dataclass(frozen=True)
class View:
    title: str
    body: Document | Selection | Form
    actions: tuple[Action, ...] = ()
    fullscreen: bool = False

    @classmethod
    def decode(cls, value: dict) -> View:
        """Reject malformed or oversized worker data before it reaches host rendering."""
        try:
            data = dict(value)
            body = dict(data.pop("body"))
            kind = body.pop("kind")
            if kind == "document":
                content = Document(**body)
                _text(content.text, 256_000)
                _text(content.lexer, 80)
            elif kind == "selection":
                body["items"] = _choices(body["items"])
                body["selected"] = tuple(body.get("selected", ()))
                content = Selection(**body)
                if type(content.multiple) is not bool or any(item not in {c.id for c in content.items} for item in content.selected):
                    raise ValueError("Invalid selection")
            elif kind == "form":
                fields = tuple(Field(**(item | {"choices": _choices(item.get("choices", ()))})) for item in body["fields"])
                _identities(fields, 32)
                for item in fields:
                    _text(item.default, 16_000)
                    if type(item.required) is not bool or type(item.multiline) is not bool:
                        raise ValueError("Invalid field flags")
                    if item.choices and item.default and item.default not in {c.id for c in item.choices}:
                        raise ValueError("Field default must identify a choice")
                content = Form(fields)
                if set(body) != {"fields"} or not fields:
                    raise ValueError("Form requires fields")
            else:
                raise ValueError("Unknown view body")
            data["actions"] = tuple(Action(**item) for item in data.get("actions", ()))
            _identities(data["actions"], 16)
            keys = [item.key for item in data["actions"] if item.key]
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate action keys")
            for key in keys:
                _text(key, 40)
            result = cls(body=content, **data)
            _text(result.title, 200)
            if type(result.fullscreen) is not bool:
                raise ValueError("fullscreen must be boolean")
            return result
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise PluginError(f"Invalid view: {error}") from error


def _text(value: str, limit: int) -> None:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"Expected plain text of at most {limit} characters")


def _identities(items: Sequence, limit: int) -> None:
    if len(items) > limit:
        raise ValueError(f"At most {limit} entries are allowed")
    ids = []
    for item in items:
        _text(item.id, 100)
        _text(item.label, 300)
        if not item.id or "\n" in item.id or "\n" in item.label:
            raise ValueError("IDs and labels must be nonempty/single-line")
        ids.append(item.id)
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate IDs")


def _choices(values: Sequence[dict]) -> tuple[Choice, ...]:
    choices = tuple(Choice(**item) for item in values)
    _identities(choices, 1000)
    for item in choices:
        _text(item.preview, 16_000)
    return choices


@dataclass(frozen=True)
class ViewResult:
    action: str
    selected: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)


class OpenView:
    """An action-owned live view. Always use as an async context manager.

    Update replaces content while retaining host selection/scroll/input where possible.
    Completing an action closes this view; open the next view from ordinary Python flow.
    """

    def __init__(self, call: HostCall, view: View):
        self.call = call
        self.view = view
        self.id = uuid.uuid4().hex
        self.task: asyncio.Future | None = None

    async def __aenter__(self) -> Self:
        self.task = asyncio.ensure_future(self.call("ui.views.show", {"id": self.id, "view": asdict(self.view)}))
        await asyncio.sleep(0)  # Publish show before an immediate update/close on this handle.
        return self

    async def result(self) -> ViewResult | None:
        if self.task is None:
            raise PluginError("Open the view with async with before awaiting its result")
        value = (await self.task).get("result")
        return ViewResult(value["action"], tuple(value.get("selected", ())), value.get("values", {})) if value is not None else None

    async def update(self, view: View) -> None:
        if self.task is None or self.task.done():
            raise PluginError("View is not open")
        await self.call("ui.views.update", {"id": self.id, "view": asdict(view)})

    async def __aexit__(self, *exc) -> None:
        if self.task is not None:
            if not self.task.done() or self.task.cancelled():
                # Cancelling a worker waiter alone does not cancel host RPC. Close the
                # identified view, then join its response before the action can return.
                await self.call("ui.views.close", {"id": self.id})
            await asyncio.gather(self.task, return_exceptions=True)
