"""Shared-workspace file transactions preserve edits across independent tool workers."""

import builtins
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from agent_harness import call, session

from wizolt.base import ToolError
from wizolt.tools import EditTool
from wizolt.tools.editplan import EditBatchPlan


@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("alias", [False, True])
async def test_concurrent_file_edits_serialize_validation_with_write(tmp_path, monkeypatch, planned, alias):
    target = tmp_path / "shared.txt"
    target.write_text("one\ntwo\n")
    sibling_path = tmp_path / "alias.txt" if alias else target
    if alias:
        sibling_path.symlink_to(target)
    first_session, second_session = session(tmp_path), session(tmp_path)
    first = EditTool(first_session, [target.name, "", [{"old": "one\n", "content": "ONE\n"}]])
    second = EditTool(second_session, [sibling_path.name, "", [{"old": "two\n", "content": "TWO\n"}]])
    if planned:
        first_plan = await EditBatchPlan(first_session).build([call("Edit", first.args)])
        second_plan = await EditBatchPlan(second_session).build([call("Edit", second.args)])
        first_call = first_plan.planned["Edit-id"].transact
        second_call = second_plan.planned["Edit-id"].transact
    else:
        first_call, second_call = first.call, second.call

    writing, release, second_started = threading.Event(), threading.Event(), threading.Event()
    real_open = builtins.open

    def gated_open(path, mode="r", *args, **kwargs):
        if str(path) == str(target) and mode == "w":
            writing.set()
            assert release.wait(3), "test did not release the first writer"
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", gated_open)

    def run_second():
        second_started.set()
        return second_call()

    with ThreadPoolExecutor(max_workers=3) as pool:
        first_result = pool.submit(first_call)
        assert writing.wait(3)
        second_result = pool.submit(run_second)
        try:
            assert second_started.wait(3)
            # Another path must remain writable while the first transaction is held open.
            other = EditTool(second_session, ["other.txt", "", [{"op": "create", "content": "independent\n"}]])
            pool.submit(other.call).result(timeout=3)
            # Give the second worker a chance to expose the old check/write race. It must wait
            # for the first writer, including when it uses a symlink to the same file.
            done = threading.Event()
            second_result.add_done_callback(lambda _: done.set())
            assert not done.wait(0.1)
        finally:
            release.set()
        first_result.result(timeout=3)
        if planned:
            with pytest.raises(ToolError, match="stale"):
                second_result.result(timeout=3)
            assert target.read_text() == "ONE\ntwo\n"
        else:
            second_result.result(timeout=3)
            assert target.read_text() == "ONE\nTWO\n"
