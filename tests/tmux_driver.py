"""A real TuiApp under a real terminal, for the tmux acceptance test.

Run as a script inside a tmux pane by `test_tui_tmux_scrollback.py`. It drives the production
path -- `TuiApp.write_to_scrollback`, `UiPrinter.emit`, `ScrollbackRegion` -- rather than a
stand-in, because the artifacts this test exists to catch only appear when real escape
sequences meet a real multiplexer.

Usage: tmux_driver.py <total> <interval> [log-path]

Emits `MARKER-nnnn` lines on a timer. Every marker written is appended to the log, so the test
can tell a line that was never emitted (merely delayed) from one the terminal destroyed after
the fact -- the two have completely different causes.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wizolt.cli.modals import choice_application
from wizolt.render import UiPrinter
from wizolt.tui.app import TuiApp


async def main(log) -> None:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05

    ui = UiPrinter()
    app = TuiApp(status_fragments_fn=lambda: [("", " tmux-driver ")], on_app_stop=ui.drain_scrollback)
    # The same wiring `TuiRuntime.build_tui` does. Without it the printer takes the no-sink path
    # and this driver would quietly measure the old implementation instead of the new one.
    ui.transcript_sink = app.record_scrollback

    async def long_selector_loop() -> None:
        ui.emit("WIZOLT-BANNER")
        ui.emit("PROVIDER-COMMAND")
        for cycle in range(3):
            while not Path(log.name).with_suffix(f".open-{cycle}").exists():
                await asyncio.sleep(0.02)
            result = await choice_application(
                SimpleNamespace(tui=app), "Provider", tuple(f"provider-{i:02d}" for i in range(80)), {}, "", set()
            )
            log.write(f"closed {cycle}: {result}\n")
            log.flush()
        await asyncio.Event().wait()

    async def selector_loop() -> None:
        """Open and close a selector repeatedly, the way `/effort` is used mid-session.

        A selector makes the app several rows taller and then shorter again, moving the boundary
        the scroll region is computed from. Doing it more than once matters: the artifacts this
        exercises only showed up on the second and later cycles.
        """
        options = [("", f" option {index}\n") for index in range(8)]
        while True:
            await asyncio.sleep(1.7)
            # Tests request a quiescent chat frame only after exercising resizes and selectors.
            # A quiet 150ms capture can also be an open selector; explicitly acknowledge that
            # this loop has closed its last modal and will not open another one.
            if log is not None and Path(log.name).with_suffix(".settle").exists():
                log.write("selectors stopped\n")
                log.flush()
                return
            opened = asyncio.get_running_loop().create_task(app.show_modal(lambda: options, lambda _key, _data="": None))
            await asyncio.sleep(0.9)
            app.close_modal(None)
            await opened
            if log is not None:
                log.write("selector cycle\n")
                log.flush()

    async def emit_loop() -> None:
        for n in range(1, total + 1):
            await asyncio.sleep(interval)
            line = f"MARKER-{n:04d} " + "x" * 30
            await app.write_to_scrollback(lambda line=line: ui.emit(line))
            if log is not None:
                log.write(f"wrote {line.split()[0]}\n")
                log.flush()
        if len(sys.argv) > 4 and sys.argv[4] == "rules":
            with ui.batched():
                ui.emit("USER-BEGIN " + "u" * 70 + " USER-END")
                ui.emit_phase_rule()
                ui.emit("ANSWER-BEGIN " + "a" * 70 + " ANSWER-END")
                ui.emit_turn_end(time.monotonic() - 65)
                ui.emit_worker_rule("[worker] 完成")
            if log is not None:
                log.write("rules complete\n")
                log.flush()
        if len(sys.argv) > 4 and sys.argv[4] == "exit":
            app.record_scrollback("EXIT-FIRST\n")
            ui.emit("EXIT-SECOND")
            app.app.exit()
            return
        # Stay up after the last line. A test that wants to resize against a fixed transcript
        # needs the app still running, and the session is torn down by the test either way.
        await asyncio.Event().wait()

    def start() -> None:
        if len(sys.argv) > 4 and sys.argv[4] == "choices":
            app.app.create_background_task(long_selector_loop())
            return
        app.app.create_background_task(emit_loop())
        app.app.create_background_task(selector_loop())

    app.on_ready = start
    await app.run()
    if log is not None:
        log.write("driver exited\n")
        log.flush()


if __name__ == "__main__":
    with contextlib.ExitStack() as stack:
        write_log = stack.enter_context(open(sys.argv[3], "w")) if len(sys.argv) > 3 else None
        asyncio.run(main(write_log))
