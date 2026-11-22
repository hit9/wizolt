"""Start the real input application before assembling its session and command loop."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import DynamicStyle, Style

from wizolt.base import run_blocking
from wizolt.image import UserInput
from wizolt.ui.render import Theme
from wizolt.ui.tui import TuiApp
from wizolt.utils import terminal

if TYPE_CHECKING:
    from wizolt.ui.cli import CommandLoop


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

    def received(color: tuple[int, int, int]) -> None:
        terminal.remember_background(color)
        if command_loop is not None:
            command_loop.configure_theme()
            tui.recolor()

    tui.on_terminal_background = received
    tui.on_ready = ready.set
    application = asyncio.create_task(tui.run(style=DynamicStyle(lambda: command_loop.view.style() if command_loop else style)))
    waiting = asyncio.create_task(ready.wait())

    def assembled(value: CommandLoop) -> None:
        nonlocal command_loop
        command_loop = value

    try:
        await asyncio.wait({waiting, application}, return_when=asyncio.FIRST_COMPLETED)
        if application.done():
            await application
            return 0, None
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
            if command_loop is not None:
                command_loop.presentation.close_background_output()
