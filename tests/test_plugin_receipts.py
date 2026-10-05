"""Operation receipts persist, update incrementally, and resume truthfully after a crash."""

import asyncio

from test_session_persistence import log_path, read_jsonl, session_with_data_dir

from wizolt.agent.context import ContextManager
from wizolt.agent.lifecycle import bootstrap_features, load_session
from wizolt.agent.runner import ToolRunner
from wizolt.model import ModelClient
from wizolt.session import SessionSnapshotStore
from wizolt.session.types import OperationReceipt


def load(session):
    return SessionSnapshotStore.load(session.uid, config=session.config, settings=session.settings, cwd=session.cwd)


async def test_receipts_round_trip_and_checkpoint_only_changes(tmp_path):
    s = session_with_data_dir(tmp_path)
    s.messages = [{"role": "user", "content": "hello"}]
    first = s.record_operation(OperationReceipt("op1", "tool.call", "c1", '{"command": "ls"}', delivered=True))
    second = s.record_operation(OperationReceipt("op2", "tool.call", "c2", '{"command": "pwd"}'))
    await s.save_snapshot()
    second.core, second.delivered, second.shaped = "completed", True, ("audit/tool.call",)
    await s.save_snapshot()
    delta = read_jsonl(log_path(s))[-1]
    assert [item["id"] for item in delta["operation_receipts_upsert"]] == ["op2"]  # Not the whole list.
    restored = load(s)
    assert restored.operation_receipts == [first, second]
    restored.close()


async def test_resume_marks_a_started_operation_unknown_and_never_retries_it(tmp_path):
    s = session_with_data_dir(tmp_path)
    s.messages = [{"role": "user", "content": "hello"}]
    s.record_operation(OperationReceipt("op1", "tool.call", "c1", '{"command": "deploy"}', core="started"))
    s.record_operation(OperationReceipt("op2", "tool.call", "c2", '{"command": "ls"}', core="completed", actual="tr.4"))
    await s.save_snapshot()
    s.close()
    restored = load_session(s.uid, config=s.config, cwd=s.cwd)  # Writable resume, as the CLI does.
    states = {receipt.id: receipt.core for receipt in restored.operation_receipts}
    assert states == {"op1": "unknown", "op2": "completed"} and all(receipt.delivered for receipt in restored.operation_receipts)
    [notice] = [message for message in restored.messages if message.get("_session_event") == "interrupted_operations"]
    assert "deploy" in notice["content"] and "outcome unknown" in notice["content"] and "result tr.4" in notice["content"]
    await restored.save_snapshot()
    restored.close()
    again = load(restored)  # Settled once: a second resume adds no second notice.
    assert sum(message.get("_session_event") == "interrupted_operations" for message in again.messages) == 1
    again.close()


async def test_replay_shows_what_plugins_did_without_running_them(tmp_path):
    from wizolt.agent.engine import Agent
    from wizolt.ui.cli import CommandLoop

    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "check"})
    calls = [{"id": call_id, "type": "function", "function": {"name": "Bash", "arguments": f'{{"command": "{call_id}"}}'}} for call_id in ("c1", "c2")]
    s.messages.append({"role": "assistant", "content": "Running.", "tool_calls": calls})
    s.record_operation(OperationReceipt("op1", "tool.call", "c1", origin="cache/tool.call", delivered=True))
    s.record_operation(OperationReceipt("op2", "tool.call", "c2", core="completed", wrapper_failure="audit's tool.call interceptor failed: boom", delivered=True))
    await s.save_snapshot()
    s.close()
    restored = load_session(s.uid, config=s.config, cwd=str(tmp_path))
    output = []
    CommandLoop(Agent(restored, output_fn=output.append), output_fn=output.append).resume.render_resumed_session()
    text = "\n".join(str(item) for item in output)
    assert "answered by plugin cache; the tool did not run" in text
    assert "audit's tool.call interceptor failed: boom" in text
    restored.close()


async def test_replay_never_pins_a_receipt_on_a_reused_call_id(tmp_path):
    from wizolt.agent.engine import Agent
    from wizolt.ui.cli import CommandLoop

    s = session_with_data_dir(tmp_path)
    call = [{"id": "call_0", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}]
    for turn in ("first", "second"):  # Some providers number calls per turn: both are call_0.
        s.messages += [{"role": "user", "content": turn}, {"role": "assistant", "content": "Running.", "tool_calls": call}]
    s.record_operation(OperationReceipt("op1", "tool.call", "call_0", origin="cache/tool.call", delivered=True))
    await s.save_snapshot()
    s.close()
    restored = load_session(s.uid, config=s.config, cwd=str(tmp_path))
    output = []
    CommandLoop(Agent(restored, output_fn=output.append), output_fn=output.append).resume.render_resumed_session()
    # Which call_0 the plugin answered cannot be told apart, so neither shows a note it may not own.
    assert "answered by plugin cache" not in "\n".join(str(item) for item in output)
    restored.close()


async def test_a_crash_during_an_intercepted_model_request_resumes_as_unknown(tmp_path):
    from wizolt.config import ProviderConfig

    s = session_with_data_dir(tmp_path)
    s.config.providers = {"default": ProviderConfig(model="test-model", url="http://test", key="sk-test")}
    bootstrap_features(s)
    plugin = tmp_path / "router.py"
    plugin.write_text("SDK_VERSION = 1\ndef setup(p):\n    async def h(ctx, request, next):\n        return await next(request)\n    p.intercept('model.request', h)\n")
    await s.plugins.manage("enable", str(plugin))
    model = ModelClient(s)
    on_disk = []

    async def transport(messages, tools, provider):
        crashed = load(s)  # The on-disk state while the request is out, as a crash would leave it.
        on_disk.extend((receipt.operation, receipt.core) for receipt in crashed.operation_receipts)
        crashed.close()
        return {"role": "assistant", "content": "ok"}, [], "ok"

    model._transport_request = transport
    try:
        await model.request([{"role": "user", "content": "hi"}], [])
    finally:
        await s.plugins.close()
    # Settled as unknown on resume: the request may have been sent and billed.
    assert on_disk == [("model.request", "unknown")]


async def test_crash_at_the_execution_checkpoint_resumes_as_unknown(tmp_path):
    s = session_with_data_dir(tmp_path)
    bootstrap_features(s)
    s.settings.yolo = True
    plugin = tmp_path / "wrap.py"
    plugin.write_text("SDK_VERSION = 1\ndef setup(p):\n    async def h(ctx, call, next):\n        return await next(call)\n    p.intercept('tool.call', h)\n")
    await s.plugins.manage("enable", str(plugin))
    marker = tmp_path / "ran"
    runner = ToolRunner(s, ContextManager(s), output_fn=lambda _: None)
    task = asyncio.create_task(runner.run([ModelClient.tool_call("c1", "Bash", {"command": f"touch {marker}; sleep 30"})]))
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                await asyncio.sleep(0.05)
        crashed = load(s)  # The on-disk state at this instant, as a crash would leave it.
        [receipt] = crashed.operation_receipts
        assert receipt.core == "unknown" and receipt.target == "c1"
        assert any(message.get("_session_event") == "interrupted_operations" for message in crashed.messages)
        crashed.close()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await s.plugins.close()
    assert s.operation_receipts[-1].core == "interrupted" and s.operation_receipts[-1].delivered
