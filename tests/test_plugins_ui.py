"""Human management and plugin projections exercise the same host UI as built-in features."""

from types import SimpleNamespace

from prompt_toolkit.data_structures import Size
from tui_harness import ResizableOutput, rendered_screen_text, run_interactive_tui, wait_until

from wizolt.ui.cli.plugins import plugins_command
from wizolt.ui.tui import TuiApp


def test_plugin_columns_align_and_long_names_leave_space_for_states():
    from wizolt.ui.cli.plugins import PluginManager

    manager = PluginManager(None, None)
    manager.records = {
        "pet": {"builtin": True, "enabled": False, "status": "off"},
        "very_long_plugin_name_that_would_push_states_off_screen": {"builtin": False, "enabled": True, "status": "running"},
    }
    for width in (35, 50, 80):
        labels = list(manager.labels(width).values())
        assert labels[0].index("builtin") == labels[1].index("user")
        assert labels[0].index("disabled") == labels[1].index("enabled")
        assert labels[0].index("off") == labels[1].index("running")
        assert all(len(label) <= width for label in labels)
        assert "..." in labels[1]


def test_manager_preview_names_presets_and_keeps_the_file_name_visible(monkeypatch):
    import os

    from wizolt.ui.cli.plugins import PluginManager

    monkeypatch.setattr("shutil.get_terminal_size", lambda *_: os.terminal_size((40, 24)))
    manager = PluginManager(None, SimpleNamespace(components=lambda: ()))
    deep = "/very/long/directory/that/does/not/fit/in/a/narrow/terminal/gitline.py"
    manager.records = {
        "gitline": {"path": deep, "presets": {"statusbar": ["git"], "divider": []}},
        "plain": {"path": "/p/plain.py", "presets": {"statusbar": [], "divider": []}},
    }
    lines = manager.facts("gitline", 34)
    # The kinds dict is always complete; listing its keys named presets a plugin never registered.
    assert "Presets: statusbar git" in lines and not any("divider" in line for line in lines)
    assert not any("Presets" in line for line in manager.facts("plain", 34))
    assert lines[0].endswith("gitline.py") and lines[0].startswith("...") and len(lines[0]) <= 34
    # An unreadable source still gets a page that says why, above the same facts.
    preview = "".join(text for _, text in manager.preview("gitline"))
    assert "Could not read this plugin's" in preview and "Presets: statusbar git" in preview


async def test_statusbar_plugin_count_tracks_only_this_agents_live_generations(tmp_path):
    from agent_harness import session_with_provider

    from wizolt.plugins.runtime import PluginRuntime
    from wizolt.ui.render import StatusBar

    session = session_with_provider(tmp_path)
    bar = StatusBar(session)
    other = PluginRuntime(session.plugins.snapshot)
    path = tmp_path / "counted.py"
    path.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.field("value", lambda ctx: 1)\n')
    try:
        assert bar.values()["plugins.count"] == 0  # Installed pet remains disabled.
        await other.manage("enable", str(path))
        assert other.active_count == 1 and bar.values()["plugins.count"] == 0
        await session.plugins.manage("enable", str(path))
        assert bar.values()["plugins.count"] == 1
        await session.plugins.start_turn()
        await session.plugins.manage("disable", "counted")
        assert bar.values()["plugins.count"] == 1  # Deferred disable still has live callbacks.
        await session.plugins.finish_turn()
        assert bar.values()["plugins.count"] == 0
        await session.plugins.manage("enable", str(path))
        await session.plugins.entries["counted"].active.worker.close()
        assert bar.values()["plugins.count"] == 0
    finally:
        await other.close()
        await session.plugins.close()
        session.close()


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
    assert "enabled" in frames[0] and "running" in frames[0]
    assert "builtin" in frames[0] and "user" in frames[0]
    assert "disabled" in frames[2] and "sample" in frames[2]
    assert "enabled" in frames[4] and "running" in frames[4]
    assert path.exists()
    await session.plugins.close()


def test_input_components_follow_actual_viewport_and_leave_input_usable(monkeypatch):
    app = TuiApp()
    output = ResizableOutput(rows=30, columns=90)
    frames = []
    calls = []

    def additions(columns, rows):
        calls.append((app.app.render_counter, columns, rows))
        return {"above_divider": [("", "BEFORE DIVIDER")], "above_input": [("", f"PLUGIN {columns}x{rows}")], "below_input": [("", "AFTER INPUT")]}

    app.extension_fragments_fn = additions
    app.idle_divider_fragments_fn = lambda: [("", "DIVIDER ROW")]

    def after_render(application):
        frames.append(rendered_screen_text(application, output))

    def drive(pipe):
        wait_until(lambda: any("PLUGIN 90x30" in frame for frame in frames))
        output.size = Size(rows=18, columns=40)
        app.app.loop.call_soon_threadsafe(app.app._on_resize)
        wait_until(lambda: any("PLUGIN 40x18" in frame for frame in frames))
        pipe.send_text("still editable")
        wait_until(lambda: app.input_buffer.text == "still editable")
        wait_until(lambda: any("still editable" in frame for frame in frames))
        frame = next(frame for frame in reversed(frames) if "still editable" in frame)
        assert frame.index("BEFORE DIVIDER") < frame.index("DIVIDER ROW") < frame.index("PLUGIN") < frame.index("still editable") < frame.index("AFTER INPUT")
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)
    assert len(calls) == len(set(calls)), "layout and drawing must share a projection for each frame"
