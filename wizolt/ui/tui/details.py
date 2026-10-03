"""Read-only detail sheets. No session access, tool execution or modal ownership."""

from __future__ import annotations

import shutil
from typing import cast

from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.utils import get_cwidth

from wizolt.base import ApprovalView, Text
from wizolt.ui.render import UiPrinter
from wizolt.ui.tui.views import TUI_MODAL_PENDING

Row = list[tuple[str, str]]
Rows = list[Row]
DETAIL_BACK = object()
DIFF_LEXER = "diff"


def wrapped_rows(text: str, width: int, margin: str = "  ", style: str = "") -> Rows:
    """Wrap literal output without interpreting Markdown or dropping structural newlines."""
    rows: Rows = []
    for raw in text.splitlines():
        indent = raw[: len(raw) - len(raw.lstrip())][: max(0, width // 4)]
        rows.extend(Text.wrap_styled([("", margin)], [("", margin + indent)], [(style, raw)], width))
    return rows


class DetailSheet:
    """One read-only document with a bounded layout cache and independent scroll position.

    The caller supplies presentation data and owns opening/closing the modal. All sections
    share the same frame; the lexer selects code, literal text, diff or prose rendering.
    """

    def __init__(self, ui: UiPrinter, view: ApprovalView, *, back_on_escape: bool = False):
        self.ui = ui
        self.view = view
        self.back_on_escape = back_on_escape
        self.scroll = 0
        self._width = 0
        self._rows: Rows = []

    @staticmethod
    def _code_rows(text: str, lexer: str, width: int) -> Rows:
        lines = text.splitlines()
        highlighted = UiPrinter.code_lines(text, lexer)
        number_width = len(str(len(lines)))
        rows: Rows = []
        for index, line in enumerate(lines):
            content = highlighted[index] if highlighted is not None and index < len(highlighted) else [("class:text", line)]
            prefix = [("class:subtle", f"{index + 1:>{number_width}}  ")]
            continuation = [("", " " * (number_width + 2))]
            rows.extend(Text.wrap_styled(prefix, continuation, content, width))
        return rows

    def _diff_rows(self, text: str, width: int) -> Rows:
        rows: Rows = []
        for source, line in zip(text.splitlines(), self.ui.segment_lines(self.ui.diff_segments(text))):
            band = self.ui.diff_background(source)
            header = source.startswith((*self.ui.DIFF_HEADER_PREFIXES, "@@ "))
            continuation = "" if header else " " * min(self.ui.DIFF_GUTTER_WIDTH, max(0, width - 2))
            for row in Text.wrap_styled([], [("", continuation)], self.ui.remove_line_ending(line), width):
                if band:
                    used = sum(get_cwidth(piece) for _, piece in row)
                    row.append((band, " " * max(0, width - used)))
                rows.append(row)
        return rows

    @staticmethod
    def _markdown_rows(text: str, width: int) -> Rows:
        # Rich remains lazy: importing a viewer must not delay the initial prompt.
        from wizolt.ui.markdown import WizoltMarkdown, markdown_console

        console = markdown_console(max(1, width))
        hard_breaks = "\n".join(line.rstrip() + "  " for line in text.split("\n"))
        with console.capture() as capture:
            console.print(WizoltMarkdown(hard_breaks))
        cleaned = UiPrinter.strip_unknown_escapes(UiPrinter.strip_trailing_pad(capture.get()))
        rows: Rows = [[]]
        for style, fragment in cast(Row, list(to_formatted_text(ANSI(cleaned)))):
            for index, piece in enumerate(fragment.split("\n")):
                if index:
                    rows.append([])
                if piece:
                    rows[-1].append((style, piece))
        return rows

    @staticmethod
    def _panel(label: str, rows: Rows, width: int, *, source: bool = False) -> Rows:
        """Frame content already wrapped to width minus the eight cells of chrome."""
        label = Text.clip_width(label, max(0, width - 10))
        border = "class:detail.border"
        framed: Rows = [
            [
                (border, "  ╭── "),
                ("class:detail.section", label),
                (border, " " + "─" * max(0, width - get_cwidth(label) - 10) + "╮"),
            ]
        ]
        base = "class:detail.source" if source else "class:text"
        for row in rows:
            content = [(base + " " + style, text) for style, text in row]
            used = sum(get_cwidth(text) for _, text in content)
            framed.append([(border, "  │ "), *content, (base, " " * max(0, width - 8 - used)), (border, " │")])
        framed.append([(border, "  ╰" + "─" * max(0, width - 6) + "╯")])
        return framed

    def _metadata(self, width: int) -> Rows:
        rows: Rows = []
        fields: Row = []
        for label, value in self.view.rows:
            style = "class:text"
            if label == "exit":
                style = "class:choice.output.ok" if value == "0" else "class:choice.output.fail"
            elif label == "status" and value == "running":
                style = "class:choice.live"
            field = [("class:detail.field", label + " "), (style, value)]
            used = sum(get_cwidth(text) for _, text in fields)
            needed = sum(get_cwidth(text) for _, text in field)
            if fields and (used + needed + 5 > width - 4 or "\n" in value):
                rows.append([("", "  "), *fields])
                fields = []
            if needed > width - 4 or "\n" in value:
                rows.extend(Text.wrap_styled([("", "  ")], [("", "  ")], field, width - 2))
            else:
                if fields:
                    fields.append(("class:detail.border", "  ·  "))
                fields.extend(field)
        if fields:
            rows.append([("", "  "), *fields])
        return rows

    def _layout(self, width: int) -> Rows:
        if width == self._width:
            return self._rows
        view = self.view
        rows = self._metadata(width)
        inner = max(1, width - 8)
        body = view.text.rstrip()
        if body:
            if view.lexer == DIFF_LEXER:
                content = self._diff_rows(body, inner)
            elif view.lexer == "text":
                content = wrapped_rows(body, inner, "")
            elif view.lexer:
                content = self._code_rows(body, view.lexer, inner)
            else:
                content = self._markdown_rows(body, inner)
            rows.append([])
            rows.extend(
                self._panel(
                    view.section or view.label.split(" · ")[0],
                    content,
                    width,
                    source=view.lexer not in {"", "text", DIFF_LEXER},
                )
            )
        if view.result.strip():
            rows.append([])
            rows.extend(self._panel("result", wrapped_rows(view.result.rstrip(), inner, ""), width))
        # Retain one width only. A resize drag must not pin copies of a large log.
        self._width, self._rows = width, rows
        return rows

    @staticmethod
    def _size() -> tuple[int, int]:
        columns, rows = shutil.get_terminal_size((120, 24))
        # Title, rule, gap, footer, statusbar and one slack row.
        return max(20, columns), max(3, rows - 6)

    def _header(self, width: int) -> Row:
        head, _, name = self.view.label.partition(" · ")
        notice = " · read-only"
        head = Text.clip_width(head[:1].upper() + head[1:], max(0, width - len(notice) - 2))
        title: Row = [("class:detail.title", "  " + head)]
        room = width - get_cwidth("  " + head + " · " + notice)
        if name and room > 0:
            title.append(("class:detail.title.name", " · " + Text.clip_width(name, room)))
        title.append(("class:detail.title.meta", notice))
        used = sum(get_cwidth(text) for _, text in title)
        title.extend(
            [
                ("class:detail.title", " " * max(0, width - used) + "\n"),
                ("class:detail.border", "  " + "─" * max(0, width - 4) + "\n\n"),
            ]
        )
        return title

    def _footer(self, width: int, height: int, count: int) -> Row:
        action = "Esc/q back · Ctrl-O close" if self.back_on_escape else "Esc/q close"
        position = f" {min(self.scroll + height, count)}/{count} "
        available = max(0, width - get_cwidth(position))
        legends = [
            f"  ↑/↓ scroll · Ctrl-D/U half-page · g/G top/bottom · {action}",
            f"  ↑/↓ · g/G · {action}",
            "  " + action,
        ]
        legend = next((text for text in legends if get_cwidth(text) <= available), Text.clip_width(legends[-1], available))
        return [("class:choice.disabled", legend.ljust(available)), ("class:detail.field", position + "\n")]

    def fragments(self) -> StyleAndTextTuples:
        width, height = self._size()
        rows = self._layout(width)
        self.scroll = min(self.scroll, max(0, len(rows) - height))
        parts = self._header(width)
        for row in rows[self.scroll : self.scroll + height]:
            parts.extend(row)
            parts.append(("", "\n"))
        parts.extend(self._footer(width, height, len(rows)))
        return cast(StyleAndTextTuples, parts)

    def handle_key(self, key: str, _data: str) -> object:
        if self.back_on_escape and key in {"q", "escape", "c-c"}:
            return DETAIL_BACK
        if key in {"q", "c-o", "escape", "c-c"}:
            return None
        _, height = self._size()
        if key in {"down", "j", "c-n"}:
            self.scroll += 1
        elif key in {"up", "k", "c-p"}:
            self.scroll -= 1
        elif key in {"pagedown", "c-d"}:
            self.scroll += height if key == "pagedown" else max(1, height // 2)
        elif key in {"pageup", "c-u"}:
            self.scroll -= height if key == "pageup" else max(1, height // 2)
        elif key in {"g", "G"}:
            self.scroll = 0 if key == "g" else 10**9
        self.scroll = max(0, self.scroll)
        return TUI_MODAL_PENDING
