"""Edit receipts: the turn diff an Edit records, its persistence, and how a resume redraws it.

Nothing here aggregates receipts across edits: a receipt describes the edit that produced it, and
the working tree is what says how the files stand now.
"""

import subprocess

from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import load_session
from wizolt.agent.runner import ToolRunner
from wizolt.base import ToolCall
from wizolt.config import Config
from wizolt.session import Session, SessionSnapshotStore
from wizolt.tools import ReadTool
from wizolt.ui.cli import CommandLoop


def session(tmp_path):
    config = Config()
    config.data_dir = str(tmp_path / "data")
    return Session(cwd=str(tmp_path), config=config)


def git_init(path):
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)


async def test_tool_runner_captures_edit_turn_diff(tmp_path):
    git_init(tmp_path)
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    s = session(tmp_path)
    s.state.turn_step = 1
    s.state.round_count = 1
    s.settings.yolo = True

    runner = ToolRunner(s, ContextManager(s), input_fn=lambda prompt: "", output_fn=lambda text: None)
    read = ReadTool(s, [{"path": "a.py"}]).call()
    key = s.register_source_drafts(list(read.drafts))[0]
    call = ToolCall("edit-1", "Edit", ["a.py", key, [{"op": "replace", "start": 1, "end": 1, "content": "new\n"}]])
    status, _, observation = await runner.run_one(call)

    assert status == "ok"
    assert observation is None
    assert len(s.turn_diffs) == 1
    td = s.turn_diffs[0]
    assert (td.path, td.turn, td.round) == ("a.py", 1, 1)
    assert td.key.startswith("tr.")
    assert "-old" in td.diff
    assert "+new" in td.diff


async def test_session_snapshot_turn_diff_roundtrip(tmp_path):
    s = session(tmp_path)
    s.messages.append({"role": "user", "content": "seed"})
    s.store_turn_diff("tr.1", 2, "x.py", "-a\n+b\n", round=1)

    uid = await s.save_snapshot()
    loaded = SessionSnapshotStore.load(uid, s.config, s.settings)

    assert [(diff.path, diff.diff, diff.turn, diff.round) for diff in loaded.turn_diffs] == [("x.py", "-a\n+b\n", 2, 1)]


async def test_resume_redraws_the_recorded_edit_diff(tmp_path):
    """A receipt is the only record of what an Edit did, so a resumed session redraws it from the
    snapshot: the file on disk has moved on, and the redraw must not follow it."""
    s = session(tmp_path)
    (tmp_path / "x.py").write_text("changed since\n", encoding="utf-8")
    s.messages.extend(
        [
            {"role": "user", "content": "edit it"},
            {
                "role": "assistant",
                "content": "doing it",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Edit", "arguments": '{"path": "x.py"}'}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "tool tr.1 Edit x.py\noutput:\nupdated"},
        ]
    )
    s.transcript_messages.extend(s.messages)
    s.store_tool_result("Edit", ["x.py"], "updated")
    s.store_turn_diff("tr.1", 1, "x.py", "--- x.py\n+++ x.py\n@@ -1 +1 @@\n-old\n+new\n", round=1)
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    restored = load_session(s.uid, config=s.config, settings=s.settings, cwd=str(tmp_path))
    output = []
    CommandLoop(Agent(restored, output_fn=output.append), output_fn=output.append).resume.render_resumed_session()
    text = "\n".join(str(item) for item in output)

    assert "-old" in text and "+new" in text
    assert "changed since" not in text
