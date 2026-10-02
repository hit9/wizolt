"""Exercise separate engines, shared files and durable inboxes at the agent-group boundary."""

import asyncio
import json
from copy import deepcopy

import pytest
from agent_harness import call, session_with_provider

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features, close_agent_resources
from wizolt.base import ModelError, ToolError, WizoltError
from wizolt.model.client import ModelClient
from wizolt.session import Session, SessionSnapshotStore
from wizolt.shellhooks import HookCommand, ShellHooks
from wizolt.tools import SkillTool, SubagentTool, Tool


@pytest.fixture
async def group(tmp_path):
    root = Agent(session_with_provider(tmp_path), output_fn=lambda _: None)
    yield root.session.subagents
    await close_agent_resources(root)
    root.session.close()


async def finished(group, entry):
    task = entry.task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 3)
    return entry


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


async def test_forked_skill_uses_a_child_without_forking_again(group, isolate_home, monkeypatch):
    folder = isolate_home / ".claude" / "skills" / "inspect"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: inspect\ndescription: inspect\ncontext: fork\n---\nCheck the parser carefully.\n")
    root = group.root.session
    root.skills.reload()
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
    assert await skill.call() == "parser checked"
    assert len(group.entries) == 2
    entry = next(entry for entry in group.entries.values() if entry.agent is not group.root)
    assert entry.agent.session.active_skills == ["inspect"]
    assert root.active_skills == []
    assert "Check the parser carefully" in str(requests[1])
    assert not SkillTool(entry.agent.session, ["inspect"]).fork_order
