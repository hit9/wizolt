"""State ownership at the agent-group, feature and persistence boundaries."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from agent_harness import call, session_with_provider
from model_harness import AsyncCloseable
from PIL import Image

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import BackgroundServices, close_agent_resources
from wizolt.agent.prompts import SYSTEM_PROMPT
from wizolt.agent.subagents import SHARED_WORKSPACE
from wizolt.model.client import ModelClient
from wizolt.session import SessionSnapshotStore
from wizolt.tools import JobTool, NoteTool


@pytest.fixture
async def family(tmp_path, monkeypatch):
    async def request(client, messages, tools=None):
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(ModelClient, "request", request)
    root = Agent(session_with_provider(tmp_path), output_fn=lambda _: None)
    try:
        yield root.session.subagents
    finally:
        await close_agent_resources(root)
        root.session.close()


async def spawn(family, parent, name):
    entry = await family.spawn(parent, name, "task")
    await asyncio.wait_for(asyncio.shield(entry.task), 3)
    return entry.agent


async def test_child_frontend_shutdown_does_not_disable_main_completion(family, tmp_path):
    root = family.root.session
    (tmp_path / "example.txt").write_text("text")
    services = BackgroundServices(root, lambda error: pytest.fail(error))
    services.open_background()
    child = await spawn(family, root, "child")
    child_services = BackgroundServices(child.session, lambda error: pytest.fail(error))
    child_services.open_background()
    try:
        assert root.mentions is not child.session.mentions
        assert root.agents is not child.session.agents
        assert root.agents.session is root
        assert child.session.agents.session is child.session
        await child_services.close_background()
        refresh = root.mentions.refresh_owner()
        assert refresh is not None
        await refresh
        assert "example.txt" in [path for _, path in await root.mentions.candidates()]
    finally:
        await child_services.close_background()
        await services.close_background()


async def test_notes_receipts_statistics_and_assets_remain_independent_after_parallel_save(family, tmp_path):
    root = family.root.session
    one, two = await asyncio.gather(spawn(family, root, "one"), spawn(family, root, "two"))
    agents = [family.root, one, two]
    assets = []
    for index, agent in enumerate(agents):
        session = agent.session
        label = session.agent_name
        file = tmp_path / f"{label}.txt"
        file.write_text(f"{label} before\n")
        NoteTool(
            session,
            [
                {
                    "set_goal": f"goal {label}",
                    "replace_plan": [{"status": "doing", "text": f"plan {label}"}],
                    "replace_known": [f"fact {label}"],
                    "set_check": f"check {label}",
                }
            ],
        ).call()
        await agent.tools.run([call("Read", [{"path": file.name, "ranges": [[0, 0]]}])])
        key = session.tool_records[-1].key
        assert key == "tr.1"
        assert session.source_view_counter == 1
        session.store_turn_diff(key, 1, file.name, "-before\n+after\n", round=1)
        session.usage.add({"prompt_tokens": 100 + index, "completion_tokens": 10}, budget=1000)
        session.compaction_usage.add({"prompt_tokens": 20 + index, "completion_tokens": 2}, budget=1000)
        assets.append(await agent.context.materialize_output(key, f"output {label}"))
        session.enqueue_user_input(f"pending {label}")
    assert len(set(assets)) == 3
    assert [Path(path).read_text() for path in assets] == ["output main", "output one", "output two"]
    await asyncio.gather(*(agent.session.save_snapshot() for agent in agents))
    for index, agent in enumerate(agents):
        session = agent.session
        restored = SessionSnapshotStore.load(session.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
        notes = json.loads(NoteTool(restored, [{"action": "view", "fields": ["goal", "plan", "known", "check"]}]).call())
        assert notes == {
            "goal": f"goal {session.agent_name}",
            "plan": [{"status": "doing", "text": f"plan {session.agent_name}"}],
            "known": [f"fact {session.agent_name}"],
            "check": f"check {session.agent_name}",
        }
        assert restored.tool_counter == restored.source_view_counter == 1
        assert restored.usage.prompt_tokens == 100 + index
        assert restored.compaction_usage.prompt_tokens == 20 + index
        assert restored.pending_user_inputs[0].text == f"pending {session.agent_name}"
        assert [record.path for record in restored.turn_diffs] == [f"{session.agent_name}.txt"]
        assert restored.owns_asset(assets[index])
        assert all(not restored.owns_asset(path) for j, path in enumerate(assets) if j != index)
    NoteTool(one.session, [{"replace_plan": [], "replace_known": []}]).call()
    one.session.tool_results.clear()
    one.session.turn_diffs.clear()
    assert root.state.plan and two.session.state.plan
    assert root.state.known == ["fact main"] and two.session.state.known == ["fact two"]
    assert root.tool_results and two.session.tool_results
    assert root.turn_diffs and two.session.turn_diffs


async def test_inherited_model_is_pinned_on_resume_even_after_parent_changes(family):
    root = family.root.session
    child = await spawn(family, root, "child")
    child.session.config.provider.reasoning = "high"
    nested = await spawn(family, child.session, "nested")
    root.config.provider.model = "o3"
    root.config.provider.reasoning = "low"
    root.config.provider.api = "responses"
    for agent, effort in [(child, "medium"), (nested, "high")]:
        restored = SessionSnapshotStore.load(agent.session.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
        assert restored.config.provider.model == "gpt-4"
        assert restored.config.provider.reasoning == effort
        assert restored.config.provider.api == "auto"
        assert "key" not in json.dumps(restored.provider_overrides)
        assert "url" not in json.dumps(restored.provider_overrides)


async def test_compaction_exports_and_identical_image_assets_have_distinct_owners(family, tmp_path):
    root = family.root.session
    child = await spawn(family, root, "child")
    Image.new("RGB", (2, 2)).save(tmp_path / "shot.png")
    image_paths = []
    for agent in [family.root, child]:
        session = agent.session
        label = session.agent_name
        NoteTool(session, [{"set_goal": label, "replace_known": [label]}]).call()
        compacted = [{"role": "user", "content": f"history {label}"}]
        agent.context.apply_compaction({"summary": f"summary {label}"}, [], compacted=compacted, trigger="manual")
        assert session.history[0].key == "seg.1"
        exported = Path(session.images.assets_dir()) / "history.1.md"
        assert f"history {label}" in exported.read_text()
        index = Path(session.images.assets_dir()) / "history.md"
        assert f"summary {label}" in index.read_text()
        session.enqueue_user_input(session.images.recognize("inspect shot.png"))
        image_paths.append(session.images.asset_path(session.pending_user_inputs[0].images[0]))
    assert image_paths[0] != image_paths[1]
    assert Path(image_paths[0]).read_bytes() == Path(image_paths[1]).read_bytes()
    assert root.state.summaries[-1].endswith("]\nsummary main")
    assert child.session.state.summaries[-1].endswith("]\nsummary child")
    child.session.request_context_reset()
    child.session.apply_context_reset()
    assert root.state.known == ["main"] and child.session.state.known == ["child"]
    await asyncio.gather(root.save_snapshot(), child.session.save_snapshot())
    for agent in [family.root, child]:
        session = agent.session
        restored = SessionSnapshotStore.load(session.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
        assert restored.history[0].summary == f"summary {session.agent_name}"
        assert restored.owns_asset(restored.images.asset_path(restored.pending_user_inputs[0].images[0]))
        assert Path(restored.images.assets_dir(), "history.1.md").exists()
    assert all(Path(path).exists() for path in image_paths)


async def test_child_prompt_is_default_instructions_plus_shared_workspace_guard(family):
    root = family.root.session
    NoteTool(root, [{"set_goal": "parent secret"}]).call()
    child = await spawn(family, root, "child")
    nested = await spawn(family, child.session, "nested")
    for agent in [child, nested]:
        assert agent.session.system_prompt == SYSTEM_PROMPT + "\n" + SHARED_WORKSPACE
        assert agent.session.state.goal == ""
        assert "parent secret" not in str(agent.session.messages)


async def test_child_request_replaces_inherited_parent_lease_and_survives_parent_cancel(family):
    root = family.root
    child = await spawn(family, root.session, "child")
    parent_client, child_client = AsyncCloseable(), AsyncCloseable()
    entered, release = asyncio.Event(), asyncio.Event()
    output, children = [], []
    root.hooks.on_stream = lambda kind, text: output.append(("main", text))
    child.hooks.on_stream = lambda kind, text: output.append(("child", text))

    async def child_wire():
        child.model._request_callback(lambda: child.hooks.on_stream("text", "first"))
        entered.set()
        await release.wait()
        child.model._request_callback(lambda: child.hooks.on_stream("text", "last"))
        return "child result"

    async def parent_wire():
        root.model._request_callback(lambda: root.hooks.on_stream("text", "parent"))
        # Spawn inside the parent's request, deliberately inheriting its active ContextVar.
        children.append(asyncio.create_task(child.model.call_client(child_client, child_wire)))
        await asyncio.Event().wait()

    task = asyncio.create_task(root.model.call_client(parent_client, parent_wire))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert parent_client.closed == 1 and child_client.closed == 0
        release.set()
        assert await asyncio.wait_for(children[0], 3) == "child result"
        assert child_client.closed == 1
        assert output == [("main", "parent"), ("child", "first"), ("child", "last")]
    finally:
        for request in [task, *children]:
            request.cancel()
        await asyncio.gather(task, *children, return_exceptions=True)


async def test_interleaved_edits_do_not_attribute_sibling_changes_to_main(family, tmp_path):
    root = family.root.session
    child = await spawn(family, root, "child")
    (tmp_path / "shared.txt").write_text("one\ntwo\nthree\n")
    for agent, old, new in [(family.root, "one\n", "ONE\n"), (child, "two\n", "TWO\n"), (family.root, "three\n", "THREE\n")]:
        agent.session.settings.yolo = True
        await agent.tools.run([call("Edit", ["shared.txt", "", [{"old": old, "content": new}]])])
        assert not agent.session.tool_errors
    # Each session records its own edits and nothing else: the sibling's change is not in main's
    # receipts, and main's are not in the child's.
    main_diffs = "\n".join(diff.diff for diff in root.turn_diffs)
    child_diffs = "\n".join(diff.diff for diff in child.session.turn_diffs)
    assert "+ONE\n" in main_diffs and "+THREE\n" in main_diffs
    assert "+TWO\n" not in main_diffs and "-two\n" not in main_diffs
    assert "+TWO\n" in child_diffs and "+ONE\n" not in child_diffs
    await asyncio.gather(root.save_snapshot(), child.session.save_snapshot())
    restored = SessionSnapshotStore.load(root.uid, config=deepcopy(root.config), settings=deepcopy(root.settings), cwd=root.cwd)
    assert [diff.diff for diff in restored.turn_diffs] == [diff.diff for diff in root.turn_diffs]


async def test_job_ids_are_local_and_child_close_stops_only_its_jobs(family):
    root = family.root.session
    child = await spawn(family, root, "child")
    for session in [root, child.session]:
        await JobTool(session, [{"action": "start", "command": "sleep 30"}]).call()
    main_job, child_job = root.jobs["job.1"], child.session.jobs["job.1"]
    assert main_job.log_path != child_job.log_path
    assert main_job.process.pid != child_job.process.pid
    await close_agent_resources(child, shared_mcp=True)
    assert child_job.process.poll() is not None
    assert not Path(child_job.log_path).exists()
    assert main_job.process.poll() is None
    await close_agent_resources(family.root)
    assert main_job.process.poll() is not None
    assert not Path(main_job.log_path).exists()
