"""View, copy and edit one format -- a bar's, the divider's sweep, or a record -- in `/theme`.

The statusbar, divider and transcript tabs share this panel. It knows the setting's text, not
what that text draws: the picker hands it the configuration path that validates and previews a
draft, its preset registry, and any rendered samples. Nothing here parses or renders templates
or formulas.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.utils import get_cwidth

from wizolt.base import Text, TextFragments, TextRows
from wizolt.ui.bars import PRESETS, expand
from wizolt.ui.render import Theme
from wizolt.utils.clipboard import Clipboard

VIEW_KEYS = "c copy value · t copy TOML · e edit · j/k scroll · Esc back"
EDIT_KEYS = "Ctrl-S apply · Esc discard · arrows move · Enter new line"
# Each editable setting, by its preset registry (`ui.bars.PRESETS`), as a (TABLE, KEY) pair.
CONFIG_KEYS = {
    "statusbar": ("statusbar", "format"),
    "divider": ("divider", "format"),
    "sweep": ("divider", "sweep"),
    "transcript": ("transcript", "format"),
}
# Settings that live in their own top-level table rather than under `[ui]`.
TOP_LEVEL = frozenset({"transcript"})


def expanded(source: str, presets: Mapping[str, str]) -> str:
    """The template a value stands for. A `preset:` name the registry does not know is shown as
    the text it is, rather than raising in a key handler: a renamed or hand-typed preset is a
    config problem the config check already reports, not a reason to break the picker."""
    try:
        return expand(source, presets)
    except ValueError:
        return source


def toml_snippet(kind: str, source: str) -> str:
    import tomlkit  # Loaded on use, like every other config write: startup does not pay for it.

    table, key = CONFIG_KEYS[kind]
    root = {table: {key: source}} if kind in TOP_LEVEL else {"ui": {table: {key: source}}}
    return tomlkit.dumps(root)


class FormatPanel:
    """One setting -- a bar's format, the sweep formula, or a record format -- viewed, copied,
    or edited as a draft.

    `kind` names its preset registry (`presets`, defaulting to the bar registries). `current` is
    the value the picker would save, `saved` the one in effect when it opened.
    `apply` validates a draft through the setting's configuration path and previews it when valid,
    returning the problems otherwise; `accept` makes a valid draft the picker's selection. A
    discarded draft re-applies `current`, so the preview returns to it."""

    def __init__(
        self,
        kind: str,
        current: Callable[[], str],
        saved: str,
        *,
        apply: Callable[[str], list[str]],
        accept: Callable[[str], None],
        preview: Callable[[], StyleAndTextTuples] | None = None,
        presets: Mapping[str, str] | None = None,
    ) -> None:
        self.kind, self.current, self.saved = kind, current, saved
        self.apply, self.accept, self.preview = apply, accept, preview
        self.presets = presets if presets is not None else PRESETS[kind]
        self.draft: Document | None = None
        self.problems: list[str] = []
        self.notice: tuple[str, str] = ("", "")  # role, text
        self.show_toml = False
        self.top = 0

    @property
    def setting(self) -> str:
        return ".".join(CONFIG_KEYS[self.kind]) if self.kind in TOP_LEVEL else "ui." + ".".join(CONFIG_KEYS[self.kind])

    def handle_key(self, key: str, data: str = "") -> bool:
        """Whether the panel stays open."""
        if self.draft is not None:
            self.edit(key, data)
            return True
        if key == "escape":
            return False
        source = self.current()
        if key in {"c", "t"}:
            text, what = (source, "the value") if key == "c" else (toml_snippet(self.kind, source), "a TOML snippet")
            error = Clipboard.copy(text)
            self.show_toml = key == "t" and error is not None
            self.notice = (
                ("success", f"Copied {what} for {self.setting}.") if error is None else ("warning", f"Not copied: {error}. Select the text above instead.")
            )
        elif key == "e":
            # A preset is edited as the template or formula it stands for.
            self.draft = Document(expanded(source, self.presets))
            self.problems, self.notice, self.top = [], ("", ""), 0
        elif key in {"j", "down"}:
            self.top += 1
        elif key in {"k", "up"}:
            self.top = max(0, self.top - 1)
        return True

    def edit(self, key: str, data: str) -> None:
        draft = self.draft
        assert draft is not None
        if key == "escape":
            self.draft, self.problems = None, []
            self.apply(self.current())
            self.notice = ("muted", "Draft discarded.")
            return
        if key == "c-s":
            if self.problems:
                self.notice = ("error", "Fix the errors above before applying.")
                return
            self.accept(draft.text)
            self.draft = None
            self.notice = ("success", "Draft applied. Enter in the picker saves it; Esc there discards everything.")
            return
        text, cursor = draft.text, draft.cursor_position
        moves = {
            "left": draft.get_cursor_left_position,
            "right": draft.get_cursor_right_position,
            "up": draft.get_cursor_up_position,
            "down": draft.get_cursor_down_position,
            "home": draft.get_start_of_line_position,
            "c-a": draft.get_start_of_line_position,
            "end": draft.get_end_of_line_position,
            "c-e": draft.get_end_of_line_position,
        }
        if key in moves:
            self.draft = Document(text, cursor + moves[key]())
            return
        if key in {"backspace", "c-h"}:
            text, cursor = text[: max(0, cursor - 1)] + text[cursor:], max(0, cursor - 1)
        elif key == "delete":
            text = text[:cursor] + text[cursor + 1 :]
        else:
            # "any" and "paste" carry their text in `data`, which can be empty (a bare bracketed
            # paste); their names are never text. A one-character key name is its own character.
            typed = "\n" if key == "enter" else data if key in {"any", "paste"} else (data or key) if len(key) == 1 else ""
            typed = typed.replace("\r\n", "\n").replace("\r", "\n")
            if not typed:
                return
            text, cursor = text[:cursor] + typed + text[cursor:], cursor + len(typed)
        self.draft = Document(text, cursor)
        self.problems = self.apply(text)
        self.notice = ("", "")

    def rows(self, width: int, height: int) -> TextRows:
        """The panel at `width` columns in at most `height` rows. The keys come first and the
        latest notice last, so neither scrolls away behind a long template."""
        muted = Theme.fg("muted")
        source = self.current()
        state = "draft" if self.draft is not None else "selected, saved" if source == self.saved else "selected, not saved yet"
        rows: TextRows = [[(muted, Text.clip_width(EDIT_KEYS if self.draft is not None else VIEW_KEYS, width))]]
        if height >= 6:
            rows.append([(Theme.fg("accent", "bold"), self.setting), (muted, " · " + state)])
        notice = Text.wrap_styled([], [], [(Theme.fg(self.notice[0]), self.notice[1])], width) if self.notice[1] else []
        room = max(1, height - len(rows) - len(notice))
        if self.draft is not None:
            body = self.editor(width, room)
        else:
            body = self.view(source, width)
            self.top = min(self.top, max(0, len(body) - room))
            body = body[self.top : self.top + room]
        return [*rows, *body, *notice][: max(1, height)]

    def view(self, source: str, width: int) -> TextRows:
        rows = self.block("value", source, width)
        if self.show_toml:  # after a failed copy, what would have been copied
            rows += [[], *self.block("TOML", toml_snippet(self.kind, source).rstrip("\n"), width)]
        if source.startswith("preset:"):
            expansion = expanded(source, self.presets)
            if expansion != source:  # a name this build does not know expands to itself
                rows += [[], *self.block("expands to", expansion, width)]
        return rows

    @staticmethod
    def block(label: str, text: str, width: int) -> TextRows:
        return [[(Theme.fg("muted"), label)], *Text.wrap_styled([("", "  ")], [("", "  ")], [(Theme.fg("text"), text)], width)]

    def editor(self, width: int, height: int) -> TextRows:
        """The draft soft-wrapped and scrolled to its cursor, above its errors or its rendered
        preview. Feedback keeps at least a row, and the draft at least its cursor's row."""
        feedback: TextRows = []
        if self.problems:
            for problem in self.problems:
                feedback += Text.wrap_styled([], [], [(Theme.fg("error"), problem)], width)
        elif self.preview is not None:
            feedback = [[(fragment[0], fragment[1]) for fragment in row] for row in split_lines(self.preview())]
            if feedback and not feedback[-1]:
                feedback.pop()
        else:
            feedback = [[(Theme.fg("muted"), Text.clip_width("Valid. The status bar below previews the draft.", width))]]
        feedback = feedback[: max(1, height // 2)] if height > 1 else []
        rule = [[(Theme.fg("rule"), "─" * width)]] if height - len(feedback) > 2 else []
        room = max(1, height - len(feedback) - len(rule))
        lines, cursor_row = self.wrapped(width)
        self.top = min(max(self.top, cursor_row - room + 1), cursor_row)
        return [*lines[self.top : self.top + room], *rule, *feedback]

    def wrapped(self, width: int) -> tuple[TextRows, int]:
        """Draft rows at `width`, with the cursor as a reversed cell, and the cursor's row."""
        assert self.draft is not None
        text, cursor = self.draft.text, self.draft.cursor_position
        style, cursor_style = Theme.fg("text"), Theme.fg("text", "reverse")
        rows: TextRows = [[]]
        used = cursor_row = 0
        # A newline ends its row; the one appended gives a cursor at the very end a cell to sit in.
        for index, char in enumerate(text + "\n"):
            cell = " " if char == "\n" else char
            drawn = index == cursor or char != "\n"
            if drawn and used + get_cwidth(cell) > width and rows[-1]:
                rows.append([])
                used = 0
            if drawn:
                if index == cursor:
                    cursor_row = len(rows) - 1
                append(rows[-1], cursor_style if index == cursor else style, cell)
                used += get_cwidth(cell)
            if char == "\n" and index < len(text):
                rows.append([])
                used = 0
        return rows, cursor_row


def append(row: TextFragments, style: str, text: str) -> None:
    if row and row[-1][0] == style:
        row[-1] = (style, row[-1][1] + text)
    else:
        row.append((style, text))
