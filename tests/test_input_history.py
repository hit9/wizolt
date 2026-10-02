"""Keyboard recall spans main sessions while child inputs remain private."""

import sys

import pytest
from prompt_toolkit.history import FileHistory
from tui_harness import run_interactive_tui, wait_until

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features, close_agent_resources
from wizolt.config import Config, ProviderConfig
from wizolt.model.client import ModelClient
from wizolt.session import Session
from wizolt.ui.cli import CommandLoop, TuiRuntime


def main_loop(tmp_path, project):
    cwd = tmp_path / project
    cwd.mkdir(exist_ok=True)
    config = Config(data_dir=str(tmp_path / "data"))
    config.providers = {"default": ProviderConfig(model="gpt-4", url="http://test", key="sk-test")}
    session = Session(cwd=str(cwd), config=config)
    bootstrap_features(session)
    return CommandLoop(Agent(session, output_fn=lambda _: None), output_fn=lambda _: None)


@pytest.mark.parametrize("project", ["one", "two"])
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("key", ["\x10", "\x1b[A"], ids=["ctrl-p", "up"])
def test_main_recall_reads_previous_sessions_without_inheriting_context(tmp_path, monkeypatch, project, running, key):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    previous = main_loop(tmp_path, "one")
    previous.input_history.append_string("input from the previous main session")
    previous.session.messages.append({"role": "user", "content": "previous context"})
    previous.session.close()
    current = main_loop(tmp_path, project)
    assert previous.session.uid != current.session.uid
    app = current.presentation.tui = TuiRuntime(current).build_tui()
    if running:
        app.set_running("working")

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        wait_until(lambda: app.input_buffer._load_history_task is not None and app.input_buffer._load_history_task.done())
        pipe_input.send_text(key)
        wait_until(lambda: app.input_buffer.text == "input from the previous main session")
        app.app.loop.call_soon_threadsafe(app.app.exit)

    try:
        run_interactive_tui(monkeypatch, app, drive=drive)
        assert current.session.messages == []
        assert current.session.pending_user_inputs == []  # Recall edits a draft; it does not submit.
    finally:
        current.session.close()


async def test_children_never_read_or_append_main_or_sibling_keyboard_history(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    root = main_loop(tmp_path, "one")
    root.input_history.append_string("main history")

    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    group = root.session.subagents
    try:
        entries = [await group.spawn(root.session, name, "independent task") for name in ("api-review", "ui-review")]
        for entry in entries:
            await group.wait([entry.agent.session.uid], 3)
        children = [CommandLoop(entry.agent, output_fn=lambda _: None) for entry in entries]
        assert all(list(child.input_history.load_history_strings()) == [] for child in children)
        children[0].input_history.append_string("private child input")
        reopened = CommandLoop(entries[0].agent, output_fn=lambda _: None)
        assert list(reopened.input_history.load_history_strings()) == ["private child input"]
        assert list(children[1].input_history.load_history_strings()) == []
        assert list(FileHistory(root.input_history.filename).load_history_strings()) == ["main history"]
        other_main = main_loop(tmp_path, "two")
        assert list(other_main.input_history.load_history_strings()) == ["main history"]
        assert other_main.session.messages == []
        other_main.session.close()
    finally:
        await close_agent_resources(root.agent)
        root.session.close()
