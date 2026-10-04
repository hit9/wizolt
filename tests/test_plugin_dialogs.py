"""Plugin UI uses the same real Application and pipe input as built-in modals."""

import asyncio
from dataclasses import asdict

import pytest
from tui_harness import request_input_from_driver, run_interactive_tui, session, wait_until

from wizolt.sdk.views import Action, Choice, Document, Field, Form, Selection, View
from wizolt.ui.cli.plugin_dialogs import PluginDialogs
from wizolt.ui.cli.presentation import Presentation
from wizolt.ui.tui import TuiApp
from wizolt.ui.tui.plugin_views import DialogState


@pytest.mark.parametrize("view, keys, expected", [
    (View("Name", Form((Field("name", "Name", "start"),))), "!\r", {"action": "submit", "selected": (), "values": {"name": "start!"}}),
    (View("Confirm", Selection((Choice("no", "Cancel"), Choice("yes", "Confirm")))), "\r", {"action": "submit", "selected": ("no",), "values": {}}),
    (View("Pick", Selection((Choice("a", "First"), Choice("b", "Second")))), "j\r", {"action": "submit", "selected": ("b",), "values": {}}),
    (View("Pick", Selection((Choice("a", "First"), Choice("b", "Second")), multiple=True)), " j \r", {"action": "submit", "selected": ("a", "b"), "values": {}}),
    (View("Cancel", Form((Field("name", "Name"),))), "\x1b", None),
    (View("Log", Document("hello"), fullscreen=True), "\x1b", None),
])
def test_dialog_input_selection_and_draft_restoration(tmp_path, monkeypatch, view, keys, expected):
    app = TuiApp()
    app.input_buffer.text = "unsent draft"
    presentation = Presentation(session(tmp_path), output_fn=lambda _: None)
    dialogs = PluginDialogs(app, presentation)
    results = []

    def drive(pipe):
        wait_until(lambda: app.app is not None and app.app.is_running)
        result = asyncio.run_coroutine_threadsafe(dialogs.call("helper", "ui.views.show", {"id": "one", "view": asdict(view)}), app.app.loop)
        wait_until(lambda: app.modal is not None)
        pipe.send_text(keys)
        results.append(result.result(timeout=5)["result"])
        assert app.modal is None
        assert app.input_buffer.text == "unsent draft"
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)
    assert results == [expected]
    assert not dialogs.dialogs


def test_live_update_and_external_cancel_restore_focus(tmp_path, monkeypatch):
    app = TuiApp()
    dialogs = PluginDialogs(app, Presentation(session(tmp_path), output_fn=lambda _: None))

    def drive(pipe):
        wait_until(lambda: app.app is not None and app.app.is_running)
        result = asyncio.run_coroutine_threadsafe(dialogs.call("helper", "ui.views.show", {"id": "one", "view": asdict(View("Log", Document("old")))}), app.app.loop)
        wait_until(lambda: app.modal is not None)
        update = asyncio.run_coroutine_threadsafe(dialogs.call("helper", "ui.views.update", {"id": "one", "view": asdict(View("Log", Document("new")))}), app.app.loop)
        update.result(timeout=5)
        assert "new" in "".join(text for _, text in app.modal_fragments())
        result.cancel()
        wait_until(lambda: app.modal is None and not dialogs.dialogs)
        assert app.app.layout.current_window is app.input_window
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_form_does_not_capture_letters_as_action_keys(tmp_path):
    state = DialogState(View("Name", Form((Field("name", "Name", required=True),)), (Action("delete", "Delete", "x"),)), lambda: (40, 12), Presentation(session(tmp_path)).ui)
    state.key("any", "x")
    result = state.key("enter")
    assert result.values == {"name": "x"}


def test_short_terminal_keeps_form_cursor_and_actions_visible(tmp_path):
    state = DialogState(View("Name", Form((Field("name", "Name", "line\n" * 40, multiline=True),))), lambda: (25, 8), Presentation(session(tmp_path)).ui)
    fragments = state.fragments()
    assert any("reverse" in style for style, _ in fragments)
    assert "Esc back" in "".join(text for _, text in fragments)
    assert "".join(text for _, text in fragments).count("\n") <= 8


def test_queued_view_update_installs_current_action_keys_when_it_opens(tmp_path, monkeypatch):
    app = TuiApp()
    dialogs = PluginDialogs(app, Presentation(session(tmp_path)))

    def drive(pipe):
        wait_until(lambda: app.app is not None and app.app.is_running)
        approval = request_input_from_driver(app)
        wait_until(lambda: app.input_mode == "approval")
        view = View("Waiting", Document("body"), (Action("old", "Old", "Ctrl-Y"),))
        result = asyncio.run_coroutine_threadsafe(dialogs.call("one", "ui.views.show", {"id": "one", "view": asdict(view)}), app.app.loop)
        wait_until(lambda: bool(app.view_bindings.bindings))
        changed = View("Updated", Document("body"), (Action("new", "New", "Ctrl-X"),))
        asyncio.run_coroutine_threadsafe(dialogs.call("one", "ui.views.update", {"id": "one", "view": asdict(changed)}), app.app.loop).result(timeout=5)
        pipe.send_text("n\r")
        assert approval.result(timeout=5) == "n"
        wait_until(lambda: app.modal is not None)
        pipe.send_text("\x18")
        assert result.result(timeout=5)["result"]["action"] == "new"
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_plugin_view_waits_for_approval_and_cancelled_queue_never_opens(tmp_path, monkeypatch):
    app = TuiApp()
    dialogs = PluginDialogs(app, Presentation(session(tmp_path)))

    def drive(pipe):
        wait_until(lambda: app.app is not None and app.app.is_running)
        approval = request_input_from_driver(app)
        wait_until(lambda: app.input_mode == "approval")
        view = asdict(View("Must wait", Document("body")))
        first = asyncio.run_coroutine_threadsafe(dialogs.call("one", "ui.views.show", {"id": "first", "view": view}), app.app.loop)
        wait_until(lambda: bool(dialogs.dialogs))
        second = asyncio.run_coroutine_threadsafe(dialogs.call("two", "ui.views.show", {"id": "second", "view": view}), app.app.loop)
        wait_until(lambda: len(dialogs.dialogs) == 2)
        assert app.modal is None
        second.cancel()
        wait_until(lambda: len(dialogs.dialogs) == 1)
        pipe.send_text("n\r")
        assert approval.result(timeout=5) == "n"
        wait_until(lambda: app.modal is not None)
        pipe.send_text("\x1b")
        assert first.result(timeout=5) == {"result": None}
        assert not dialogs.dialogs and app.modal is None
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


async def test_background_agent_requests_attention_without_stealing_frontend(tmp_path):
    app = TuiApp()
    app.managed = True
    attention = asyncio.Event()
    app.on_attention = attention.set
    dialogs = PluginDialogs(app, Presentation(session(tmp_path)))
    task = asyncio.create_task(dialogs.call("helper", "ui.views.show", {"id": "background", "view": asdict(View("Name", Form((Field("name", "Name"),))))}))
    await asyncio.wait_for(attention.wait(), 3)
    assert app.modal is None and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not dialogs.dialogs
