"""Human management and plugin projections exercise the same host UI as built-in features."""

from types import SimpleNamespace

from prompt_toolkit.data_structures import Size
from tui_harness import ResizableOutput, rendered_screen_text, run_interactive_tui, wait_until

from wizolt.ui.cli.plugins import plugins_command
from wizolt.ui.tui import TuiApp


async def test_manager_keeps_disabled_plugins_and_can_reenable(tmp_path):
    from agent_harness import session_with_provider

    from wizolt.plugins.session import SessionPlugins

    session = session_with_provider(tmp_path)
    session.plugins = SessionPlugins(session)
    path = tmp_path / "sample.py"
    path.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.field("n", lambda ctx: 1)\n')
    await session.plugins.manage("enable", str(path))
    frames = []
    replies = iter(("sample", "disable", "sample", "enable", None))

    class Terminal:
        async def show_modal(self, fragments, handle_key, **kwargs):
            frames.append("".join(text for _, text in fragments()))
            return next(replies)

    loop = SimpleNamespace(session=session, interactive_input=True, presentation=SimpleNamespace(tui=Terminal()))
    assert await plugins_command(loop, "") == ""
    assert "enabled" in frames[0] and "active" in frames[0]
    assert "disabled" in frames[2] and "sample" in frames[2]
    assert "enabled" in frames[4] and "active" in frames[4]
    assert path.exists()
    await session.plugins.close()


def test_input_components_follow_actual_viewport_and_leave_input_usable(monkeypatch):
    app = TuiApp()
    output = ResizableOutput(rows=30, columns=90)
    frames = []
    calls = []

    def additions(columns, rows):
        calls.append((app.app.render_counter, columns, rows))
        return [("", f"PLUGIN {columns}x{rows}")]

    app.above_input_fragments_fn = additions

    def after_render(application):
        frames.append(rendered_screen_text(application, output))

    def drive(pipe):
        wait_until(lambda: any("PLUGIN 90x30" in frame for frame in frames))
        output.size = Size(rows=18, columns=40)
        app.app.loop.call_soon_threadsafe(app.app._on_resize)
        wait_until(lambda: any("PLUGIN 40x18" in frame for frame in frames))
        pipe.send_text("still editable")
        wait_until(lambda: app.input_buffer.text == "still editable")
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)
    assert len(calls) == len(set(calls)), "layout and drawing must share a projection for each frame"
