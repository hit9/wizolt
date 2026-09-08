"""The scroll-region projection under the states the tmux acceptance run does not reach.

`test_tui_tmux_scrollback.py` proves the mechanism against a real multiplexer, but it only
drives streaming output. The cases here are the ones where the geometry the projection depends
on changes underneath it -- a selector making the app taller, an exclusive viewer taking the
alternate screen, an app that fills the pane -- and each of them has a way to fail that looks
nothing like the artifacts the tmux run measures.

They run the real `TuiApp` against the terminal model from `test_tui_resize.py`, which
interprets the escape sequences the projection actually emits, so a rebuild that never happened
cannot pass as one that did.
"""

from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.data_structures import Size
from test_tui_resize import ReflowingTerminal
from tui_harness import run_interactive_tui, wait_for, wait_until

from wizolt.render import UiPrinter
from wizolt.tui.app import TuiApp
from wizolt.tui.scrollback import ScrollbackRegion, app_top_row

ROWS = 30


@pytest.fixture
def wired():
    """A TuiApp and a UiPrinter wired the way `TuiRuntime.build_tui` wires them."""
    output = ReflowingTerminal(ROWS, 80)
    app = TuiApp()
    printer = UiPrinter()
    printer.color = True
    printer.transcript_sink = app.record_scrollback
    return output, app, printer


def run_tui(monkeypatch, app, output, drive):
    """Run the app against the model, answering CPR the way a real terminal does.

    Without the CPR wiring the renderer never learns its origin, the projection can never place
    a line, and every assertion below would fail for a reason that has nothing to do with it.
    """
    run_interactive_tui(
        monkeypatch,
        app,
        drive=drive,
        output=output,
        on_application=lambda application: setattr(output, "report_cursor_row", application.renderer.report_absolute_cursor_row),
    )


def open_modal(app, *, rows: int, exclusive: bool = False):
    """Open a real modal, the way `/effort` and `/diff` do, rather than poking at layout state."""
    fragments = [("", f"option {index}\n") for index in range(rows)]
    return asyncio.run_coroutine_threadsafe(
        app.show_modal(lambda: fragments, lambda _key, _data="": None, exclusive=exclusive),
        app.app.loop,
    )


def emit_and_wait(app, printer, text):
    """Emit one line through the recorder and wait for it to reach the transcript."""
    app.app.loop.call_soon_threadsafe(printer.emit, text)
    wait_until(lambda: text in "".join(app.scrollback.transcript))


def test_transcript_is_written_above_a_taller_app(monkeypatch, wired):
    """A selector makes the app taller; the transcript must land above the new top, not over it.

    The scroll region is computed from the app's height, so a height that changed between the
    emit and the write is exactly how lines end up printed onto the selector.
    """
    output, app, printer = wired
    seen = []

    def drive(_pipe_input):
        wait_until(lambda: any(line.startswith(UiPrinter.PROMPT_PREFIX) for line in output.lines))
        emit_and_wait(app, printer, "before selector")
        before = app.app.renderer.last_rendered_screen.height
        open_modal(app, rows=8)
        wait_until(lambda: app.app.renderer.last_rendered_screen.height > before)
        emit_and_wait(app, printer, "during selector")
        seen.append(list(output.lines))
        app.app.loop.call_soon_threadsafe(lambda: app.close_modal(None))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_tui(monkeypatch, app, output, drive)

    lines = seen[0]
    prompts = [index for index, line in enumerate(lines) if line.startswith(UiPrinter.PROMPT_PREFIX)]
    assert len(prompts) == 1, "the taller app left a second copy of its prompt on screen"
    written = [index for index, line in enumerate(lines) if "during selector" in line]
    assert written, "the line emitted while the selector was open never reached the screen"
    assert max(written) < prompts[0], "transcript was written into the app's own rows"


def test_width_change_under_an_exclusive_viewer_defers_the_rebuild(monkeypatch, wired):
    """A rebuild must not run while /diff owns the alternate screen, and must not be forgotten.

    Purging there would take the primary screen's scrollback with it to redraw something nobody
    can see, and simply skipping the rebuild would leave the pane reflowed and unattributable
    once the viewer closes.
    """
    output, app, printer = wired
    # Watch when the rebuild runs rather than what is on screen: a full-screen viewer repaints
    # the whole pane anyway, so screen content cannot tell a purge from an ordinary redraw.
    on_alternate_screen = []
    real_rebuild = ScrollbackRegion.rebuild

    def spy(self, application):
        on_alternate_screen.append(application.renderer.full_screen)
        real_rebuild(self, application)

    monkeypatch.setattr(ScrollbackRegion, "rebuild", spy)

    def drive(_pipe_input):
        wait_until(lambda: any(line.startswith(UiPrinter.PROMPT_PREFIX) for line in output.lines))
        emit_and_wait(app, printer, "recorded before diff")

        open_modal(app, rows=8, exclusive=True)
        wait_until(lambda: app.app.renderer.full_screen)

        output.size = Size(rows=ROWS, columns=50)
        app.app.loop.call_soon_threadsafe(app.app._on_resize)
        wait_until(lambda: app.app.renderer._last_size == output.size)

        app.app.loop.call_soon_threadsafe(lambda: app.close_modal(None))
        wait_until(lambda: not app.app.renderer.full_screen)
        wait_until(lambda: on_alternate_screen)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_tui(monkeypatch, app, output, drive)

    assert on_alternate_screen, "the width change was noticed but never rebuilt"
    assert not any(on_alternate_screen), "a rebuild ran while the exclusive viewer owned the screen"
    assert "recorded before diff" in "".join(app.scrollback.transcript)


def test_lines_are_held_rather_than_written_over_an_app_that_fills_the_pane(monkeypatch, wired):
    """With no row above the app there is nowhere safe to write, so the lines must wait.

    Writing anyway would put transcript on top of the prompt; dropping them would lose output.
    """
    output, app, printer = wired
    held = []

    async def shrink_to_app_height():
        # Terminal writes precede publication of last_rendered_screen. Read geometry on the
        # application's loop, between renders, rather than racing it from the driver thread.
        await wait_for(lambda: app.app.renderer.last_rendered_screen is not None)
        output.size = Size(rows=app.app.renderer.last_rendered_screen.height, columns=80)
        app.app._on_resize()

    def drive(_pipe_input):
        wait_until(lambda: any(line.startswith(UiPrinter.PROMPT_PREFIX) for line in output.lines))
        # A pane exactly as tall as the app leaves no region above it.
        asyncio.run_coroutine_threadsafe(shrink_to_app_height(), app.app.loop).result(timeout=10)
        wait_until(lambda: app.app.renderer._last_size == output.size)

        app.app.loop.call_soon_threadsafe(printer.emit, "held while cramped")
        wait_until(lambda: app.scrollback.pending)
        held.append(not any("held while cramped" in line for line in output.lines))

        output.size = Size(rows=ROWS, columns=80)
        app.app.loop.call_soon_threadsafe(app.app._on_resize)
        wait_until(lambda: not app.scrollback.pending)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_tui(monkeypatch, app, output, drive)

    assert held == [True], "the line was written over the app instead of being held"
    assert "held while cramped" in "".join(app.scrollback.transcript), "the held line was dropped"


def test_the_app_stays_flush_with_the_pane_bottom(monkeypatch, wired):
    """The region boundary is the app's top row, which is only meaningful if the app reaches the
    pane bottom. prompt-toolkit guarantees that by treating its available height as a floor on the
    rendered height; if that ever stops holding, the region would be computed from a boundary that
    is not one, and lines would land inside the app.
    """
    output, app, printer = wired
    geometry = []

    def drive(_pipe_input):
        wait_until(lambda: any(line.startswith(UiPrinter.PROMPT_PREFIX) for line in output.lines))
        for index in range(4):
            emit_and_wait(app, printer, f"line {index}")
            renderer = app.app.renderer
            top = app_top_row(renderer)
            geometry.append((top, top + renderer.last_rendered_screen.height, output.size.rows))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_tui(monkeypatch, app, output, drive)

    assert geometry
    for top, bottom, rows in geometry:
        assert top > 0, "the app filled the pane, leaving no region for the transcript"
        assert bottom == rows, f"the app was not flush with the pane bottom: {top}..{bottom} of {rows}"


def test_transcript_stays_on_screen_after_the_app_stops(monkeypatch, wired):
    """Output printed while the runtime unwinds still has to reach the terminal.

    Once the app has stopped there is no render to defer to, so anything still queued would
    simply never be written.
    """
    output, app, printer = wired

    def drive(_pipe_input):
        wait_until(lambda: any(line.startswith(UiPrinter.PROMPT_PREFIX) for line in output.lines))
        emit_and_wait(app, printer, "before exit")
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_tui(monkeypatch, app, output, drive)

    printer.emit("after exit")
    recorded = "".join(app.scrollback.transcript)
    assert "before exit" in recorded
    assert "after exit" in recorded, "output printed after the app stopped was not recorded"


@pytest.mark.parametrize("final_write", [False, True])
def test_accepted_output_survives_exit_before_the_next_render(monkeypatch, wired, capsys, final_write):
    output, app, _printer = wired
    if final_write:
        app.on_app_stop = lambda: app.record_scrollback("later shutdown output\n")

    def drive(_pipe_input):
        wait_until(lambda: any(line.startswith(UiPrinter.PROMPT_PREFIX) for line in output.lines))

        def emit_then_exit():
            app.record_scrollback("accepted just before exit\n")
            app.app.exit()

        app.app.loop.call_soon_threadsafe(emit_then_exit)

    run_tui(monkeypatch, app, output, drive)

    assert "accepted just before exit" in "".join(app.scrollback.transcript)
    assert not app.scrollback.pending
    recorded = "".join(app.scrollback.transcript)
    printed = capsys.readouterr().out
    assert "accepted just before exit" in printed or any("accepted just before exit" in line for line in output.lines)
    if final_write:
        assert recorded.index("accepted just before exit") < recorded.index("later shutdown output")
        assert printed.index("accepted just before exit") < printed.index("later shutdown output")


def test_a_rebuild_leaves_the_app_on_the_row_it_was_on(monkeypatch, wired):
    """A width change must not move the session up the pane.

    The rebuild replays from the top, so with a transcript shorter than the space above the app
    the app would land wherever the transcript happens to end -- rising a little further on every
    resize, which reads as the whole session creeping toward the top. It opens the gap above the
    transcript instead, so the app keeps its row.
    """
    output, app, printer = wired
    rows = []

    def drive(_pipe_input):
        wait_until(lambda: any(line.startswith(UiPrinter.PROMPT_PREFIX) for line in output.lines))
        emit_and_wait(app, printer, "one short line")
        rows.append(app_top_row(app.app.renderer))
        for columns in (60, 90, 70):
            output.size = Size(rows=ROWS, columns=columns)
            app.app.loop.call_soon_threadsafe(app.app._on_resize)
            wait_until(lambda columns=columns: app.app.renderer._last_size.columns == columns)
            wait_until(lambda: not app.scrollback.pending)
            rows.append(app_top_row(app.app.renderer))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_tui(monkeypatch, app, output, drive)

    assert len(set(rows)) == 1, f"the app moved between width changes: {rows}"


# The rebuild's switch from counting newlines to counting rows has no test here on purpose. With
# the anchoring above in place, a short transcript is padded to the app's row either way, so the
# two counts agree wherever the app can be placed at all. `physical_rows` is pinned directly, at
# widths measured against tmux, in `test_tui_transcript_recording.py`.
