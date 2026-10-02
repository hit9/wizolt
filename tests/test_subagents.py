"""Exercise separate engines, shared files and durable inboxes at the agent-group boundary."""

import asyncio
import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest
from agent_harness import call, session_with_provider

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features, close_agent_resources
from wizolt.base import ConfigError, ModelError, ToolError, WizoltError
from wizolt.config import ProviderConfig, RuntimeSettings, SubagentDefaults
from wizolt.model.client import ModelClient
from wizolt.session import Session, SessionSnapshotStore
from wizolt.shellhooks import HookCommand, ShellHooks
from wizolt.tools import SkillTool, SubagentTool, Tool, toolblocks
from wizolt.ui.cli import CommandLoop, commands
from wizolt.ui.cli.agents import ModelSettingsEditor


@pytest.fixture
async def group(tmp_path):
    root = Agent(session_with_provider(tmp_path), output_fn=lambda _: None)
    yield root.session.subagents
    await close_agent_resources(root)
    root.session.close()


async def finished(group, entry):
    while (task := entry.task) is not None:
        await asyncio.wait_for(asyncio.shield(task), 3)
    return entry


async def test_turn_clock_is_live_then_frozen_and_restored(group, monkeypatch):
    from wizolt.session import types

    now = [100.0]
    monkeypatch.setattr(types.time, "monotonic", lambda: now[0])

    async def request(client, messages, tools=None):
        now[0] += 192
        assert client.session.state.elapsed == 192
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    await group.root.run("work")
    state = group.root.session.state
    assert state.turn_started_at == 0
    assert state.elapsed == 192
    now[0] += 40
    assert state.elapsed == 192
    loaded = SessionSnapshotStore.load(group.root.session.uid, config=group.root.session.config, settings=group.root.session.settings)
    assert loaded.state.elapsed == 192


async def test_subagent_defaults_seed_independent_approvals_and_survive_resume(group, monkeypatch):
    root = group.root.session
    root.config.providers["deepseek"] = ProviderConfig(url="http://test", key="test", model="base")
    root.config.subagent = SubagentDefaults(provider="deepseek", model="child-default", reasoning="high", api="responses")
    root.provider_overrides = {"active_provider": root.config.active_provider}
    one = SubagentTool(root, [{"action": "spawn", "name": "one", "message": "task"}])
    two = SubagentTool(root, [{"action": "spawn", "name": "two", "message": "task"}])
    first, second = one.approval_config(), two.approval_config()
    assert first.config.active_provider == second.config.active_provider == "deepseek"
    assert first.config.provider.model == second.config.provider.model == "child-default"
    first.config.active_provider = root.config.active_provider
    first.config.provider.model = "approved-model"
    assert second.config.provider.model == "child-default"

    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    await one.call()
    await two.call()
    direct = await group.spawn(root, "direct", "task")
    for entry in tuple(group.entries.values())[1:]:
        await finished(group, entry)
    assert direct.agent.session.config.active_provider == "deepseek"
    children = {entry.agent.session.agent_name: entry.agent.session for entry in group.entries.values() if entry.parent}
    assert children["one"].config.provider.model == "approved-model"
    assert children["two"].config.provider.model == "child-default"
    assert root.config.provider.model != "approved-model"
    root.config.subagent = SubagentDefaults(provider="deepseek", model="changed-after-spawn")
    loaded = SessionSnapshotStore.load(children["one"].uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
    assert loaded.config.provider.model == "approved-model"


async def test_second_turn_on_same_agent_is_rejected_without_touching_first(group, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(client, messages, tools=None):
        entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    task = asyncio.create_task(group.root.run("first"))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        with pytest.raises(WizoltError, match="already has a turn"):
            await group.root.run("second")
        assert not task.done() and not task.cancelling()
        assert not any(m.get("content") == "second" for m in group.root.session.messages)
    finally:
        release.set()
        await task


@pytest.mark.parametrize("tool,yolo", [("Bash", False), ("Subagent", True)])
async def test_headless_child_approval_refuses_without_reading_process_stdin(group, monkeypatch, tool, yolo):
    def forbidden_input(*_args):
        pytest.fail("background child read process stdin")

    monkeypatch.setattr("builtins.input", forbidden_input)
    group.root.session.settings.yolo = yolo
    requests = []

    async def request(client, messages, tools=None):
        requests.append(deepcopy(messages))
        if len(requests) == 1:
            args = ["touch child-approval-sentinel"] if tool == "Bash" else [{"action": "spawn", "name": "nested", "message": "task"}]
            return {}, [call(tool, args)], ""
        return {"role": "assistant", "content": "approval unavailable"}, [], "approval unavailable"

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await group.spawn(group.root.session, "headless", "task")
    await finished(group, entry)
    assert entry.status == "completed"
    assert "Subagent approval requires an interactive frontend" in str(requests[-1])
    assert len(group.entries) == 2
    assert not Path(group.root.session.cwd, "child-approval-sentinel").exists()


async def test_child_tool_cannot_stop_main_or_wait_for_itself(group, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(client, messages, tools=None):
        if client.session is group.root.session:
            entered.set()
            await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    child = await group.spawn(group.root.session, "child", "task")
    await finished(group, child)
    main_turn = asyncio.create_task(group.root.run("main work"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(ToolError, match="Cannot stop the main"):
            await SubagentTool(child.agent.session, [{"action": "stop", "agent_id": group.root.session.uid}]).call()
        for action in ("stop", "wait"):
            with pytest.raises(ToolError, match="calling agent"):
                await SubagentTool(child.agent.session, [{"action": action, "agent_id": child.agent.session.uid}]).call()
        assert not main_turn.done() and not main_turn.cancelling()
    finally:
        release.set()
        await main_turn


async def test_two_spawns_have_independent_approval_settings_before_first_request_and_on_resume(group, monkeypatch):
    root = group.root.session
    root.settings.yolo = True
    root.config.providers["alternate"] = ProviderConfig(url="http://alternate", key="sk-test", model="gpt-4", reasoning="low")
    replies = iter(["c", "v", "", "config", "view", ""])
    command_loop = CommandLoop(group.root, input_fn=lambda _: next(replies), output_fn=lambda _: None)
    settings_seen, views, requests = [], [], {}

    async def configure(settings):
        assert len(group.entries) == 1 + len(settings_seen)
        assert settings is not root
        editor = ModelSettingsEditor(settings, command_loop.presentation, False)
        if not settings_seen:
            commands.set_provider(editor, "alternate")
        await commands.set_model(editor, "o3" if not settings_seen else "o4-mini")
        commands.set_reasoning(editor, "high" if not settings_seen else "low")
        commands.set_api(editor, "responses")
        settings_seen.append(settings)

    async def view(task):
        views.append(dict(task.rows))

    async def request(client, messages, tools=None):
        s = client.session
        requests[s.agent_name] = (s.config.active_provider, s.config.provider.model, s.config.provider.reasoning, s.config.provider.api)
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    group.root.hooks.approval_config = configure
    group.root.hooks.text_viewer = view
    await group.root.tools.run([
        call("Subagent", [{"action": "spawn", "name": "one", "message": "task one"}]),
        call("Subagent", [{"action": "spawn", "name": "two", "message": "task two"}]),
    ])
    assert not root.tool_errors
    children = [entry for entry in group.entries.values() if entry.parent]
    assert len(children) == 2
    for entry in children:
        await finished(group, entry)
        restored = SessionSnapshotStore.load(entry.agent.session.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
        assert restored.config.active_provider == entry.agent.session.config.active_provider
        assert restored.config.provider.model == entry.agent.session.config.provider.model
        assert restored.config.provider.reasoning == entry.agent.session.config.provider.reasoning
        assert restored.config.provider.api == "responses"
    assert requests == {"one": ("alternate", "o3", "high", "responses"), "two": ("default", "o4-mini", "low", "responses")}
    assert settings_seen[0] is not settings_seen[1]
    assert [view["model"] for view in views] == ["o3", "o4-mini"]
    assert root.config.active_provider == "default"
    assert root.config.provider.model == "gpt-4"
    assert root.provider_overrides == {}


@pytest.mark.parametrize("reply", ["n", None, "cost too high"])
async def test_yolo_spawn_refusal_after_configuration_leaves_no_child_or_parent_changes(group, reply):
    root = group.root.session
    root.settings.yolo = True
    replies = iter(["c", reply])
    CommandLoop(group.root, input_fn=lambda _: next(replies), output_fn=lambda _: None)

    async def configure(settings):
        settings.config.provider.model = "different"

    group.root.hooks.approval_config = configure
    await group.root.tools.run([call("Subagent", [{"action": "spawn", "name": "child", "message": "task"}])])
    assert len(group.entries) == 1
    assert root.subagent_entries == []
    assert root.config.provider.model == "gpt-4"
    assert "Cancelled" in root.tool_errors[-1].error


async def test_yolo_send_requires_approval_and_does_not_change_child_settings(group):
    root = group.root.session
    root.settings.yolo = True
    child = await group.spawn(root, "child", "initial")
    task = child.task
    group.stop(child.agent.session.uid)
    await asyncio.gather(task, return_exceptions=True)
    pending = list(child.agent.session.pending_user_inputs)
    prompts = []
    CommandLoop(group.root, input_fn=lambda prompt: prompts.append(prompt) or "n", output_fn=lambda _: None)
    await group.root.tools.run([call("Subagent", [{"action": "send", "agent_id": child.agent.session.uid, "message": "extra"}])])
    assert len(prompts) == 1
    assert child.agent.session.pending_user_inputs == pending
    assert "Cancelled" in root.tool_errors[-1].error


@pytest.mark.parametrize("limit", [0, 1, 5, 32])
async def test_configured_limit_is_group_wide_and_visible_to_all_models(group, limit):
    root = group.root.session
    root.settings.max_subagents = limit
    root_schema = next(s for s in Tool.resolved_schemas(root) if s["function"]["name"] == "Subagent")
    assert f"Maximum retained child agents: {limit} (excluding main)" in root_schema["function"]["description"]
    parent = root
    for index in range(min(limit, 5)):
        entry = await group.spawn(parent, f"child {index}", "task")
        task = entry.task
        group.stop(entry.agent.session.uid)
        await asyncio.gather(task, return_exceptions=True)
        parent = entry.agent.session
        parent.settings.max_subagents = 32  # child settings cannot increase group admission.
    if limit <= 5:
        with pytest.raises(ToolError, match="Subagent limit reached"):
            await group.spawn(parent, "overflow", "task")
    child_schema = next(s for s in Tool.resolved_schemas(parent) if s["function"]["name"] == "Subagent")
    assert f"Maximum retained child agents: {limit} (excluding main)" in child_schema["function"]["description"]


@pytest.mark.parametrize("value", [-1, 33, True, "3", 1.5])
def test_invalid_subagent_limit_config_is_rejected(value):
    with pytest.raises(ConfigError):
        RuntimeSettings.from_dict({"runtime": {"max_subagents": value}})


@pytest.mark.parametrize("value", [0, 3, 32])
def test_valid_subagent_limit_config(value):
    assert RuntimeSettings.from_dict({"runtime": {"max_subagents": value}}).max_subagents == value


async def test_parallel_engines_isolate_context_config_and_statistics(group, monkeypatch):
    root = group.root.session
    root.messages.append({"role": "user", "content": "parent secret"})
    entered = asyncio.Event()
    release = asyncio.Event()
    requests = {}

    async def request(client, messages, tools=None):
        requests[client.session.agent_name] = deepcopy(messages)
        if len(requests) == 2:
            entered.set()
        await release.wait()
        tokens = 100 if client.session.agent_name == "one" else 200
        client.session.usage.add({"prompt_tokens": tokens, "completion_tokens": 10}, budget=1000)
        return {"role": "assistant", "content": client.session.agent_name}, [], client.session.agent_name

    monkeypatch.setattr(ModelClient, "request", request)
    one = await group.spawn(root, "one", "task one")
    two = await group.spawn(root, "two", "task two")
    await asyncio.wait_for(entered.wait(), 3)
    assert one.status == two.status == "running"
    assert one.agent.session.cwd == two.agent.session.cwd == root.cwd
    assert all("parent secret" not in str(messages) for messages in requests.values())
    assert "task two" not in str(requests["one"])
    assert "task one" not in str(requests["two"])
    one.agent.session.config.provider.model = "changed"
    assert two.agent.session.config.provider.model == root.config.provider.model == "gpt-4"
    release.set()
    await finished(group, one)
    await finished(group, two)
    assert root.usage.calls == 0
    assert one.agent.session.usage.prompt_tokens == 100
    assert two.agent.session.usage.prompt_tokens == 200
    assert one.agent.session.usage.context_percent() == 10
    assert two.agent.session.usage.context_percent() == 20
    assert one.agent.session.messages[-1]["content"] == "one"
    assert two.agent.session.messages[-1]["content"] == "two"


async def test_steering_is_claimed_once_by_the_running_agent(group, monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("shared file")
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def request(client, messages, tools=None):
        requests.append(deepcopy(messages))
        if len(requests) == 1:
            entered.set()
            await release.wait()
            return {}, [call("Read", [{"path": "a.txt", "ranges": [[0, 0]]}])], ""
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await group.spawn(group.root.session, "reader", "read the shared file")
    await asyncio.wait_for(entered.wait(), 3)
    await group.send(entry.agent.session.uid, "also inspect the end")
    release.set()
    await finished(group, entry)
    assert len(requests) == 2
    assert sum("also inspect the end" in str(message) for message in requests[1]) == 1
    assert entry.agent.session.state.round_count == 1
    assert entry.agent.session.pending_user_inputs == []
    assert entry.agent.session.tool_records[0].name == "Read"
    assert group.root.session.tool_records == []


async def test_followup_reuses_child_history_and_queue_only_does_not_wake_it(group, monkeypatch):
    requests = []

    async def request(client, messages, tools=None):
        requests.append(deepcopy(messages))
        return {"role": "assistant", "content": "answer"}, [], "answer"

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await group.spawn(group.root.session, "child", "first task")
    await finished(group, entry)
    uid = entry.agent.session.uid
    await group.send(uid, "queued instruction", start=False)
    assert entry.task is None
    assert len(requests) == 1
    await group.send(uid, "second task")
    await finished(group, entry)
    assert "first task" in str(requests[1])
    assert "queued instruction" in str(requests[1])
    assert "second task" in str(requests[1])
    assert entry.agent.session.state.round_count == 2


@pytest.mark.parametrize("outcome", ["completed", "failed", "interrupted"])
@pytest.mark.parametrize("start", [False, True])
@pytest.mark.parametrize("boundary", ["turn", "consumer"])
async def test_send_during_final_snapshot_hands_off_to_a_new_consumer(group, monkeypatch, outcome, start, boundary):
    saving, release = asyncio.Event(), asyncio.Event()
    original = Session.save_snapshot
    gated = False

    async def save(session):
        nonlocal gated
        if session.agent_parent and session.state.last_turn_status == outcome and bool(session._active_runs) == (boundary == "turn") and not gated:
            gated = True
            saving.set()
            await release.wait()
        return await original(session)

    async def request(client, messages, tools=None):
        if not gated:
            if outcome == "failed":
                raise ModelError("provider failed")
            if outcome == "interrupted":
                raise asyncio.CancelledError
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(Session, "save_snapshot", save)
    monkeypatch.setattr(ModelClient, "request", request)
    entry = await group.spawn(group.root.session, "handoff", "first")
    await asyncio.wait_for(saving.wait(), 3)
    await group.send(entry.agent.session.uid, "second", start=start)
    release.set()
    await finished(group, entry)
    resumed = start or outcome == "completed"
    assert entry.agent.session.state.round_count == (2 if resumed else 1)
    assert entry.status == ("completed" if resumed else outcome)
    assert [item.text for item in entry.agent.session.pending_user_inputs] == ([] if resumed else ["second"])
    assert sum(item.get("content") == "second" for item in entry.agent.session.messages) == int(resumed)


@pytest.mark.parametrize("failure", [False, True])
async def test_interrupted_or_failed_child_keeps_held_work_paused(group, monkeypatch, failure):
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(client, messages, tools=None):
        entered.set()
        await release.wait()
        raise ModelError("provider failed")

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await group.spawn(group.root.session, "paused", "first")
    await asyncio.wait_for(entered.wait(), 3)
    entry.agent.session.enqueue_user_input("held work", next_turn=True)
    if failure:
        release.set()
    else:
        group.stop(entry.agent.session.uid)
    await finished(group, entry)
    assert entry.status == ("failed" if failure else "interrupted")
    assert [item.text for item in entry.agent.session.pending_user_inputs] == ["held work"]
    assert entry.task is None


async def test_timeout_and_stop_do_not_cancel_siblings(group, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def request(client, messages, tools=None):
        nonlocal count
        count += 1
        if count == 2:
            started.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    one = await group.spawn(group.root.session, "one", "one")
    two = await group.spawn(group.root.session, "two", "two")
    await asyncio.wait_for(started.wait(), 3)
    await group.wait(one.agent.session.uid, 0)
    assert one.status == two.status == "running"
    group.stop(one.agent.session.uid)
    await finished(group, one)
    assert one.status == "interrupted"
    assert two.status == "running"
    release.set()
    await finished(group, two)
    assert two.status == "completed"


@pytest.mark.parametrize("timeout,expected", [(None, 180), (0, 0), (180, 180), (600, 600), (900, 600)])
async def test_wait_timeout_defaults_and_bounds_leave_child_running(group, monkeypatch, timeout, expected):
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(client, messages, tools=None):
        entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await group.spawn(group.root.session, "long-task", "task")
    await asyncio.wait_for(entered.wait(), 3)
    real_wait = asyncio.wait
    timeouts = []

    async def immediate_wait(tasks, *, timeout):
        timeouts.append(timeout)
        return await real_wait(tasks, timeout=0)

    payload = {"action": "wait", "agent_id": entry.agent.session.uid}
    if timeout is not None:
        payload["timeout"] = timeout
    with monkeypatch.context() as clock:
        clock.setattr(asyncio, "wait", immediate_wait)
        result = json.loads(await SubagentTool(group.root.session, [payload]).call())
    assert timeouts == [expected]
    assert entry.status == result[0]["status"] == "running"
    assert not entry.task.cancelling()
    release.set()
    await finished(group, entry)


def test_wait_schema_exposes_long_wait_limit_and_default():
    schema = SubagentTool.params_schema()["properties"]["timeout"]
    assert schema["minimum"] == 0 and schema["maximum"] == 600
    assert "180" in schema["description"]


@pytest.mark.parametrize("previous", ["", "earlier answer"])
async def test_stop_and_list_survive_interrupted_tool_call_only_history(group, monkeypatch, previous):
    entered, release = asyncio.Event(), asyncio.Event()
    requests = {}
    blocked = 0

    async def request(client, messages, tools=None):
        nonlocal blocked
        uid = client.session.uid
        requests[uid] = requests.get(uid, 0) + 1
        if requests[uid] == 1:
            return {"role": "assistant", "content": None}, [call("Read", [{"path": "missing.txt", "ranges": [[0, 0]]}])], ""
        blocked += 1
        if blocked == 2:
            entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    root = group.root.session
    root.messages.append({"role": "assistant", "content": None})
    one = await group.spawn(root, "api-review", "inspect API")
    if previous:
        one.agent.session.messages.append({"role": "assistant", "content": previous})
    two = await group.spawn(root, "ui-review", "inspect UI")
    await asyncio.wait_for(entered.wait(), 3)
    stop = SubagentTool(root, [{"action": "stop", "agent_id": one.agent.session.uid}])
    result = json.loads(await stop.call())[0]
    assert result["status"] == "interrupted"
    assert result["answer"] == previous
    assert one.task is None and one.agent._active_task is None
    assert two.status == "running"
    assert any(
        m.get("role") == "assistant" and m.get("content") is None for m in one.agent.session.messages
    )
    assert json.loads(await stop.call())[0]["status"] == "interrupted"  # repeated stops are safe.
    listing = json.loads(await SubagentTool(root, [{"action": "list"}]).call())
    assert len(listing) == 3 and listing[0]["answer"] == ""
    release.set()
    await finished(group, two)


@pytest.mark.parametrize("name", ["main", "MAIN", "api-review", " API-review "])
async def test_agent_names_are_required_and_unique_across_the_group(group, name):
    entry = await group.spawn(group.root.session, "api-review", "review")
    group.stop(entry.agent.session.uid)
    await group.wait(entry.agent.session.uid, 3)
    with pytest.raises(ToolError, match="name already in use"):
        await group.spawn(entry.agent.session, name, "nested review")
    assert len(group.entries) == 2


async def test_failed_child_can_be_followed_up_without_losing_context(group, monkeypatch):
    failed = True

    async def request(client, messages, tools=None):
        if failed:
            raise ModelError("provider failed")
        return {"role": "assistant", "content": "recovered"}, [], "recovered"

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await group.spawn(group.root.session, "child", "task")
    await finished(group, entry)
    assert entry.status == "failed"
    assert "provider failed" in entry.error
    failed = False
    await group.send(entry.agent.session.uid, "retry")
    await finished(group, entry)
    assert entry.status == "completed"


async def test_child_references_and_queued_inputs_survive_snapshot_deltas(group, monkeypatch):
    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "saved"}, [], "saved"

    monkeypatch.setattr(ModelClient, "request", request)
    root = group.root.session
    root.messages.append({"role": "user", "content": "main"})
    await root.save_snapshot()  # references are added by subsequent deltas, not the first snapshot.
    entry = await group.spawn(root, "saved child", "task")
    await finished(group, entry)
    await group.send(entry.agent.session.uid, "pending", start=False)
    restored = SessionSnapshotStore.load(root.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
    assert restored.subagent_entries == root.subagent_entries
    assert [item.uid for item in SessionSnapshotStore.list_sessions(root.config.data_dir, root.cwd)] == [root.uid]
    child = SessionSnapshotStore.load(entry.agent.session.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
    assert child.agent_name == "saved child"
    assert child.agent_parent == root.uid
    assert child.pending_user_inputs[0].text == "pending"
    assert any(message.get("content") == "saved" for message in child.messages)


async def test_restored_group_keeps_child_context_usage_and_queue_without_running(group, monkeypatch):
    async def request(client, messages, tools=None):
        client.session.usage.add({"prompt_tokens": 320, "completion_tokens": 10}, budget=1000)
        return {"role": "assistant", "content": "child history"}, [], "child history"

    monkeypatch.setattr(ModelClient, "request", request)
    root = group.root.session
    root.messages.append({"role": "user", "content": "main history"})
    entry = await group.spawn(root, "child", "original task")
    await finished(group, entry)
    await group.send(entry.agent.session.uid, "held input", start=False)
    restored = SessionSnapshotStore.load(root.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
    bootstrap_features(restored)
    restored.borrow_ownership(root)
    agent = Agent(restored, output_fn=lambda _: None)
    try:
        await restored.subagents.restore()
        child = restored.subagents.entry(entry.agent.session.uid)
        assert child.task is None
        assert child.instruction == "original task"
        assert child.agent.session.pending_user_inputs[0].text == "held input"
        assert child.agent.session.usage.prompt_tokens == 320
        assert "child history" in str(child.agent.session.messages)
        assert "main history" not in str(child.agent.session.messages)
        assert "child history" not in str(restored.messages)
        assert restored.usage.calls == 0
    finally:
        await close_agent_resources(agent)
        restored.close()


@pytest.mark.parametrize("damage", ["missing", "header", "record", "attach"])
async def test_bad_child_snapshot_does_not_block_healthy_family_restore(group, monkeypatch, damage):
    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "retained answer"}, [], "retained answer"

    monkeypatch.setattr(ModelClient, "request", request)
    root = group.root.session
    bad = await group.spawn(root, "bad", "bad task")
    healthy = await group.spawn(root, "healthy", "healthy task")
    descendant = await group.spawn(bad.agent.session, "descendant", "descendant task")
    for entry in (bad, healthy, descendant):
        await finished(group, entry)
    await close_agent_resources(group.root)
    path = Path(SessionSnapshotStore.find_session_path(root.config.data_dir, bad.agent.session.uid))
    if damage == "missing":
        path.unlink()
    elif damage == "header":
        path.write_text("not json\n")
    elif damage == "record":
        path.write_text(path.read_text() + "not json\n")
    restored = SessionSnapshotStore.load(root.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
    bootstrap_features(restored)
    restored.borrow_ownership(root)
    agent = Agent(restored, output_fn=lambda _: None)
    try:
        if damage == "attach":
            async def created(child):
                if child.session.uid == bad.agent.session.uid:
                    raise ValueError("bad restored frontend")
            restored.subagents.on_created = created
        problems = await restored.subagents.restore()
        assert len(problems) == 1 and bad.agent.session.uid in problems[0]
        assert set(restored.subagents.entries) == {root.uid, healthy.agent.session.uid, descendant.agent.session.uid}
        for original in (healthy, descendant):
            loaded = restored.subagents.entry(original.agent.session.uid)
            assert loaded.answer == "retained answer" and loaded.task is None
        assert len(restored.subagent_entries) == 3  # keep the damaged log recoverable
    finally:
        await close_agent_resources(agent)
        restored.close()


@pytest.mark.parametrize("limit,name,error", [(1, "new", "limit reached"), (2, "existing", "name already in use")])
async def test_restore_serializes_admission_before_limit_and_name_checks(group, monkeypatch, limit, name, error):
    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await group.spawn(group.root.session, "existing", "task")
    await finished(group, entry)
    root = group.root.session
    restored = SessionSnapshotStore.load(root.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
    restored.settings.max_subagents = limit
    bootstrap_features(restored)
    restored.borrow_ownership(root)
    agent = Agent(restored, output_fn=lambda _: None)
    loading, release = threading.Event(), threading.Event()
    original = SessionSnapshotStore.load

    def load(*args, **kwargs):
        loading.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(SessionSnapshotStore, "load", load)
    restoring = asyncio.create_task(restored.subagents.restore())
    spawning = None
    try:
        assert await asyncio.to_thread(loading.wait, 3)
        spawning = asyncio.create_task(restored.subagents.spawn(restored, name, "new task"))
        await asyncio.sleep(0)
        assert not spawning.done()
        release.set()
        assert await restoring == []
        with pytest.raises(ToolError, match=error):
            await spawning
    finally:
        release.set()
        await asyncio.gather(restoring, *([spawning] if spawning else []), return_exceptions=True)
        await close_agent_resources(agent)
        restored.close()


async def test_rejected_operations_do_not_create_or_wake_agents(group):
    with pytest.raises(ToolError, match="Unknown agent"):
        await group.send("missing", "hello")
    with pytest.raises(ToolError, match="name and message"):
        await group.spawn(group.root.session, "", "task")
    with pytest.raises(ToolError, match="main agent"):
        await group.send(group.root.session.uid, "loop")
    await group.close()
    with pytest.raises(ToolError, match="closed"):
        await group.spawn(group.root.session, "new", "task")
    assert len(group.entries) == 1


async def test_nested_agent_inherits_its_immediate_parents_hooks_and_sanitizes_name(group, monkeypatch):
    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    parent = await group.spawn(group.root.session, "parent", "task")
    await finished(group, parent)
    hook = HookCommand("PreToolUse", "true")
    parent.agent.session.shell_hooks = ShellHooks((hook,), parent.agent.session.cwd)
    nested = await group.spawn(parent.agent.session, "\x1b\n" + "very long child name " * 10, "nested task")
    await finished(group, nested)
    assert nested.parent == parent.agent.session.uid
    assert nested.agent.session.shell_hooks.configured == (hook,)
    assert "\x1b" not in nested.agent.session.agent_name and "\n" not in nested.agent.session.agent_name
    assert len(nested.agent.session.agent_name) <= 40


async def test_stopping_before_the_child_starts_reports_interruption(group):
    entry = await group.spawn(group.root.session, "child", "task")
    task = entry.task
    group.stop(entry.agent.session.uid)
    await asyncio.gather(task, return_exceptions=True)
    assert entry.status == "interrupted"
    assert entry.task is None
    assert entry.agent.session.pending_user_inputs[0].text == "task"


async def test_root_lease_cannot_be_released_before_child_shutdown(group):
    entry = await group.spawn(group.root.session, "child", "task")
    with pytest.raises(WizoltError, match="subagents are closed"):
        group.root.session.close()
    assert entry.agent.session.assert_ownership() is not None
    group.stop(entry.agent.session.uid)
    await group.close()
    assert entry.status == "interrupted"


async def test_shutdown_waits_for_accepted_spawn_before_stopping_children(group, monkeypatch):
    saving, release = asyncio.Event(), asyncio.Event()
    original = Session.save_snapshot

    async def save(session):
        if session.agent_parent and not saving.is_set():
            saving.set()
            await release.wait()
        return await original(session)

    monkeypatch.setattr(Session, "save_snapshot", save)
    spawning = asyncio.create_task(group.spawn(group.root.session, "child", "task"))
    await asyncio.wait_for(saving.wait(), 3)
    closing = asyncio.create_task(group.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    entry, _ = await asyncio.wait_for(asyncio.gather(spawning, closing), 3)
    assert group.closed and group.quiescent
    assert entry.task is None
    with pytest.raises(ToolError, match="closed"):
        await group.send(entry.agent.session.uid, "late input")


async def test_failed_shutdown_save_still_closes_clients_and_is_idempotent(group, monkeypatch):
    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    entry = await group.spawn(group.root.session, "child", "task")
    await finished(group, entry)
    closed = []

    async def close(client):
        closed.append(client.session.uid)

    async def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(ModelClient, "close", close)
    monkeypatch.setattr(entry.agent.session, "save_snapshot", fail_save)
    with pytest.raises(OSError, match="disk full"):
        await close_agent_resources(group.root)
    assert set(closed) == {group.root.session.uid, entry.agent.session.uid}
    assert group.closed and group.quiescent
    await group.close()  # completed shutdown never attempts to save released child sessions again.


@pytest.mark.parametrize("name,message", [(None, "task"), ("child", None), ([], "task"), ("child", {})])
async def test_invalid_spawn_fields_are_tool_errors_without_partial_children(group, name, message):
    with pytest.raises(ToolError, match="name and message"):
        await group.spawn(group.root.session, name, message)
    assert len(group.entries) == 1


async def test_tool_is_available_by_default_and_returns_agent_state(group, monkeypatch):
    names = [schema["function"]["name"] for schema in Tool.resolved_schemas(group.root.session)]
    assert "Subagent" in names
    assert "Delegate" not in names
    description = next(schema["function"]["description"] for schema in Tool.resolved_schemas(group.root.session) if schema["function"]["name"] == "Subagent")
    assert "SAME working directory and filesystem" in description
    assert "immediately visible" in description
    assert "Do not overwrite or revert" in description
    payload = json.loads(await SubagentTool(group.root.session, [{"action": "list"}]).call())
    assert payload[0]["name"] == "main"
    assert payload[0]["agent_id"] == group.root.session.uid


def test_spawn_approval_shows_settings_and_config_action_without_repeating_task(tmp_path):
    root = session_with_provider(tmp_path)
    invocation = call("Subagent", [{"action": "spawn", "name": "child", "message": "TASK-ONCE"}])
    tool = SubagentTool(root, invocation.args)
    block = toolblocks.approval_display(root, invocation, tool, "confirm")
    fields = {row.label.strip(): row.text for row, _ in block.walk()}
    assert fields["provider"] == "default"
    assert fields["model"] == "gpt-4"
    assert fields["effort"] == "medium"
    assert fields["files"] == "shared with all agents"
    assert str(block).count("TASK-ONCE") == 1
    assert toolblocks.approval_actions(tool) == [("Approve", ""), ("View agent task", "v"), ("Config", "c"), ("Refuse", "n")]


async def test_forked_skill_uses_a_child_without_forking_again(group, isolate_home, monkeypatch):
    folder = isolate_home / ".claude" / "skills" / "inspect"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: inspect\ndescription: inspect\ncontext: fork\n---\nCheck the parser carefully.\n")
    root = group.root.session
    root.skills.reload()
    root.config.subagent = SubagentDefaults(model="fork-default")
    root.settings.yolo = True
    requests = []

    async def request(client, messages, tools=None):
        requests.append(deepcopy(messages))
        if len(requests) == 1:
            return {}, [call("Skill", ["inspect"])], ""
        return {"role": "assistant", "content": "parser checked"}, [], "parser checked"

    monkeypatch.setattr(ModelClient, "request", request)
    skill = SkillTool(root, ["inspect"])
    assert skill.always_confirms()
    draft = skill.approval_config()
    assert draft.config.provider.model == "fork-default"
    draft.config.provider.model = "o3"
    assert dict(skill.approval_view().rows)["model"] == "o3"
    assert await skill.call() == "parser checked"
    assert len(group.entries) == 2
    entry = next(entry for entry in group.entries.values() if entry.agent is not group.root)
    assert entry.agent.session.config.provider.model == "o3"
    assert root.config.provider.model == "gpt-4"
    assert entry.agent.session.active_skills == ["inspect"]
    assert root.active_skills == []
    assert "Check the parser carefully" in str(requests[1])
    assert not SkillTool(entry.agent.session, ["inspect"]).fork_order
    assert entry.agent.session.agent_name == dict(skill.approval_view().rows)["agent"]
    repeats = [SkillTool(root, ["inspect"]), SkillTool(root, ["inspect"])]
    names = [dict(repeat.approval_view().rows)["agent"] for repeat in repeats]
    assert len(set(names)) == 2 and all(name.startswith("skill-inspect-") for name in names)
    assert await asyncio.gather(*(repeat.call() for repeat in repeats)) == ["parser checked", "parser checked"]
    assert {entry.agent.session.agent_name for entry in group.entries.values() if entry.parent} == {skill.fork_name, *names}


@pytest.mark.parametrize("limit", [0, 1])
async def test_forked_skills_respect_retained_limit_without_falling_back_inline(group, isolate_home, monkeypatch, limit):
    folder = isolate_home / ".claude" / "skills" / "inspect"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: inspect\ndescription: inspect\ncontext: fork\n---\nInspect in isolation.\n")
    root = group.root.session
    root.skills.reload()
    root.settings.max_subagents = limit

    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    if limit:
        assert await SkillTool(root, ["inspect"]).call() == "done"
    # User slash invocation and model invocation both preserve the fork boundary.
    assert await group.root.skill_command("/inspect")
    with pytest.raises(ToolError, match="Subagent limit reached"):
        await SkillTool(root, ["inspect"]).call()
    assert len(group.entries) == limit + 1
    assert root.active_skills == [] and root.messages == []


@pytest.mark.parametrize("stop", [False, True])
async def test_forked_skill_reports_failure_or_interruption(group, isolate_home, monkeypatch, stop):
    folder = isolate_home / ".claude" / "skills" / "inspect"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: inspect\ndescription: inspect\ncontext: fork\n---\nInspect.\n")
    group.root.session.skills.reload()
    entered = asyncio.Event()

    async def request(client, messages, tools=None):
        entered.set()
        if stop:
            await asyncio.Future()
        raise ModelError("skill request failed")

    monkeypatch.setattr(ModelClient, "request", request)
    task = asyncio.create_task(SkillTool(group.root.session, ["inspect"]).call())
    await asyncio.wait_for(entered.wait(), 3)
    if stop:
        entry = next(entry for entry in group.entries.values() if entry.parent)
        group.stop(entry.agent.session.uid)
    with pytest.raises(ToolError, match="interrupted" if stop else "skill request failed"):
        await asyncio.wait_for(task, 3)
