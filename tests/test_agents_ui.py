"""Selection changes the displayed agent; accepted inputs retain their original target."""

import asyncio
import sys

import pytest
from prompt_toolkit.document import Document
from test_command_ui import ModalHarness
from tui_harness import loop, session

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import close_agent_resources
from wizolt.config import ProviderConfig
from wizolt.model.client import ModelClient
from wizolt.session import Session, SessionSnapshotStore
from wizolt.tools import SubagentTool
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli import agents as agents_module
from wizolt.ui.cli.agents import AgentPreview, AgentsFrontend, agents_command
from wizolt.ui.cli.commands import COMMAND_LOOKUP, set_value, status
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


async def test_main_attention_is_reported_in_selected_child(frontend, monkeypatch):
    runtime = await child(frontend)
    frontend.current = runtime
    notices = []
    monkeypatch.setattr(runtime.loop.presentation, "agent_notice", lambda *args: notices.append(args))
    frontend.root.tui.on_attention()
    assert notices == [("agent [main]", "waiting for input")]


@pytest.mark.parametrize("theme", ["forest", "paper"])
@pytest.mark.parametrize("status,role,label", [
    ("waiting for input", "warning", "! subagent [child] needs input"),
    ("completed", "success", "✓ subagent [child] completed"),
    ("failed", "error", "! subagent [child] failed"),
])
async def test_background_agent_notices_use_selected_theme(frontend, monkeypatch, theme, status, role, label):
    from wizolt.ui.render import Theme

    runtime = await child(frontend)
    monkeypatch.setattr(Theme, "_mode", theme)
    blocks = []
    presentation = frontend.root.loop.presentation
    monkeypatch.setattr(presentation, "emit", lambda block, indent=0: blocks.append(block))
    frontend.notice(runtime.loop.agent, status)
    parts = presentation.ui.log_segments(blocks[0])
    style = next(style for style, text in parts if label in text)
    assert Theme.fg(role) in style
    if role != "error":
        assert "bold" in style
    text = "".join(text for _, text in parts)
    assert ("/agents" in text) == (status == "waiting for input")
    assert not frontend.root.loop.session.messages


async def test_child_frontend_failure_reaches_group_without_affecting_sibling(frontend, monkeypatch):
    from wizolt.base import ModelError

    healthy = await child(frontend, "healthy")

    async def request(client, messages, tools=None):
        raise ModelError("provider failed")

    monkeypatch.setattr(ModelClient, "request", request)
    broken = await child(frontend, "broken")
    entry = frontend.group.entry(broken.loop.session.uid)
    assert entry.status == "failed" and "provider failed" in entry.error
    assert frontend.group.entry(healthy.loop.session.uid).status == "completed"
    assert broken.tui.input_mode == InputMode.CHAT


async def test_exiting_child_prints_root_resume_uid(frontend, monkeypatch):
    runtime = await child(frontend)
    output = []
    monkeypatch.setattr(runtime.loop.presentation, "emit", output.append)
    handled, exit_now = await runtime.loop.command("exit")
    assert (handled, exit_now) == (True, True)
    assert output[-1].endswith(f"wizolt --resume {frontend.root.loop.session.uid}")
    assert runtime.loop.session.uid not in output[-1]


@pytest.mark.parametrize("keys", [["X"], ["x", "up", "enter"]])
async def test_completed_child_can_stop_itself_from_its_picker(frontend, monkeypatch, keys):
    runtime = await child(frontend)
    frontend.current = runtime
    modal = ModalHarness(keys, consumed=True)

    async def show_modal(fragments_fn, key_fn, **kwargs):
        result = modal._drive(fragments_fn, key_fn, **kwargs)
        # Real modals suspend until a key closes them or their owner is cancelled.
        await asyncio.sleep(0)
        return result

    monkeypatch.setattr(runtime.tui, "show_modal", show_modal)
    runtime.tui.set_dispatching()
    runtime.submit_chat("/agents")
    await runtime.submissions.join()
    entry = frontend.group.entry(runtime.loop.session.uid)
    await asyncio.wait_for(asyncio.shield(entry.task), 3)
    assert runtime.tui.input_mode == InputMode.CHAT
    assert runtime.command_task is None
    assert not runtime.cancel_pending
    assert entry.task is None
    runtime.submit_chat("continue")
    await runtime.submissions.join()
    await asyncio.wait_for(asyncio.shield(entry.task), 3)
    assert entry.status == "completed"


async def test_child_archives_its_own_view_and_history_remains_in_picker(frontend, monkeypatch):
    runtime = await child(frontend)
    uid = runtime.loop.session.uid
    frontend.current = runtime
    modal = ModalHarness(["d", "up", "enter"], consumed=True)
    monkeypatch.setattr(runtime.tui, "show_modal", modal.show_modal)
    runtime.submit_chat("/agents")
    await runtime.submissions.join()
    command = frontend.group.entry(uid).task
    if command is not None:
        await asyncio.wait_for(asyncio.gather(command, return_exceptions=True), 3)
    tasks = [task for task in frontend.root.tasks if task.get_name() == "archive-agent"]
    await asyncio.wait_for(asyncio.gather(*tasks), 3)
    assert frontend.current is frontend.root
    assert uid not in frontend.runtimes
    assert uid not in frontend.group.entries
    assert not frontend.root.shutdown.is_set()

    async def choice(_loop, _title, choices, *, label_fn, preview_fn, **kwargs):
        assert choices[-1] == uid
        row = label_fn(uid)
        assert all(style == "class:muted" for style, _ in row)
        assert "archived" in "".join(text for _, text in row)
        assert "child reply" in "".join(text for _, text in preview_fn(uid))
        return uid

    async def viewer(_loop, view, **kwargs):
        assert "child task" in view.text and "child reply" in view.text
        assert ("mode", "read-only") in view.rows

    monkeypatch.setattr(agents_module, "choice_application", choice)
    monkeypatch.setattr(agents_module, "approval_text_viewer", viewer)
    await frontend.select(frontend.root.loop)


async def test_archive_rechecks_selected_view_after_confirmation(frontend):
    runtime = await child(frontend)
    # The user can switch back while archival is waiting for snapshots to drain.
    frontend.current = runtime
    await frontend.group.archive(runtime.loop.session.uid)
    assert frontend.current is frontend.root
    assert frontend.switching
    assert runtime.loop.session.uid not in frontend.runtimes


async def test_failed_archive_reopens_child_input(frontend, monkeypatch):
    runtime = await child(frontend)
    save = Session.save_snapshot

    async def fail(session):
        if session is frontend.root.loop.session:
            raise OSError("disk full")
        return await save(session)

    # Fail before publishing the archive manifest, after frontend admission was drained.
    with monkeypatch.context() as patch:
        patch.setattr(Session, "save_snapshot", fail)
        await frontend.archive_after_command(runtime.loop.session.uid, None)
    assert runtime.accepting
    assert runtime.submissions_task is not None
    runtime.submit_chat("continue")
    await runtime.submissions.join()
    entry = frontend.group.entry(runtime.loop.session.uid)
    await asyncio.wait_for(asyncio.shield(entry.task), 3)
    assert entry.status == "completed"


@pytest.mark.parametrize("message", ["exit", "quit", "/yolo", "/clear", "/src/api.py: fix the 500"])
async def test_model_tasks_are_literal_inputs_not_frontend_commands(frontend, message):
    entry = await frontend.group.spawn(frontend.root.loop.session, "literal", message)
    await asyncio.wait_for(asyncio.shield(entry.task), 3)
    await frontend.group.send(entry.agent.session.uid, message)
    await asyncio.wait_for(asyncio.shield(entry.task), 3)
    assert not frontend.root.shutdown.is_set()
    assert entry.agent.session.settings.yolo == frontend.root.loop.session.settings.yolo
    assert sum(item.get("content") == message for item in entry.agent.session.messages) == 2
    assert entry.agent.session.state.round_count == 2


async def test_user_child_commands_keep_their_origin_across_held_turns_and_snapshots(frontend, monkeypatch):
    runtime = await child(frontend)
    runtime.submit_chat("/yolo")
    await runtime.submissions.join()
    entry = frontend.group.entry(runtime.loop.session.uid)
    if entry.task is not None:
        await asyncio.wait_for(asyncio.shield(entry.task), 3)
    assert runtime.loop.session.settings.yolo != frontend.root.loop.session.settings.yolo
    assert runtime.loop.session.state.round_count == 1
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(client, messages, tools=None):
        entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    await frontend.group.send(entry.agent.session.uid, "continue")
    await asyncio.wait_for(entered.wait(), 3)
    runtime.submit_next_turn("/yolo")
    await runtime.submissions.join()
    queued = runtime.loop.session.pending_user_inputs[0]
    assert queued.commands and queued.next_turn
    assert type(queued).from_json(queued.to_json()).commands
    release.set()
    await asyncio.wait_for(asyncio.shield(entry.task), 3)
    assert runtime.loop.session.settings.yolo == frontend.root.loop.session.settings.yolo
    assert runtime.loop.session.state.round_count == 2


async def test_statusbar_group_counts_are_live_while_usage_remains_selected_agent_local(frontend, monkeypatch):
    waiting = await child(frontend, "waiting")
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(client, messages, tools=None):
        entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    work = await frontend.group.spawn(frontend.root.loop.session, "work", "task")
    await asyncio.wait_for(entered.wait(), 3)
    answer = asyncio.create_task(waiting.loop.agent.tools.await_user(waiting.tui.request_input("Approve?")))
    try:
        await asyncio.sleep(0)
        for index, runtime in enumerate(frontend.runtimes.values(), 1):
            runtime.loop.session.usage.add({"prompt_tokens": index * 100, "completion_tokens": 10}, budget=1000)
            values = runtime.loop.presentation.status_bar.values()
            assert (values["agents.count"], values["agents.running"], values["agents.waiting"]) == (3, 1, 1)
            assert values["agent.name"] == runtime.loop.session.agent_name
            assert values["context.percent"] == index * 10
            assert "3 total · 1 running · 1 waiting for input (group-wide)" in status(runtime.loop, "")
        waiting.tui.resolve_input("n")
        assert await answer == "n"
        assert frontend.root.loop.presentation.status_bar.values()["agents.waiting"] == 0
        release.set()
        await asyncio.wait_for(asyncio.shield(work.task), 3)
        for runtime in frontend.runtimes.values():
            values = runtime.loop.presentation.status_bar.values()
            assert values["agents.count"] == 3 and values["agents.running"] == values["agents.waiting"] == 0
    finally:
        release.set()
        answer.cancel()
        await asyncio.gather(answer, return_exceptions=True)


async def test_approval_config_picker_edits_only_its_draft_and_returns_to_approval(frontend, monkeypatch):
    command_loop = frontend.root.loop
    root = command_loop.session
    root.config.providers["alternate"] = ProviderConfig(model="o3", available_models=("o3", "o4-mini"), reasoning="low")
    spawn = SubagentTool(root, [{"action": "spawn", "name": "child", "message": "task"}])
    sibling = SubagentTool(root, [{"action": "spawn", "name": "sibling", "message": "other task"}])
    draft = spawn.approval_config()
    harness = ModalHarness([
        "g", "enter",  # config -> provider
        "g", "enter",  # alternate provider
        "enter",          # o3
        "enter",          # automatic API
        "G", "enter",  # high effort
        "up", "enter",   # config -> API
        "G", "enter",  # anthropic API
        "escape",         # return to approval
    ], consumed=True)
    monkeypatch.setattr(frontend.root.tui, "show_modal", harness.show_modal)
    await command_loop.agent.hooks.approval_config(draft)
    assert draft.config.active_provider == "alternate"
    assert draft.config.provider.model == "o3"
    assert draft.config.provider.reasoning == "high"
    assert draft.config.provider.api == "anthropic"
    assert draft.provider_overrides["active_provider"] == "alternate"
    assert root.config.active_provider == "default"
    assert sibling.approval_config().config.active_provider == "default"
    assert len(frontend.group.entries) == 1
    assert harness.pos == len(harness.keys)


async def test_group_limit_setting_validation_and_status_from_each_agent(frontend):
    root = frontend.root.loop
    assert set_value(root, "runtime.max_subagents 5") == "Set runtime.max_subagents"
    for value in ("-1", "33", "many"):
        assert set_value(root, "runtime.max_subagents " + value) == "Invalid value for runtime.max_subagents"
    assert root.session.settings.max_subagents == 5
    assert set_value(root, "runtime.worker on") == "Unknown config key: runtime.worker"
    runtime = await child(frontend)
    assert "1/5 retained (group-wide)" in status(root, "")
    assert "1/5 retained (group-wide)" in status(runtime.loop, "")
    assert "main agent" in set_value(runtime.loop, "runtime.max_subagents 32")
    assert set_value(root, "runtime.max_subagents 0") == "Set runtime.max_subagents"
    assert "1/0 retained (group-wide)" in status(runtime.loop, "")


async def pick(frontend, runtime, monkeypatch):
    async def choice(*args, **kwargs):
        return runtime.loop.session.uid

    monkeypatch.setattr(agents_module, "choice_application", choice)
    await agents_command(frontend.current.loop, "")


async def test_picker_includes_main_without_children(frontend, monkeypatch):
    async def choice(command_loop, title, choices, *, labels, current, preview_fn, disabled, label_fn, **kwargs):
        uid = frontend.root.loop.session.uid
        assert choices == (uid,)
        assert current == uid
        assert disabled == set()
        assert "main" in labels[uid] and "idle" in labels[uid]
        assert "main" in "".join(text for _, text in preview_fn(uid))
        return uid

    monkeypatch.setattr(agents_module, "choice_application", choice)
    await agents_command(frontend.root.loop, "")
    assert frontend.current is frontend.root
    assert not frontend.switching


async def test_picker_exposes_state_context_and_preview_without_changing_focus(frontend, monkeypatch):
    runtime = await child(frontend)
    runtime.loop.session.usage.add({"prompt_tokens": 320, "completion_tokens": 10}, budget=1000)
    previews = []

    async def choice(command_loop, title, choices, *, labels, current, preview_fn, disabled, label_fn, **kwargs):
        assert title == "Agents"
        assert "main" in labels[frontend.root.loop.session.uid]
        assert "completed" in labels[runtime.loop.session.uid]
        assert "ctx 32%" in labels[runtime.loop.session.uid]
        runtime.loop.session.state.awaiting_input = True
        live = "".join(text for _, text in label_fn(runtime.loop.session.uid))
        assert "waiting for input" in live
        runtime.loop.session.state.awaiting_input = False
        previews.append("".join(text for _, text in preview_fn(runtime.loop.session.uid)))
        assert frontend.current is frontend.root  # moving the cursor only previews.

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
        assert "inspect the pending work" in "".join(text for _, text in kwargs["preview_fn"](entry.agent.session.uid))
        with monkeypatch.context() as clock:
            clock.setattr(agents_module.time, "monotonic", lambda: 0)
            first = kwargs["label_fn"](entry.agent.session.uid)
            clock.setattr(agents_module.time, "monotonic", lambda: .8)
            second = kwargs["label_fn"](entry.agent.session.uid)
        assert first[0][1] == second[0][1] == "● " and first[0][0] != second[0][0]
        assert kwargs["label_fn"](frontend.root.loop.session.uid)[0] == ("class:text", "● ")

    monkeypatch.setattr(agents_module, "choice_application", choice)
    await frontend.select(frontend.root.loop)
    release.set()
    if entry.task is not None:
        await asyncio.wait_for(asyncio.shield(entry.task), 3)


@pytest.mark.parametrize("width", [20, 40, 80, 180])
@pytest.mark.parametrize("height", [5, 8, 12, 40])
def test_agent_preview_is_a_bounded_box_with_wrapped_live_text(width, height):
    from prompt_toolkit.utils import get_cwidth

    preview = AgentPreview("中文任务 " * 100, "old line\n" * 30 + "最新 live text", True, "ui-review")
    parts = preview.fragments(width, height)
    text = "".join(value for _, value in parts)
    lines = text.splitlines()
    assert len(lines) <= min(height, 16)
    assert "ui-review" in lines[0] and lines[0].strip().startswith("┌")
    assert lines[-1].strip().startswith("└")
    assert all(get_cwidth(line) == min(width, 100) for line in lines)
    assert "Task" in text and "Live" in text and "…" in text
    assert "live text" in text if width >= 40 else "text" in text
    assert any(style == "class:text" for style, value in parts if "text" in value)


def test_roomy_agent_preview_separates_headings_and_aligns_body_text():
    text = "".join(value for _, value in AgentPreview("Task body", "Latest reply", name="reviewer").fragments(80, 16))
    rows = text.splitlines()
    task = next(i for i, row in enumerate(rows) if row.strip(" │") == "Task")
    reply = next(i for i, row in enumerate(rows) if row.strip(" │") == "Reply")
    assert rows[task + 1].index("Task body") == rows[reply + 1].index("Latest reply") == 6
    assert not rows[1].strip(" │") and not rows[-2].strip(" │")
    assert not rows[reply - 1].strip(" │")


@pytest.mark.parametrize("width", [40, 50, 70, 80, 140])
async def test_agent_list_aligns_columns_and_selection_with_preview(frontend, monkeypatch, width):
    from os import terminal_size

    from prompt_toolkit.utils import get_cwidth

    await child(frontend, "core-review")
    await child(frontend, "中文界面-review")
    await child(frontend, "test-gap-review")
    frontend.root.loop.session.state.turn_elapsed = 192
    monkeypatch.setattr(agents_module.shutil, "get_terminal_size", lambda _fallback: terminal_size((width, 40)))

    async def choice(_loop, _title, choices, *, label_fn, preview_fn, preview_title, **kwargs):
        rows = ["".join(text for _, text in label_fn(uid)) for uid in choices]
        assert {get_cwidth(row) for row in rows} == {min(width, 100) - 8}
        assert len({get_cwidth(row[:row.index("ctx")]) for row in rows}) == 1
        assert len({get_cwidth(row[:row.index("%")]) for row in rows}) == 1
        statuses = [frontend.group.entry(uid).status for uid in choices]
        assert len({get_cwidth(row[:row.index(status)]) for row, status in zip(rows, statuses, strict=True)}) == 1
        assert ("(current)" if width >= 70 else "*") in rows[0]
        if width >= 80:
            assert "3m12s" in rows[0]
        assert "3m12s" in "".join(text for _, text in preview_fn(choices[0]))
        assert all(not row.rstrip().endswith("...") for row in rows)
        assert preview_title == " "  # the frame supplies its own border; no redundant rule
        frame = "".join(text for _, text in preview_fn(choices[-1])).splitlines()
        assert get_cwidth(frame[0]) == get_cwidth(rows[0]) + 8

    monkeypatch.setattr(agents_module, "choice_application", choice)
    await frontend.select(frontend.root.loop)


@pytest.mark.parametrize("keys,stopped,confirmed", [
    (["down", "x", "enter", "escape"], False, True),
    (["down", "x", "up", "enter", "escape"], True, True),
    (["down", "X", "escape"], True, False),
    (["/", "x", "X", "enter", "escape", "escape"], False, False),
])
async def test_agent_picker_confirmed_and_immediate_stops_keep_selection(frontend, monkeypatch, keys, stopped, confirmed):
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(client, messages, tools=None):
        entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await frontend.group.spawn(frontend.root.loop.session, "api-review", "review the API")
    await asyncio.wait_for(entered.wait(), 3)
    modal = ModalHarness(keys, consumed=True)
    monkeypatch.setattr(frontend.root.tui, "show_modal", modal.show_modal)
    await frontend.select(frontend.root.loop)
    assert frontend.current is frontend.root and not frontend.switching
    frames = ["".join(value for _, value in frame) for frame in modal.frames]
    assert any("Stop api-review?" in frame for frame in frames) == confirmed
    if keys[0] != "/":
        assert any("┌─ api-review" in frame for frame in frames)
    if stopped:
        task = entry.task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), 3)
        assert entry.status == "interrupted" and entry.task is None
    else:
        assert entry.status == "running"
        release.set()
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
