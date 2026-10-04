"""Persistent shortcuts use host admission, never arbitrary plugin key interception."""

import asyncio
from dataclasses import asdict

import pytest
from agent_harness import session_with_provider
from tui_harness import run_interactive_tui, wait_until

from wizolt.plugins.session import SessionPlugins
from wizolt.sdk import PluginError
from wizolt.sdk.views import Action, Document, View
from wizolt.ui.cli.plugin_dialogs import PluginDialogs
from wizolt.ui.cli.plugin_keys import PluginKeys
from wizolt.ui.cli.presentation import Presentation
from wizolt.ui.tui import TuiApp
from wizolt.ui.tui.app import InputMode


async def test_global_bindings_persist_conflicts_are_explicit_and_disable_is_dormant(tmp_path):
    session = session_with_provider(tmp_path)
    runtime = SessionPlugins(session)
    path = tmp_path / "sample.py"
    path.write_text('''SDK_VERSION = 1
def setup(p):
    async def show(ctx, args): return "done"
    p.command("sample-view", "Open the sample", show)
''')
    tui = TuiApp()
    keys = PluginKeys(runtime, tui, lambda _: None)
    try:
        await runtime.manage("enable", str(path))
        result = await keys.call("ui.shortcuts.bind", {"key": "f6", "command": "sample.sample-view", "replace": False})
        assert result["bindings"] == [{"key": "f6", "command": "sample.sample-view", "active": True}]
        assert PluginKeys(runtime, TuiApp(), lambda _: None).saved == keys.saved
        installations, problems = runtime.catalog.read()
        assert not problems
        assert installations["sample"].enabled
        with pytest.raises(PluginError, match="already bound"):
            await keys.call("ui.shortcuts.bind", {"key": "Ctrl-L", "command": "sample.sample-view", "replace": False})
        await keys.call("ui.shortcuts.bind", {"key": "Ctrl-L", "command": "sample.sample-view", "replace": True})
        assert keys.enabled("sample.sample-view")
        tui.input_mode = InputMode.APPROVAL
        assert not keys.enabled("sample.sample-view")
        tui.input_mode = InputMode.RUNNING
        assert not keys.enabled("sample.sample-view")
        await runtime.manage("disable", "sample")
        assert all(not item["active"] for item in keys.listing()["bindings"])
        assert keys.saved["f6"] == "sample.sample-view"
        await runtime.manage("enable", "sample")
        assert all(item["active"] for item in keys.listing()["bindings"])
    finally:
        await runtime.close()


@pytest.mark.parametrize("key", ["escape", "enter", "Ctrl-C", "c-d", "c-\\", "Ctrl-S", "x", "unknown"])
def test_protected_or_text_keys_cannot_be_global_shortcuts(key):
    with pytest.raises(PluginError):
        PluginKeys.check(key)


def test_view_local_modified_key_closes_only_its_view(tmp_path, monkeypatch):
    session = session_with_provider(tmp_path)
    app = TuiApp()
    dialogs = PluginDialogs(app, Presentation(session))
    view = View("Actions", Document("hello"), (Action("inspect", "Inspect", "Ctrl-X"),))

    def drive(pipe):
        wait_until(lambda: app.app is not None and app.app.is_running)
        result = asyncio.run_coroutine_threadsafe(dialogs.call("sample", "ui.views.show", {"id": "one", "view": asdict(view)}), app.app.loop)
        wait_until(lambda: app.modal is not None)
        pipe.send_text("\x18")
        assert result.result(timeout=5)["result"]["action"] == "inspect"
        assert app.modal is None
        assert not app.view_bindings.bindings
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)
