"""Plugin declarations adapted to ordinary host selectors, editors and detail sheets.

These states perform no RPC or plugin execution. Paint reads local values; callbacks, view
ownership and cancellation belong to the CLI adapter. Replacements preserve user navigation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document as BufferDocument
from prompt_toolkit.formatted_text import StyleAndTextTuples

from wizolt.base import SELECTION_BACK, ApprovalView, Text
from wizolt.sdk import PluginError
from wizolt.sdk.views import Document, Form, Selection, View, ViewResult
from wizolt.ui.render import UiPrinter
from wizolt.ui.tui.details import DetailSheet
from wizolt.ui.tui.keys import normalized_key
from wizolt.ui.tui.views import TUI_MODAL_PENDING, ChoiceViewState

Size = Callable[[], tuple[int, int]]


class BodyState(Protocol):
    """The shared host contract: content, interaction state and semantic result."""

    @property
    def editing(self) -> bool: ...
    def fragments(self) -> StyleAndTextTuples: ...
    def key(self, key: str, data: str) -> object: ...
    def result(self, action: str) -> ViewResult | None: ...
    def update(self, view: View) -> None: ...


class SelectionState:
    def __init__(self, view: View, size: Size):
        assert isinstance(view.body, Selection)
        self.size = size
        self.state = ChoiceViewState((), {}, set())
        self.checked = set(view.body.selected)
        self.update(view)
        if view.body.selected:
            self.state.selected = self.state.choices.index(view.body.selected[0])

    @property
    def editing(self) -> bool:
        return self.state.searching

    def update(self, view: View) -> None:
        assert isinstance(view.body, Selection)
        self.view = view
        self.body = view.body
        selected = self.state.selected_choice()
        self.state.choices = tuple(item.id for item in self.body.items)
        self.checked.intersection_update(self.state.choices)
        if selected in self.state.choices:
            self.state.selected = self.state.choices.index(selected)

    def fragments(self) -> StyleAndTextTuples:
        _, height = self.size()
        self.state.height = height
        self.state.labels = {item.id: (("[x] " if item.id in self.checked else "[ ] ") if self.body.multiple else "") + item.label for item in self.body.items}
        previews = {item.id: item.preview for item in self.body.items}
        return self.state.fragments(
            self.view.title, lambda item: previews[item], keys="↑/↓ j/k move · / search" + (" · Space toggle" if self.body.multiple else "")
        )

    def key(self, key: str, data: str) -> object:
        if not self.editing and self.body.multiple and (key == " " or data == " "):
            selected = self.state.selected_choice()
            if selected is not None:
                self.checked.symmetric_difference_update((selected,))
            return TUI_MODAL_PENDING
        return self.state.handle_key(key, data)

    def result(self, action: str) -> ViewResult:
        selected = (
            tuple(item for item in self.state.choices if item in self.checked) if self.body.multiple else tuple(filter(None, (self.state.selected_choice(),)))
        )
        return ViewResult(action, selected)


class DocumentState:
    def __init__(self, view: View, size: Size, ui: UiPrinter):
        self.size = size
        self.searching = False
        self.query = ""
        self.sheet = DetailSheet(ui, ApprovalView(view.title, ""), size=lambda: (size()[0], max(1, size()[1] - 6)))
        self.update(view)

    @property
    def editing(self) -> bool:
        return self.searching

    def update(self, view: View) -> None:
        assert isinstance(view.body, Document)
        self.sheet.replace(ApprovalView(view.title, view.body.text, lexer="" if view.body.lexer == "markdown" else view.body.lexer))

    def fragments(self) -> StyleAndTextTuples:
        return [
            *self.sheet.fragments(),
            ("class:muted", Text.clip_width("  /" + self.query if self.searching else "  / search · n next match", self.size()[0]) + "\n"),
        ]

    def key(self, key: str, data: str) -> object:
        if self.searching:
            if key == "escape":
                self.searching = False
            elif key == "enter":
                self.sheet.find(self.query)
                self.searching = False
            elif key in {"backspace", "c-h"}:
                self.query = self.query[:-1]
            elif text := _typed(key, data):
                self.query = (self.query + text.replace("\n", ""))[:200]
            return TUI_MODAL_PENDING
        if key == "/":
            self.searching, self.query = True, ""
        elif key == "n":
            self.sheet.find(self.query)
        else:
            return self.sheet.handle_key(key, data)
        return TUI_MODAL_PENDING

    def result(self, action: str) -> ViewResult:
        return ViewResult(action)


def _typed(key: str, data: str) -> str:
    text = data if key in {"any", "paste"} else key if len(key) == 1 else ""
    return "".join(c for c in text.replace("\r\n", "\n").replace("\r", "\n") if ord(c) >= 32 or c in "\n\t")


class FormState:
    def __init__(self, view: View, size: Size):
        self.size = size
        self.buffers: dict[str, Buffer] = {}
        self.selected = 0
        self.error = ""
        self.update(view)

    @property
    def editing(self) -> bool:
        return True  # Single-character action keys never steal text from an editor.

    def update(self, view: View) -> None:
        assert isinstance(view.body, Form)
        self.view, self.fields = view, view.body.fields
        self.buffers = {item.id: self.buffers.get(item.id) or Buffer(document=BufferDocument(item.default, len(item.default))) for item in self.fields}
        self.selected = min(self.selected, len(self.fields) - 1)

    def fragments(self) -> StyleAndTextTuples:
        width, height = self.size()
        rows = [[("class:choice.title", "  " + Text.clip_width(self.view.title, max(1, width - 2)))], []]
        cursor_row = 0
        for index, item in enumerate(self.fields):
            rows.append([("class:accent" if index == self.selected else "class:muted", "  " + Text.clip_width(item.label, max(1, width - 2)))])
            buffer = self.buffers[item.id]
            text = next((choice.label for choice in item.choices if choice.id == buffer.text), buffer.text) if item.choices else buffer.text
            cursor = min(buffer.cursor_position, len(text))
            spans = [("class:text", text)]
            if index == self.selected:
                spans = [("class:text", text[:cursor]), ("class:text reverse", text[cursor : cursor + 1] or " "), ("class:text", text[cursor + 1 :])]
            lines = Text.wrap_styled([("", "  ")], [("", "  ")], spans, max(3, width))
            if index == self.selected:
                cursor_row = len(rows) + next((i for i, line in enumerate(lines) if any("reverse" in style for style, _ in line)), 0)
            rows.extend(lines)
        room = max(1, height - 2)
        top = max(0, cursor_row - room + 1)
        parts: StyleAndTextTuples = [(style, text) for row in rows[top : top + room] for style, text in [*row, ("", "\n")]]
        parts.append(("class:error" if self.error else "class:muted", Text.clip_width("  " + (self.error or "Tab next field · Ctrl-S submit"), width) + "\n"))
        return parts

    def key(self, key: str, data: str) -> object:
        item = self.fields[self.selected]
        buffer = self.buffers[item.id]
        if key == "escape":
            return None
        if key in {"tab", "s-tab"}:
            self.selected = (self.selected + (1 if key == "tab" else -1)) % len(self.fields)
        elif key == "c-s" or (key == "enter" and not item.multiline):
            return self.result("submit") or TUI_MODAL_PENDING
        elif item.choices:
            if key in {"up", "down", "left", "right"}:
                ids = [choice.id for choice in item.choices]
                index = ids.index(buffer.text) if buffer.text in ids else 0
                buffer.text = ids[(index + (1 if key in {"down", "right"} else -1)) % len(ids)]
        elif key in {"left", "right", "up", "down"}:
            getattr(buffer, "cursor_" + key)()
        elif key in {"home", "c-a"}:
            buffer.cursor_position += buffer.document.get_start_of_line_position()
        elif key in {"end", "c-e"}:
            buffer.cursor_position += buffer.document.get_end_of_line_position()
        elif key in {"backspace", "c-h"}:
            buffer.delete_before_cursor()
        elif key == "delete":
            buffer.delete()
        else:
            text = "\n" if key == "enter" and item.multiline else _typed(key, data)
            if not item.multiline:
                text = text.replace("\n", " ")
            buffer.insert_text(text[: max(0, 16_000 - len(buffer.text))])
        return TUI_MODAL_PENDING

    def result(self, action: str) -> ViewResult | None:
        values = {item.id: self.buffers[item.id].text for item in self.fields}
        for index, item in enumerate(self.fields):
            if (item.required and not values[item.id].strip()) or (item.choices and values[item.id] not in {choice.id for choice in item.choices}):
                self.selected, self.error = index, f"Enter a value for {item.label}"
                return None
        return ViewResult(action, values=values)


class DialogState:
    """One declaration and one content state, with a single action/navigation policy."""

    RESERVED = frozenset({"escape", "c-c", "c-\\", "enter", "tab", "s-tab", "c-s", "up", "down", "left", "right", "home", "end", "pagedown", "pageup", "/"})

    def __init__(self, view: View, size: Size, ui: UiPrinter):
        self.size = size
        self.view = view
        self.validate(view)
        self.body: BodyState
        if isinstance(view.body, Selection):
            self.body = SelectionState(view, size)
        elif isinstance(view.body, Form):
            self.body = FormState(view, size)
        else:
            self.body = DocumentState(view, size, ui)

    @classmethod
    def validate(cls, view: View) -> None:
        keys = [normalized_key(action.key) for action in view.actions if action.key]
        if len(keys) != len(set(keys)):
            raise PluginError("Duplicate action key aliases")
        for action in view.actions:
            if action.key and normalized_key(action.key) in {normalized_key(key) for key in cls.RESERVED}:
                raise PluginError(f"Reserved view key: {action.key}")

    def update(self, view: View) -> None:
        self.validate(view)
        if type(view.body) is not type(self.view.body) or view.fullscreen != self.view.fullscreen:
            raise PluginError("Updates must retain the view kind and presentation mode")
        self.view = view
        self.body.update(view)

    def fragments(self) -> StyleAndTextTuples:
        width, _ = self.size()
        keys = " · ".join(
            f"{('Enter/' if index == 0 and item.key else '') + (item.key or 'Enter')} {item.label}" for index, item in enumerate(self.view.actions)
        )
        return [*self.body.fragments(), ("class:muted", Text.clip_width("  " + (keys or "Enter select") + " · Esc back", width) + "\n")]

    def key(self, key: str, data: str = "") -> object:
        if key == "c-c":
            return None
        actual = data if key == "any" else key
        for action in self.view.actions:
            if action.key and actual == normalized_key(action.key) and not (self.body.editing and len(actual) == 1):
                return self.body.result(action.id) or TUI_MODAL_PENDING
        editing = self.body.editing
        result = self.body.key(key, data)
        if result is None or result is SELECTION_BACK or isinstance(result, KeyboardInterrupt):
            return None
        if isinstance(result, (ViewResult, str)) or (key == "enter" and not editing and isinstance(self.view.body, Document)):
            return self.body.result(self.view.actions[0].id if self.view.actions else "submit") or TUI_MODAL_PENDING
        return result
