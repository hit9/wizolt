"""Lifecycle observations cross real workers; tool counts come from execution, not observers."""

import asyncio
import json
from dataclasses import asdict

import pytest
from agent_harness import call, session_with_provider

from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.agent.runner import ToolRunner
from wizolt.sdk import ToolCounts
from wizolt.tools import TOOL_REGISTRY, Tool


async def observer(session, tmp_path):
    path = tmp_path / "observer.py"
    path.write_text('''import json
from dataclasses import asdict
SDK_VERSION = 1
def setup(p):
    events = []
    async def observe(event):
        events.append(asdict(event))
    async def read(ctx, args):
        return json.dumps({"events": events, "turn": asdict(ctx.turn)})
    for event in ("session.started", "session.finished", "tool.started", "tool.finished"):
        p.on(event, observe)
    p.tool("read", "Read observed events", {"type": "object"}, read)
''')
    await session.plugins.manage("enable", str(path))


async def observed(session):
    return json.loads(await session.plugins.invoke("observer", "tool", "read", {}))


async def test_counts_include_serial_parallel_and_nested_calls_once(tmp_path):
    session = session_with_provider(tmp_path)
    session.settings.yolo = True
    (tmp_path / "a.txt").write_text("hello")
    runner = ToolRunner(session, ContextManager(session), output_fn=lambda _: None)
    try:
        await observer(session, tmp_path)
        await session.plugins.start_turn()
        await runner.run([call("Read", [{"path": "a.txt"}]), call("Read", [{"path": "a.txt"}])])
        await runner.run([call("Bash", ["exit 4"])])
        await runner.run([call("ToolScript", [{"action": "call", "code": 'call("Read", {"path": "a.txt"})'}])])
        facts = await observed(session)
        assert facts["turn"]["tools"] == asdict(ToolCounts(started=5, completed=4, failed=1))
        starts = [e["tool"] for e in facts["events"] if e["name"] == "tool.started"]
        finishes = [e["tool"] for e in facts["events"] if e["name"] == "tool.finished"]
        assert len(starts) == len(finishes) == 5
        assert len({item["id"] for item in starts}) == 5, "provider call IDs can repeat"
        script = next(item for item in starts if item["name"] == "ToolScript")
        assert starts[-1]["parent_id"] == script["id"]
        assert all(item["elapsed"] >= 0 and item["status"] != "running" for item in finishes)
        assert facts["turn"]["active_tools"] == []
    finally:
        await session.plugins.close()


async def test_counts_survive_late_enable_but_reset_each_turn_and_are_agent_local(tmp_path):
    session = session_with_provider(tmp_path)
    sibling = session_with_provider(tmp_path)
    runner = ToolRunner(session, ContextManager(session), output_fn=lambda _: None)
    (tmp_path / "a.txt").write_text("hello")
    try:
        await session.plugins.start_turn()
        await runner.run([call("Read", [{"path": "a.txt"}])])
        await session.plugins.finish_turn()
        await observer(session, tmp_path)
        facts = await observed(session)
        assert facts["events"] == []  # No fabricated history for late observers.
        assert facts["turn"]["tools"]["completed"] == 1
        assert sibling.plugins.facts().turn.tools.started == 0
        await session.plugins.start_turn()
        assert (await observed(session))["turn"]["tools"]["started"] == 0
    finally:
        await session.plugins.close()
        await sibling.plugins.close()


async def test_refusal_and_unknown_calls_do_not_count_as_execution(tmp_path):
    session = session_with_provider(tmp_path)
    runner = ToolRunner(session, ContextManager(session), input_fn=lambda _: "n", output_fn=lambda _: None)
    await runner.run([call("Bash", [":"])])
    await runner.run([call("Unknown", [])])
    assert session.plugins.facts().turn.tools == ToolCounts()


async def test_cancellation_settles_live_tool_and_publishes_finished(tmp_path, monkeypatch):
    entered = asyncio.Event()

    class BlockingTool(Tool):
        async def call(self):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setitem(TOOL_REGISTRY, "Blocking", BlockingTool)
    session = session_with_provider(tmp_path)
    runner = ToolRunner(session, ContextManager(session), output_fn=lambda _: None)
    try:
        await observer(session, tmp_path)
        task = asyncio.create_task(runner.execute_readonly(call("Blocking", [])))
        await asyncio.wait_for(entered.wait(), 5)
        active = session.plugins.facts().turn
        assert active.tools.running == 1 and active.active_tools[0].name == "Blocking"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        facts = await observed(session)
        assert facts["turn"]["tools"] == asdict(ToolCounts(started=1, cancelled=1))
        assert facts["events"][-1]["tool"]["status"] == "cancelled"
    finally:
        await session.plugins.close()


async def test_session_notifications_are_once_per_lifecycle(tmp_path):
    session = session_with_provider(tmp_path)
    agent = Agent(session, output_fn=lambda _: None)
    try:
        await observer(session, tmp_path)
        await agent.start_session()
        await agent.start_session()
        await agent.end_session("exit")
        await agent.end_session("exit")
        events = (await observed(session))["events"]
        assert [(e["name"], e["reason"]) for e in events] == [("session.started", "startup"), ("session.finished", "exit")]
    finally:
        await session.plugins.close()
        session.close()


async def test_broken_observer_cannot_prevent_tool_execution(tmp_path):
    session = session_with_provider(tmp_path)
    path = tmp_path / "broken.py"
    path.write_text('SDK_VERSION = 1\nasync def fail(event):\n    raise RuntimeError("observer failed")\ndef setup(p):\n    p.on("tool.started", fail)\n')
    (tmp_path / "a.txt").write_text("expected result")
    try:
        await session.plugins.manage("enable", str(path))
        runner = ToolRunner(session, ContextManager(session), output_fn=lambda _: None)
        await runner.run([call("Read", [{"path": "a.txt"}])])
        assert session.plugins.facts().turn.tools.completed == 1
        assert "observer failed" in session.plugins.entries["broken"].active.error
        assert "expected result" in next(iter(session.tool_results.values()))
    finally:
        await session.plugins.close()
