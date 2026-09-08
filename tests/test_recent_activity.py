"""Historical activity survives compaction without becoming a mutable request prefix."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from agent_harness import call, session

from wizolt.compaction import Compactor
from wizolt.context import ContextManager
from wizolt.model import ModelClient
from wizolt.runner import ToolRunner
from wizolt.session import SessionSnapshotStore
from wizolt.tools import BashTool, NoteTool


async def test_activity_uses_completed_tool_evidence_and_keeps_both_exit_results(tmp_path):
    s = session(tmp_path)
    s.settings.yolo = True
    runner = ToolRunner(s, ContextManager(s), output_fn=lambda _: None)
    await runner.run(
        [
            call("Edit", ["file.py", "", [{"op": "create", "content": "hello\n"}]]),
            call("Bash", ["exit 1"]),
            call("Bash", ["printf 'FAIL: this is only output' ; exit 0"]),
            call("Read", [{"path": "missing.py"}]),
        ]
    )
    activity = s.recent_activity()
    assert '"file.py"' in activity
    assert '"exit 1": exit code 1' in activity
    assert 'exit 0": exit code 0' in activity
    assert activity.index("exit code 1") < activity.index("exit code 0")
    assert "Read:" in activity and "missing.py" in activity
    assert "errors may already be resolved" in activity
    assert len(s.recent_commands) == 2
    view = json.loads(NoteTool(s, [{"action": "view"}]).call())
    assert view["recent_activity"] == activity
    NoteTool(s, [{"set_goal": "next goal"}]).call()
    assert s.recent_activity() == activity


async def test_read_refused_skipped_and_cancelled_calls_do_not_invent_activity(tmp_path, monkeypatch):
    s = session(tmp_path)
    (tmp_path / "file.py").write_text("hello\n")
    runner = ToolRunner(s, ContextManager(s), input_fn=lambda _: "n", output_fn=lambda _: None)
    await runner.run([call("Read", [{"path": "file.py"}]), call("Bash", ["exit 1"]), call("Bash", ["exit 2"])])
    assert not s.recent_activity()
    s.settings.yolo = True
    started = asyncio.Event()

    async def pending_command(_tool):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(BashTool, "call", pending_command)
    task = asyncio.create_task(runner.run([call("Bash", ["printf started; sleep 30"])]))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not s.recent_activity()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_checkpoints_freeze_activity_and_resume_preserves_the_bounded_window(tmp_path):
    s = session(tmp_path)
    ctx = ContextManager(s)
    s.messages = [{"role": "user", "content": "continue"}]
    normal = deepcopy(ctx.model_messages("system"))
    s.record_command_result("exit 1", 1)
    assert ctx.model_messages("system") == normal
    ctx.apply_compaction({"summary": "first"}, [], compacted=s.messages)
    first = deepcopy(s.messages)
    assert "exit code 1" in s.messages[0]["content"]
    s.record_command_result("exit 0", 0)
    assert s.messages == first
    await s.save_snapshot()
    for i in range(12):
        s.record_command_result(f"command {i}", i)
    s.record_command_result("command 4", 4)
    assert len(s.recent_commands) == 10
    assert s.recent_commands[-1]["command"] == "command 4"
    await s.save_snapshot()
    restored = SessionSnapshotStore.load(s.uid, s.config, s.settings, cwd=s.cwd)
    assert restored.recent_commands == s.recent_commands
    assert restored.messages[:-1] == first
    assert restored.messages[-1]["_session_event"] == "resumed"
    ContextManager(restored).apply_compaction({"summary": "second"}, [], compacted=restored.messages)
    assert len(restored.messages) == 1
    assert restored.messages[0]["content"].count("Recent tool activity") == 1
    assert '"command 4": exit code 4' in restored.messages[0]["content"]
    assert '"exit 1": exit code 1' not in restored.messages[0]["content"]
    assert s.messages == first


def test_activity_is_bounded_and_compactor_receives_it_even_for_fallback(tmp_path):
    s = session(tmp_path)
    for i in range(30):
        s.store_turn_diff(str(i), i, f"file{i}.py", "-a\n+b")
    s.store_turn_diff("again", 31, "file22.py", "-b\n+c")
    s.record_command_result("x" * 10000, 1)
    activity = s.recent_activity()
    assert '"file19.py"' not in activity
    assert activity.count('"file22.py"') == 1
    assert activity.index('"file29.py"') < activity.index('"file22.py"')
    assert len(activity) < 6000
    ctx = ContextManager(s)
    assert activity in Compactor(ctx, ModelClient(s)).input([])
    ctx.apply_compaction(None, [], fallback_note="trimmed", compacted=[{"role": "user", "content": "old"}])
    assert activity in s.messages[0]["content"]
    assert not json.loads(NoteTool(session(tmp_path), [{"action": "view"}]).call()).get("recent_activity")


def test_inline_compaction_appends_activity_without_changing_cached_messages(tmp_path):
    s = session(tmp_path)
    s.messages = [{"role": "user", "content": "old request"}, *({"role": "assistant", "content": f"step {i}"} for i in range(15))]
    ctx = ContextManager(s)
    compactor = Compactor(ctx, ModelClient(s))
    compacted, _ = compactor.parts()
    before = compactor.request(compacted)
    assert before is not None
    s.record_command_result("exit 0", 0)
    after = compactor.request(compacted)
    assert after is not None
    assert after[0][:-1] == before[0][:-1]
    assert after[1] == before[1]
    assert s.recent_activity() in after[0][-1]["content"]


async def test_legacy_snapshot_without_command_receipts_loads_empty(tmp_path):
    from test_session_persistence import log_path

    s = session(tmp_path)
    s.messages = [{"role": "user", "content": "old session"}]
    await s.save_snapshot()
    path = Path(log_path(s))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for record in records:
        record.pop("recent_commands", None)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    restored = SessionSnapshotStore.load(s.uid, s.config, s.settings, cwd=s.cwd)
    assert restored.recent_commands == []
    assert restored.recent_activity() == ""


def test_note_activity_is_read_only_and_projection_has_a_total_budget(tmp_path):
    from wizolt.base import ToolError

    s = session(tmp_path)
    for i in range(10):
        s.record_command_result(str(i) + "\n" * 320, i)
        s.store_turn_diff(str(i), i, str(i) + "\n" * 240, "changed")
    before = s.recent_activity()
    assert len(before) < 6400
    with pytest.raises(ToolError):
        NoteTool(s, [{"recent_activity": "everything passed"}]).call()
    assert s.recent_activity() == before
    assert json.loads(NoteTool(s, [{"fields": ["recent_activity"]}]).call()) == {"recent_activity": before}


async def test_frozen_activity_escapes_ids_and_pins_no_retention_root(tmp_path):
    (tmp_path / "one.py").write_text("hello\n")
    (tmp_path / "two.py").write_text("world\n")
    s = session(tmp_path)
    s.settings.yolo = True
    ctx = ContextManager(s)
    runner = ToolRunner(s, ctx, output_fn=lambda _: None)
    await runner.run([call("Read", [{"path": "one.py"}])])
    view = next(iter(s.source_views))
    # An Edit naming the view for a different path fails with the id quoted; a Recall failure can
    # quote a tr.N key; a command can echo one. None may survive into text that outlives the
    # records it names.
    await runner.run([call("Edit", ["two.py", view, [{"op": "replace", "start": 1, "end": 1, "content": "x\n"}]])])
    s.record_tool_error("-", "Recall", ["tr.7"], "ToolError: tr.7 is unknown or expired")
    record = s.store_tool_result("Bash", ["echo evidence"], "evidence")
    s.record_command_result(f"echo {view} {record}", 0)
    activity = s.recent_activity()
    assert view not in activity and r"view\u002e" in activity
    assert "tr.7" not in activity and r"tr\u002e7" in activity
    assert record in s.tool_results
    ctx.apply_compaction({"summary": "mismatch"}, [], compacted=s.messages)
    checkpoint = s.messages[0]["content"]
    assert view not in checkpoint and r"view\u002e" in checkpoint
    assert view not in s.source_views
    assert record not in s.tool_results


async def test_activity_preserves_id_shaped_filenames_commands_and_errors(tmp_path):
    s = session(tmp_path)
    path = "fixtures/view.12.py"
    command = r'cat "fixtures/tr.7.txt" fixtures/view.12.py literal\u002e'
    error = 'ToolError: missing "fixtures/tr.7.txt"'
    s.store_turn_diff("edit", 1, path, "-old\n+new")
    s.record_command_result(command, 1)
    s.record_tool_error("error", "Bash", [command], error)
    activity = s.recent_activity()
    rows = [row for row in activity.splitlines() if row.startswith("- ")]
    assert json.loads(rows[0][2:]) == path
    assert json.loads(rows[1][2:].rsplit(": exit code ", 1)[0]) == command
    assert json.loads(rows[2].split(": ", 1)[1]) == error
    assert s.turn_diffs[-1].path == path
    assert s.recent_commands[-1]["command"] == command
    note = NoteTool(s, [{"fields": ["recent_activity"]}]).call()
    assert json.loads(note)["recent_activity"] == activity
    assert "view.12" not in note and "tr.7" not in note
    ctx = ContextManager(s)
    ctx.apply_compaction({"summary": "continue"}, [], compacted=[])
    assert activity in s.messages[0]["content"]
    await s.save_snapshot()
    restored = SessionSnapshotStore.load(s.uid, s.config, s.settings, cwd=s.cwd)
    assert restored.recent_activity() == activity
    assert activity in restored.messages[0]["content"]


async def test_timed_out_command_settles_at_the_reported_exit_code(tmp_path):
    s = session(tmp_path)
    s.settings.yolo = True
    s.settings.shell_timeout = 0.2
    s.settings.bash_wait_timeout = 0
    runner = ToolRunner(s, ContextManager(s), output_fn=lambda _: None)
    message = (await runner.run([call("Bash", ["sleep 5"])]))[0]["content"]
    assert "exit_code: -1" in message and "timeout" in message
    # The receipt must agree with the result the model saw, not the SIGKILL behind it.
    assert s.recent_commands == [{"command": "sleep 5", "exit_code": -1}]
