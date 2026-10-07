"""Declarative interaction values. Plugins never receive a terminal or a live widget.

The same validated values cross worker RPC and feed offline authoring previews. Stable
IDs identify choices and actions; displayed labels are never interpreted as commands.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Literal, Self

from wizolt.sdk import PluginError
from wizolt.sdk.models import HostCall


@dataclass(frozen=True)
class Choice:
    """A stable value, visible label and optional literal preview for a selector."""

    id: str
    label: str
    preview: str = ""


@dataclass(frozen=True)
class Action:
    """An action ID returned to the caller; key is optional and scoped to its view."""

    id: str
    label: str
    key: str = ""


@dataclass(frozen=True)
class Field:
    """An editable text value, or a choice ID when choices are supplied."""

    id: str
    label: str
    default: str = ""
    required: bool = False
    multiline: bool = False
    choices: tuple[Choice, ...] = ()


@dataclass(frozen=True)
class Section:
    """A labelled part of a Document, drawn after its text under a gray rule carrying the label."""

    label: str
    text: str


@dataclass(frozen=True)
class Document:
    """Read-only text; lexer selects plain text, Markdown or syntax highlighting. Sections
    continue it in the same frame and lexer, each set apart by its labelled rule."""

    text: str
    lexer: str = "text"
    sections: tuple[Section, ...] = ()
    kind: Literal["document"] = field(default="document", init=False)


@dataclass(frozen=True)
class Selection:
    """Searchable choices; selected supplies initial IDs, multiple enables checkboxes."""

    items: tuple[Choice, ...]
    selected: tuple[str, ...] = ()
    multiple: bool = False
    kind: Literal["selection"] = field(default="selection", init=False)


@dataclass(frozen=True)
class Form:
    """Ordered fields submitted together; required fields are checked by the host."""

    fields: tuple[Field, ...]
    kind: Literal["form"] = field(default="form", init=False)


@dataclass(frozen=True)
class View:
    """A transient view. Fullscreen uses the alternate screen and restores scrollback."""

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
                body["sections"] = tuple(Section(**item) for item in body.get("sections", ()))
                content = Document(**body)
                _text(content.text, 256_000)
                _text(content.lexer, 80)
                if len(content.sections) > 32:
                    raise ValueError("At most 32 sections are allowed")
                for section in content.sections:
                    _text(section.label, 200)
                    _text(section.text, 256_000)
                    if "\n" in section.label:
                        raise ValueError("Section labels must be single-line")
                if sum(len(section.text) for section in content.sections) + len(content.text) > 256_000:
                    raise ValueError("Document is limited to 256000 characters")
            elif kind == "selection":
                body["items"] = _choices(body["items"])
                body["selected"] = tuple(body.get("selected", ()))
                content = Selection(**body)
                identities = {c.id for c in content.items}
                # Keep validation linear even for malformed worker payloads: it runs on
                # the host event loop before any cancellable interaction is opened.
                if (
                    type(content.multiple) is not bool
                    or len(content.selected) > len(identities)
                    or any(item not in identities for item in content.selected)
                    or len(set(content.selected)) != len(content.selected)
                ):
                    raise ValueError("Invalid selection")
                if not content.multiple and len(content.selected) > 1:
                    raise ValueError("Single selection accepts at most one default")
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
            if any(not item.key for item in data["actions"][1:]):
                raise ValueError("Actions after the default require a key")
            keys = [item.key for item in data["actions"] if item.key]
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate action keys")
            for key in keys:
                _text(key, 40)
            result = cls(body=content, **data)
            _text(result.title, 200)
            if "\n" in result.title:
                raise ValueError("Title must be single-line")
            if type(result.fullscreen) is not bool:
                raise ValueError("fullscreen must be boolean")
            return result
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise PluginError(f"Invalid view: {error}") from error


# C0 controls except tab and newline, DEL and C1 controls. A regex scans at C speed: the host
# validates every view and update on its UI loop, and a per-character loop over a full frame
# took about 30 ms, which an action updating in a loop could repeat for its whole deadline.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _text(value: str, limit: int) -> None:
    if not isinstance(value, str) or len(value) > limit or _CONTROL.search(value):
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
    """Completed action with stable choice IDs and field values; cancellation is None."""

    action: str
    selected: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)

    @classmethod
    def resolve(cls, view: View, reply: dict) -> ViewResult:
        """Resolve semantic input identically for keyboard and scripted answers.

        Omitted values use declaration defaults. Adapters may supply current drafts instead;
        field errors identify where a human editor should restore focus, without owning UI.
        """
        if not isinstance(reply, dict) or reply.keys() - {"action", "selected", "values"}:
            raise ResponseError("Reply must be a view result")
        action = reply.get("action", view.actions[0].id if view.actions else "submit")
        if not isinstance(action, str) or action not in ({item.id for item in view.actions} if view.actions else {"submit"}):
            raise ResponseError("Unknown view action")
        selected, values = reply.get("selected", []), reply.get("values", {})
        if not isinstance(selected, (list, tuple)) or any(not isinstance(item, str) for item in selected):
            raise ResponseError("Selection IDs must be a list of strings")
        if not isinstance(values, dict):
            raise ResponseError("Form values must be an object")
        body = view.body
        if isinstance(body, Selection):
            default = body.selected or (() if body.multiple else tuple(item.id for item in body.items[:1]))
            selected = reply.get("selected", default)
            identities = {item.id for item in body.items}
            if len(selected) != len(set(selected)) or any(item not in identities for item in selected):
                raise ResponseError("Unknown or duplicate selection IDs")
            if not body.multiple and len(selected) != min(1, len(body.items)):
                raise ResponseError("Select exactly one choice")
            if values:
                raise ResponseError("Selection replies cannot contain form values")
            return cls(action, tuple(item.id for item in body.items if item.id in selected))
        if isinstance(body, Form):
            if selected or values.keys() - {item.id for item in body.fields}:
                raise ResponseError("Unknown form fields")
            resolved = {}
            for item in body.fields:
                value = values.get(item.id, item.default)
                try:
                    _text(value, 16_000)
                    if not item.multiline and "\n" in value:
                        raise ValueError("Expected single-line text")
                    if (item.required and not value.strip()) or (item.choices and value not in {choice.id for choice in item.choices}):
                        raise ValueError(f"Enter a value for {item.label}")
                except ValueError as error:
                    raise ResponseError(str(error), field=item.id) from error
                resolved[item.id] = value
            return cls(action, values=resolved)
        if selected or values:
            raise ResponseError("Document replies only support actions")
        return cls(action)


class ResponseError(PluginError):
    """Invalid semantic input, optionally attached to a form field for focus recovery."""

    def __init__(self, message: str, *, field: str = ""):
        super().__init__(message)
        self.field = field


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
