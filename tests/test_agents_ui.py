"""Selection changes the displayed agent; accepted inputs retain their original target."""

import asyncio
import sys

import pytest
from prompt_toolkit.document import Document
from tui_harness import loop, session

from wizolt.agent.lifecycle import close_agent_resources
from wizolt.agent.engine import Agent
from wizolt.model.client import ModelClient
from wizolt.session import SessionSnapshotStore
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli import agents as agents_module
from wizolt.ui.cli.agents import AgentsFrontend, agents_command
from wizolt.ui.cli.commands import COMMAND_LOOKUP, status
from wizolt.ui.cli.runtime import TuiRuntime
from wizolt.ui.tui import InputMode


def test_first_input_history_survives_startup_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    s = session(tmp_path)
    command_loop = CommandLoop(Agent(s, output_fn=lambda _: None), output_fn=lambda _: None)
    assert command_loop.input_history is not None
    assert not s.messages
    SessionSnapshotStore.clean_expired(s.config.data_dir, s.uid, 1)
    command_loop.input_history.append_string("/agents")
    assert list(command_loop.input_history.load_history_strings()) == ["/agents"]


async def test_headless_agents_lists_main_and_handles_stop_all(tmp_path, monkeypatch):
    command_loop = loop(tmp_path)
    assert await agents_command(command_loop, "") == "main: idle"
    assert await agents_command(command_loop, "unknown") == "Usage: /agents [stop-all]"
    stopped = []
    monkeypatch.setattr(command_loop.session.subagents, "stop", stopped.append)
    assert await agents_command(command_loop, "stop-all") == "Stopping all agents"
    assert stopped == [command_loop.session.uid]


@pytest.fixture
async def frontend(tmp_path, monkeypatch):
    command_loop = loop(tmp_path)
    command_loop.interactive_input = True
    root = TuiRuntime(command_loop)
    root.runtime_loop = asyncio.get_running_loop()
    root.shutdown = asyncio.Event()
    root.application_ready = asyncio.Event()
    root.accepting = True
    command_loop.presentation.tui = root.build_tui()
    root.tui.managed = True
    root.submissions_task = root.spawn(root._consume_submissions())
    frontend = AgentsFrontend(root)

    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "child reply"}, [], "child reply"

    monkeypatch.setattr(ModelClient, "request", request)
    yield frontend
    await root._close_submissions()
    await frontend.close()
    await close_agent_resources(root.loop.agent)
    root.loop.session.close()


async def child(frontend, name="child"):
    entry = await frontend.group.spawn(frontend.root.loop.session, name, "child task")
    await asyncio.wait_for(asyncio.shield(entry.task), 3)
    return frontend.runtimes[entry.agent.session.uid]


async def pick(frontend, runtime, monkeypatch):
    async def choice(*args, **kwargs):
        return runtime.loop.session.uid

    monkeypatch.setattr(agents_module, "choice_application", choice)
    await agents_command(frontend.current.loop, "")


async def test_picker_includes_main_without_children(frontend, monkeypatch):
    async def choice(command_loop, title, choices, *, labels, current, preview_fn, disabled, label_fn):
        uid = frontend.root.loop.session.uid
        assert choices == (uid,)
        assert current == uid
        assert disabled == set()
        assert "main" in labels[uid] and "idle" in labels[uid]
        assert preview_fn(uid).startswith("main")
        return uid

    monkeypatch.setattr(agents_module, "choice_application", choice)
    await agents_command(frontend.root.loop, "")
    assert frontend.current is frontend.root
    assert not frontend.switching


async def test_picker_exposes_state_context_and_preview_without_changing_focus(frontend, monkeypatch):
    runtime = await child(frontend)
    runtime.loop.session.usage.add({"prompt_tokens": 320, "completion_tokens": 10}, budget=1000)
    previews = []

    async def choice(command_loop, title, choices, *, labels, current, preview_fn, disabled, label_fn):
        assert title == "Agents"
        assert "main" in labels[frontend.root.loop.session.uid]
        assert "completed" in labels[runtime.loop.session.uid]
        assert "ctx 32%" in labels[runtime.loop.session.uid]
        runtime.loop.session.state.awaiting_input = True
        live = "".join(text for _, text in label_fn(runtime.loop.session.uid))
        assert "waiting for input" in live
        runtime.loop.session.state.awaiting_input = False
        previews.append(preview_fn(runtime.loop.session.uid))
        assert frontend.current is frontend.root  # moving the cursor only previews.
        return None

    monkeypatch.setattr(agents_module, "choice_application", choice)
    await agents_command(frontend.root.loop, "")
    assert "child task" in previews[0]
    assert "child reply" in previews[0]
    assert frontend.current is frontend.root
    assert COMMAND_LOOKUP["/agents"].queue_safe
    assert "/worker" not in COMMAND_LOOKUP


async def test_selected_agent_owns_draft_model_and_statistics(frontend, monkeypatch):
    runtime = await child(frontend, "editor")
    root = frontend.root
    root.tui.input_buffer.document = Document("main draft")
    runtime.tui.input_buffer.document = Document("child draft")
    root.loop.session.config.provider.model = "main-model"
    runtime.loop.session.config.provider.model = "child-model"
    root.loop.session.usage.add({"prompt_tokens": 90, "completion_tokens": 10}, budget=1000)
    runtime.loop.session.usage.add({"prompt_tokens": 640, "completion_tokens": 10}, budget=1000)
    await pick(frontend, runtime, monkeypatch)
    assert frontend.current is runtime
    assert frontend.current.tui.input_buffer.text == "child draft"
    values = frontend.current.loop.presentation.status_bar.values()
    assert values["model"] == "child-model"
    assert values["context.percent"] == 64
    bar = "".join(text for _, text in runtime.loop.presentation.status_bar.fragments())
    assert "editor" in bar and "child-model" in bar
    report = status(frontend.current.loop, "")
    assert "editor" in report and "child-model" in report
    assert "main-model" not in report and "worker" not in report
    assert "64%" in report
    await pick(frontend, root, monkeypatch)
    assert frontend.current.tui.input_buffer.text == "main draft"
    assert root.loop.presentation.status_bar.values()["context.percent"] == 9


async def test_preview_contains_the_task_before_the_first_answer(frontend, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(client, messages, tools=None):
        entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await frontend.group.spawn(frontend.root.loop.session, "reader", "inspect the pending work")
    await asyncio.wait_for(entered.wait(), 3)

    async def choice(*args, **kwargs):
        assert "inspect the pending work" in kwargs["preview_fn"](entry.agent.session.uid)
        return None

    monkeypatch.setattr(agents_module, "choice_application", choice)
    await frontend.select(frontend.root.loop)
    release.set()
    if entry.task is not None:
        await asyncio.wait_for(asyncio.shield(entry.task), 3)


async def test_input_accepted_before_switch_stays_with_original_agent(frontend, monkeypatch):
    runtime = await child(frontend)
    runtime.submit_chat("original child input")
    await pick(frontend, frontend.root, monkeypatch)
    await runtime.submissions.join()
    task = frontend.group.entry(runtime.loop.session.uid).task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 3)
    assert any(message.get("content") == "original child input" for message in runtime.loop.session.messages)
    assert all(message.get("content") != "original child input" for message in frontend.root.loop.session.messages)


async def test_inactive_output_is_retained_without_writing_to_the_terminal(frontend, capsys):
    runtime = await child(frontend)
    runtime.tui.record_scrollback("only in child\n")
    assert "only in child" not in capsys.readouterr().out
    assert runtime.tui.scrollback.pending
    assert frontend.root.tui.scrollback.transcript == []


async def test_final_selected_output_drains_after_the_view_stops(frontend, capsys):
    runtime = await child(frontend)
    frontend.current = runtime
    frontend.root.tui.record_scrollback("background main line\n")
    runtime.tui.record_scrollback("last selected line\n")
    runtime.tui._after_run()
    assert "last selected line" not in capsys.readouterr().out
    runtime.tui.finish()
    output = capsys.readouterr().out
    assert "last selected line" in output
    assert "background main line" not in output
    runtime.tui.record_scrollback("late terminal line\n")
    assert "late terminal line" in capsys.readouterr().out


async def test_switch_preserves_pending_approval_and_agent_draft(frontend, monkeypatch):
    runtime = await child(frontend)
    runtime.tui.input_buffer.document = Document("saved child draft")
    approval = asyncio.create_task(runtime.tui.request_input("Approve child edit?"))
    await asyncio.sleep(0)
    assert runtime.tui.input_mode == InputMode.APPROVAL
    await pick(frontend, runtime, monkeypatch)
    runtime.tui._after_run()  # switching suspends a view, rather than granting/refusing its prompt.
    await pick(frontend, frontend.root, monkeypatch)
    assert not approval.done()
    assert frontend.root.tui.input_mode != InputMode.APPROVAL
    await pick(frontend, runtime, monkeypatch)
    runtime.tui.resolve_input("n")
    assert await approval == "n"
    assert runtime.tui.input_buffer.text == "saved child draft"


async def test_current_interrupt_does_not_stop_background_sibling(frontend, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def request(client, messages, tools=None):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    one = await frontend.group.spawn(frontend.root.loop.session, "one", "task one")
    two = await frontend.group.spawn(frontend.root.loop.session, "two", "task two")
    await asyncio.wait_for(entered.wait(), 3)
    selected = frontend.runtimes[one.agent.session.uid]
    await pick(frontend, selected, monkeypatch)
    frontend.current.interrupt()
    if one.task is not None:
        await asyncio.wait_for(asyncio.shield(one.task), 3)
    assert one.status == "interrupted"
    assert two.status == "running"
    release.set()
    if two.task is not None:
        await asyncio.wait_for(asyncio.shield(two.task), 3)
    assert two.status == "completed"
