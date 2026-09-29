"""Start the real input application before assembling its session and command loop."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.vt100 import Vt100Input
from prompt_toolkit.styles import DynamicStyle, Style

from wizolt.base import run_blocking
from wizolt.image import UserInput
from wizolt.ui.render import Theme
from wizolt.ui.tui import TuiApp
from wizolt.utils import terminal

if TYPE_CHECKING:
    from wizolt.ui.cli import CommandLoop


class _BackgroundReply:
    """Remove OSC 11/device replies before the key parser, preserving keys and pasted text."""

    COLOR = re.compile(r"\x1b\]11;rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}(?:\x07|\x1b\\)")
    ATTRIBUTES = re.compile(r"\x1b\[\?[0-9;]*c")
    PARTIAL_COLOR = re.compile(r"\x1b\]11;rgb:[0-9a-fA-F/]{0,32}\x1b?")
    PARTIAL_ATTRIBUTES = re.compile(r"\x1b\[\?[0-9;]{0,64}")

    def __init__(self, forward: Callable[[str], None], in_paste: Callable[[], bool], received: Callable[[tuple[int, int, int]], None]):
        self.forward = forward
        self.in_paste = in_paste
        self.received = received
        self.pending = ""

    def feed(self, data: str) -> None:
        if not self.pending and "\x1b" not in data:
            self.forward(data)
            return
        for index, char in enumerate(data):
            if self.in_paste():
                self.forward(data[index:])
                return
            self.pending += char
            text = self.pending
            if self.COLOR.fullmatch(text):
                self.pending = ""
                color = terminal.parse_background(text.encode())
                assert color is not None
                self.received(color)
            elif self.ATTRIBUTES.fullmatch(text):
                self.pending = ""
            elif (
                any(prefix.startswith(text) for prefix in ("\x1b]11;rgb:", "\x1b[?"))
                or self.PARTIAL_COLOR.fullmatch(text)
                or self.PARTIAL_ATTRIBUTES.fullmatch(text)
            ):
                continue
            else:
                self.flush()

    def flush(self) -> None:
        # An idle gap inside a recognizable terminal reply is not an Escape key. Keep its
        # bounded partial sequence for the next input chunk; lone Esc and ambiguous prefixes
        # still follow prompt-toolkit's normal timeout so keyboard chords remain responsive.
        if (
            self.pending.startswith("\x1b]11;") and ("\x1b]11;rgb:".startswith(self.pending) or self.PARTIAL_COLOR.fullmatch(self.pending))
        ) or self.PARTIAL_ATTRIBUTES.fullmatch(self.pending):
            return
        text, self.pending = self.pending, ""
        if text:
            self.forward(text)


def run_startup(assemble: Callable[[], CommandLoop], banner: str) -> tuple[int, CommandLoop | None]:
    """Own one loop from the first editable frame through the session runtime's shutdown."""
    return asyncio.run(_run_startup(assemble, banner))


async def _run_startup(assemble: Callable[[], CommandLoop], banner: str) -> tuple[int, CommandLoop | None]:
    pending: list[UserInput] = []
    stopping = asyncio.Event()
    ready = asyncio.Event()
    command_loop: CommandLoop | None = None
    terminal.remember_background(None)
    style = Style.from_dict(
        {
            **Theme.tui_styles(),
            "prompt": Theme.inline("accent", "bold"),
            "bottom-toolbar": "noreverse bg:default fg:default",
            "bottom-toolbar.text": "noreverse bg:default fg:default",
            "search-toolbar": "noreverse bg:default fg:default",
        }
    )

    def submit(value: UserInput) -> None:
        if not value.images and str(value).strip() in {"/quit", "/exit"}:
            stopping.set()
        else:
            pending.append(value)
            # The normal submit path enters DISPATCH until the runtime admits the line. During
            # assembly there is no dispatcher yet; keep accepting further lines into the FIFO.
            tui.set_idle()

    tui = TuiApp(
        on_chat_submit=submit,
        on_exit_request=stopping.set,
        on_force_exit=stopping.set,
        on_interrupt=stopping.set,
        input_hint_fn=lambda: "starting…",
        history=InMemoryHistory(),
    )
    tui.scrollback.transcript.append(banner)
    tui.on_ready = ready.set
    application = asyncio.create_task(tui.run(style=DynamicStyle(lambda: command_loop.view.style() if command_loop else style)))
    waiting = asyncio.create_task(ready.wait())
    restore_input: Callable[[], None] = lambda: None

    def assembled(value: CommandLoop) -> None:
        nonlocal command_loop
        command_loop = value

    try:
        await asyncio.wait({waiting, application}, return_when=asyncio.FIRST_COMPLETED)
        if application.done():
            await application
            return 0, None
        app = tui.app
        assert app is not None
        if isinstance(app.input, Vt100Input):
            parser = app.input.vt100_parser
            feed, flush = parser.feed, parser.flush

            def received(color: tuple[int, int, int]) -> None:
                terminal.remember_background(color)
                if command_loop is not None:
                    command_loop.configure_theme()
                    tui.recolor()

            replies = _BackgroundReply(feed, lambda: parser._in_bracketed_paste, received)
            parser.feed = replies.feed

            def flush_input() -> None:
                replies.flush()
                flush()

            parser.flush = flush_input

            def restore_input() -> None:
                parser.feed, parser.flush = feed, flush

            app.output.write_raw("\x1b]11;?\x1b\\\x1b[c")
            app.output.flush()
        if stopping.is_set():
            return 0, None
        # Assembly is unpublished until this worker completes. Cancellation waits for it so the
        # entry point can always release a session/lease the worker acquired, even on early exit.
        await run_blocking(assemble, commit=assembled)
        assert command_loop is not None
        if application.done():
            await application
            return 0, None
        if stopping.is_set():
            return 0, None
        command_loop.configure_theme()
        from wizolt.ui.cli.runtime import TuiRuntime

        code = await TuiRuntime(command_loop).run(show_banner=False, tui=tui, application=application, initial_inputs=pending)
        return code, command_loop
    finally:
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        if not application.done():
            tui.exit()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await application
        finally:
            restore_input()
            if command_loop is not None:
                command_loop.presentation.close_background_output()
