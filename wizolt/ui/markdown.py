"""The app's Rich-backed Markdown rendering: the one renderable and the console it needs.

Rich (and, through it, ``markdown_it``) is imported here rather than in ``wizolt.ui.render`` so the
interactive first frame does not pay for it: Rich only renders completed output, which never exists
before the prompt is live. The palette half of rendering -- ``Theme`` -- stays in ``render`` and is
imported from there; every Rich render in the app goes through ``markdown_console`` built on it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Heading, Markdown, MarkdownElement, TableElement
from rich.syntax import Syntax
from rich.table import Table

from wizolt.ui.render import Theme


class _Heading(Heading):
    """Keep headings aligned with the transcript body."""

    LEVEL_ALIGN: ClassVar[dict[str, Any]] = dict.fromkeys(Heading.LEVEL_ALIGN, "left")


class _CodeBlock(CodeBlock):
    """A fenced block highlighted on the terminal's own background.

    Rich pads the block and fills it with the Pygments theme's background, which on a terminal that
    is not exactly that color reads as a misplaced rectangle. Dropping the fill and the padding
    leaves the highlighting, which is what the fence was for, and matches how the transcript
    renders the code it prints itself.
    """

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        del console, options
        yield Syntax(str(self.text).rstrip(), self.lexer_name, theme=self.theme, word_wrap=True, padding=0, background_color="default")


class _Table(TableElement):
    """A table with no outer edge, so it does not arrive wrapped in blank rows.

    Rich's `show_edge` draws an empty row above and below the table; with the blank line Markdown
    already puts between blocks, a table ends up floating two rows away from its own paragraph.
    The header underline is enough to bound it. A table whose headings are all empty is a key/value
    list (`/status`): with no header to bound it, it takes a rounded outline instead, and no rule
    between its columns.
    """

    # Rich's box spec, one line per part: top, header row, header rule, body row, row rule, footer
    # rule, footer row, bottom. The column divider is blank everywhere.
    KEY_VALUE_BOX = box.Box("╭──╮\n│  │\n├──┤\n│  │\n├──┤\n├──┤\n│  │\n╰──╯\n")

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        del console, options
        table = Table(box=box.SIMPLE, pad_edge=False, style="markdown.table.border", show_edge=False, collapse_padding=True)
        if self.header is not None and self.header.row is not None:
            cells = self.header.row.cells
            table.show_header = any(column.content.plain.strip() for column in cells)
            if not table.show_header:
                table.box, table.show_edge, table.pad_edge = self.KEY_VALUE_BOX, True, True
            for column in cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(heading)
        if self.body is not None:
            for row in self.body.rows:
                table.add_row(*[element.content for element in row.cells])
        yield table


class WizoltMarkdown(Markdown):
    """The one Markdown renderable in the app, so every surface lays a document out the same way.

    Hyperlinks stay off: prompt-toolkit cannot parse the OSC 8 escapes Rich emits for them, and
    `UiPrinter` strips those before they reach the terminal, so a hyperlink would only cost the URL.
    """

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "heading_open": _Heading,
        "fence": _CodeBlock,
        "code_block": _CodeBlock,
        "table_open": _Table,
    }

    def __init__(self, markup: str) -> None:
        # The theme's own Pygments style, so a fenced block is colored like the code the transcript
        # prints. `Theme.pygments_style` returning None means the name did not load; Rich falls back
        # to plain ANSI rather than raising on it.
        style = Theme.palette()["pygments"] if Theme.pygments_style() is not None else "ansi_dark"
        super().__init__(markup, code_theme=style, hyperlinks=False)


def markdown_console(width: int) -> Console:
    """A Rich console for the capture-then-emit path, carrying the palette's named styles.

    Every Rich render in the app goes through one of these. The console is always a capture target,
    never a direct writer: printing Rich output while the prompt-toolkit application is live
    interleaves raw escapes with its renderer, so callers capture and emit the result as ANSI.

    Colors come from the theme, so `wizolt.*` style names resolve here and nowhere else.
    """
    return Console(force_terminal=True, color_system="truecolor", no_color=False, width=max(10, width), theme=Theme.rich_theme())
