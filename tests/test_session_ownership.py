"""Exclusive session ownership: leases, contention, and the mutation boundaries they cover.

Mutual exclusion is exercised across real processes with explicit pipes and events; `flock` is
never mocked, and no test coordinates a race with a sleep. The lease contract is one owner per
session family -- a parent log and its `<uid>.w` worker -- for as long as the runtime lives.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from wizolt.base import SESSION_EVENT_KEY, WizoltError
from wizolt.cli import CommandLoop
from wizolt.cli import commands as commands_mod
from wizolt.config import Config
from wizolt.engine import Agent
from wizolt.session import (
    Session,
    SessionBusyError,
    SessionLease,
    SessionOwnershipError,
    SessionSnapshotStore,
)
from wizolt.session.store import SnapshotWritePlan
from wizolt.tui import TuiApp

LOAD_CHILD = textwrap.dedent(
    """
    import json, sys
    from wizolt.base import SESSION_EVENT_KEY
    from wizolt.config import Config
    from wizolt.session import Session, SessionBusyError
    data_dir, uid, cwd = sys.argv[1:4]
    def spoken(session):
        return [m.get("content") for m in session.messages if m.get("role") == "user" and not m.get(SESSION_EVENT_KEY)]
    try:
        session = Session.load_snapshot(uid, config=Config(data_dir=data_dir), cwd=cwd)
    except SessionBusyError as error:
        print(json.dumps({"status": "busy", "uid": error.uid}))
    except Exception as error:
        print(json.dumps({"status": "error", "detail": repr(error)}))
    else:
        print(json.dumps({"status": "ok", "messages": spoken(session)}))
        session.close()
    """
)

LOAD_AFTER_SIGNAL_CHILD = textwrap.dedent(
    """
    import json, sys
    from wizolt.base import SESSION_EVENT_KEY
    from wizolt.config import Config
    from wizolt.session import Session
    data_dir, uid, cwd = sys.argv[1:4]
    sys.stdin.readline()
    session = Session.load_snapshot(uid, config=Config(data_dir=data_dir), cwd=cwd)
    print(json.dumps([m.get("content") for m in session.messages if m.get("role") == "user" and not m.get(SESSION_EVENT_KEY)]), flush=True)
    session.close()
    """
)

HOLD_CHILD = textwrap.dedent(
    """
    import sys, time
    from wizolt.config import Config
    from wizolt.session import Session
    data_dir, uid, cwd = sys.argv[1:4]
    session = Session.load_snapshot(uid, config=Config(data_dir=data_dir), cwd=cwd)
    print("ready", flush=True)
    time.sleep(30)
    """
)


def config_for(tmp_path, name="data") -> Config:
    return Config(data_dir=str(tmp_path / name))


def stored_session(tmp_path, text="hello", *, uid="") -> Session:
    session = Session(cwd=str(tmp_path), config=config_for(tmp_path), uid=uid)
    session.messages.append({"role": "user", "content": text})
    return session


def run_child(source: str, *args: str) -> subprocess.Popen:
    """Start an independent interpreter; stdin stays a pipe the caller can signal through."""

    return subprocess.Popen(
        [sys.executable, "-c", source, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def child_result(process: subprocess.Popen) -> dict:
    out, err = process.communicate(timeout=30)
    assert process.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def append_text(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8") as file:
        file.write(text)


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as file:
        return file.read()


def read_records(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as file:
        return [json.loads(line) for line in file]


def async_callable(fn):
    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


def picker_loop(session, tmp_path):
    loop = CommandLoop(Agent(session, output_fn=lambda _text: None), input_fn=lambda prompt: "", output_fn=lambda _text: None)
    loop.tui = TuiApp()
    loop.interactive_input = True
    return loop


async def test_a_second_process_and_object_fail_while_a_different_session_opens(tmp_path):
    owner = stored_session(tmp_path, "owned")
    await owner.save_snapshot()
    other = stored_session(tmp_path, "other")
    await other.save_snapshot()
    other.close()

    busy = child_result(run_child(LOAD_CHILD, owner.config.data_dir, owner.uid, str(tmp_path)))
    assert busy == {"status": "busy", "uid": owner.uid}

    opened = child_result(run_child(LOAD_CHILD, other.config.data_dir, other.uid, str(tmp_path)))
    assert opened["status"] == "ok" and opened["messages"] == ["other"]

    with pytest.raises(SessionBusyError) as error:
        Session.load_snapshot(owner.uid, config=owner.config, cwd=str(tmp_path))
    assert error.value.uid == owner.uid
    assert str(error.value) == f"Session {owner.uid} is already in use by another Wizolt instance."


async def test_every_resume_alias_resolves_to_the_same_owned_identity(tmp_path):
    owner = stored_session(tmp_path, "alias me")
    await owner.save_snapshot()
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "data")

    for uid, config in (
        (owner.uid, owner.config),
        ("latest", owner.config),
        ("alias me", owner.config),
        (owner.uid, Config(data_dir=str(alias))),
    ):
        with pytest.raises(SessionBusyError):
            Session.load_snapshot(uid, config=config, cwd=str(tmp_path))


async def test_contended_open_leaves_every_file_untouched(tmp_path):
    owner = stored_session(tmp_path, "do not touch")
    await owner.save_snapshot()
    log = SessionSnapshotStore.session_path(owner.config.data_dir, owner.cwd, owner.uid)
    meta = SessionSnapshotStore.meta_path(owner.config.data_dir, owner.cwd, owner.uid)
    directory = os.path.dirname(log)
    before = {
        name: (os.path.getsize(os.path.join(directory, name)), os.stat(os.path.join(directory, name)).st_mtime_ns) for name in sorted(os.listdir(directory))
    }

    with pytest.raises(SessionBusyError):
        Session.load_snapshot(owner.uid, config=owner.config, cwd=str(tmp_path))

    after = {
        name: (os.path.getsize(os.path.join(directory, name)), os.stat(os.path.join(directory, name)).st_mtime_ns) for name in sorted(os.listdir(directory))
    }
    assert after == before
    assert os.path.isfile(log) and os.path.isfile(meta)
    assert not os.path.exists(log[: -len(".jsonl")] + ".assets")


async def test_resume_decodes_only_after_acquisition(tmp_path):
    owner = stored_session(tmp_path, "first")
    await owner.save_snapshot()
    child = run_child(LOAD_AFTER_SIGNAL_CHILD, owner.config.data_dir, owner.uid, str(tmp_path))
    try:
        # The child is parked on stdin, so the only thing that can free it is this release.
        owner.messages.append({"role": "user", "content": "second"})
        await owner.save_snapshot()
        owner.close()
        child.stdin.write("go\n")
        child.stdin.flush()
        out, err = child.communicate(timeout=30)
        assert child.returncode == 0, err
        assert json.loads(out.strip()) == ["first", "second"]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


async def test_close_permits_immediate_reopen_and_rejects_later_writes(tmp_path):
    owner = stored_session(tmp_path)
    await owner.save_snapshot()
    plan = SessionSnapshotStore(owner).plan()
    assert plan is not None
    owner.close()
    owner.close()  # idempotent

    with pytest.raises(SessionOwnershipError):
        await owner.save_snapshot()
    with pytest.raises(SessionOwnershipError):
        plan.execute()

    reopened = Session.load_snapshot(owner.uid, config=owner.config, cwd=str(tmp_path))
    assert reopened.uid == owner.uid


async def test_a_killed_owner_leaves_a_reusable_lock_file(tmp_path):
    owner = stored_session(tmp_path, "crash me")
    await owner.save_snapshot()
    owner.close()
    process = run_child(HOLD_CHILD, owner.config.data_dir, owner.uid, str(tmp_path))
    try:
        assert process.stdout.readline().strip() == "ready"
        process.kill()
        process.wait(timeout=10)
        lock_files = os.listdir(os.path.join(owner.config.data_dir, "session-locks"))
        assert lock_files  # the lock file is deliberately left behind
        reopened = Session.load_snapshot(owner.uid, config=owner.config, cwd=str(tmp_path))
        assert reopened.uid == owner.uid
        reopened.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_a_forked_child_does_not_retain_the_lease(tmp_path):
    """A fork child inherits the descriptor; the after-fork hook closes its copy without LOCK_UN."""

    owner = Session(cwd=str(tmp_path), config=config_for(tmp_path))
    owner.messages.append({"role": "user", "content": "fork"})
    asyncio.run(owner.save_snapshot())
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the forked child
        try:
            os.close(ready_read)
            os.write(ready_write, b"1")
            os.close(ready_write)
            time.sleep(5)
        finally:
            os._exit(0)
    os.close(ready_write)
    try:
        assert os.read(ready_read, 1) == b"1"
        owner.close()
        # The child is still alive and holds a copy of the descriptor; if it had kept the lock,
        # this acquisition would fail.
        reopened = Session.load_snapshot(owner.uid, config=owner.config, cwd=str(tmp_path))
        assert reopened.uid == owner.uid
        reopened.close()
    finally:
        os.close(ready_read)
        os.kill(pid, 9)
        os.waitpid(pid, 0)


async def test_a_surviving_subprocess_does_not_retain_the_lease(tmp_path):
    """The lease descriptor is non-inheritable, so a long-lived child cannot keep the lock alive."""

    owner = stored_session(tmp_path, "spawn")
    await owner.save_snapshot()
    child = run_child("import sys, time; sys.stdin.readline(); time.sleep(5)")
    try:
        owner.close()
        # The child is alive and owns a pipe, but not the lease descriptor.
        reopened = Session.load_snapshot(owner.uid, config=owner.config, cwd=str(tmp_path))
        assert reopened.uid == owner.uid
        reopened.close()
    finally:
        child.stdin.write("go\n")
        child.stdin.flush()
        child.wait(timeout=10)


async def test_cancelling_an_accepted_write_keeps_the_session_exclusive(tmp_path, monkeypatch):
    owner = stored_session(tmp_path, "blocked")
    await owner.save_snapshot()  # a baseline exists; the blocked write is a delta
    entered, release = threading.Event(), threading.Event()
    real_execute = SnapshotWritePlan.execute

    def slow_execute(plan):
        entered.set()
        release.wait(5)
        return real_execute(plan)

    monkeypatch.setattr(SnapshotWritePlan, "execute", slow_execute)
    save = asyncio.ensure_future(owner.save_snapshot())
    await asyncio.to_thread(entered.wait, 5)
    save.cancel()
    await asyncio.sleep(0)

    with pytest.raises(SessionBusyError):
        Session.load_snapshot(owner.uid, config=owner.config, cwd=str(tmp_path))

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await save
    owner.close()
    monkeypatch.undo()
    reopened = Session.load_snapshot(owner.uid, config=owner.config, cwd=str(tmp_path))
    assert reopened.uid == owner.uid


async def test_busy_resume_keeps_the_current_run_and_a_reserved_handoff_leaves_no_gap(tmp_path, monkeypatch):
    current = stored_session(tmp_path, "current work")
    await current.save_snapshot()
    target = stored_session(tmp_path, "target work")
    await target.save_snapshot()

    loop = picker_loop(current, tmp_path)
    monkeypatch.setattr(commands_mod, "choice_application", async_callable(lambda *_args, **_kwargs: target.uid))

    # The target still owns itself, so the switch is an ordinary command error and current stays.
    message = await commands_mod.sessions_command(loop, "")
    assert message == f"Session {target.uid} is already in use by another Wizolt instance."
    assert loop.resume_request == "" and loop.resume_lease is None

    target.close()
    assert await commands_mod.sessions_command(loop, "") is None
    assert loop.resume_request == target.uid
    reserved = loop.resume_lease
    assert reserved is not None and not reserved.closed
    # The target is already owned before the handoff, so there is no release-and-reacquire window.
    with pytest.raises(SessionBusyError):
        Session.load_snapshot(target.uid, config=target.config, cwd=str(tmp_path))

    current.close()
    handed = Session.load_snapshot(target.uid, config=target.config, cwd=str(tmp_path), lease=reserved)
    assert handed.uid == target.uid


async def test_a_failed_reserved_load_releases_its_lease(tmp_path):
    session = stored_session(tmp_path)
    await session.save_snapshot()
    session.close()
    path = SessionSnapshotStore.session_path(session.config.data_dir, session.cwd, session.uid)
    await asyncio.to_thread(append_text, path, "{not json}\n")
    reserved = SessionLease.acquire(session.config.data_dir, path)

    # Existing corruption diagnostics are preserved; the lease is released either way.
    with pytest.raises(ValueError):
        Session.load_snapshot(session.uid, config=session.config, cwd=str(tmp_path), lease=reserved)
    assert reserved.closed


async def test_opposite_switches_do_not_deadlock(tmp_path):
    first = stored_session(tmp_path, "first")
    await first.save_snapshot()
    second = stored_session(tmp_path, "second")
    await second.save_snapshot()

    with pytest.raises(SessionBusyError):
        SessionLease.acquire(first.config.data_dir, SessionSnapshotStore.session_path(second.config.data_dir, second.cwd, second.uid))
    with pytest.raises(SessionBusyError):
        SessionLease.acquire(second.config.data_dir, SessionSnapshotStore.session_path(first.config.data_dir, first.cwd, first.uid))


async def test_cleanup_skips_a_live_family_and_deletes_it_after_release(tmp_path):
    parent = stored_session(tmp_path, "parent")
    await parent.save_snapshot()
    worker = Session(cwd=str(tmp_path), config=parent.config, settings=parent.settings, uid=parent.uid + ".w", listed=False)
    worker.messages.append({"role": "user", "content": "worker"})
    worker.borrow_ownership(parent)
    await worker.save_snapshot()
    directory = SessionSnapshotStore.project_dir(parent.config.data_dir, parent.cwd)
    stale = time.time() - 30 * 86400
    for name in (parent.uid + ".jsonl", worker.uid + ".jsonl"):
        os.utime(os.path.join(directory, name), (stale, stale))
    current = stored_session(tmp_path, "current")
    current.settings.session_retention_days = 1

    # Old mtimes do not matter while the family is owned.
    assert SessionSnapshotStore.clean_expired(parent.config.data_dir, current.uid, 1) == 0
    assert os.path.isfile(os.path.join(directory, parent.uid + ".jsonl"))
    assert os.path.isfile(os.path.join(directory, worker.uid + ".jsonl"))

    parent.close()
    assert SessionSnapshotStore.clean_expired(parent.config.data_dir, current.uid, 1) == 2
    assert not os.path.exists(os.path.join(directory, parent.uid + ".jsonl"))
    assert not os.path.exists(os.path.join(directory, worker.uid + ".jsonl"))
    # The lock file survives the session it named.
    assert os.listdir(os.path.join(parent.config.data_dir, "session-locks"))


async def test_cleanup_races_a_resume_without_deleting_under_it(tmp_path):
    """A family another process is resuming stays; once that process dies, retention can delete it."""

    target = stored_session(tmp_path, "being resumed")
    await target.save_snapshot()
    directory = SessionSnapshotStore.project_dir(target.config.data_dir, target.cwd)
    path = os.path.join(directory, target.uid + ".jsonl")
    stale = time.time() - 30 * 86400
    os.utime(path, (stale, stale))
    target.close()
    current = stored_session(tmp_path, "current")
    holder = run_child(HOLD_CHILD, target.config.data_dir, target.uid, str(tmp_path))
    try:
        assert holder.stdout.readline().strip() == "ready"
        assert SessionSnapshotStore.clean_expired(target.config.data_dir, current.uid, 1) == 0
        assert os.path.isfile(path)
        holder.kill()
        holder.wait(timeout=10)
        assert SessionSnapshotStore.clean_expired(target.config.data_dir, current.uid, 1) == 1
        assert not os.path.exists(path)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


async def test_cleanup_deletes_an_orphan_worker_under_the_parent_identity(tmp_path):
    parent = stored_session(tmp_path, "parent")
    await parent.save_snapshot()
    worker = Session(cwd=str(tmp_path), config=parent.config, settings=parent.settings, uid=parent.uid + ".w", listed=False)
    worker.messages.append({"role": "user", "content": "worker"})
    worker.borrow_ownership(parent)
    await worker.save_snapshot()
    directory = SessionSnapshotStore.project_dir(parent.config.data_dir, parent.cwd)
    os.unlink(os.path.join(directory, parent.uid + ".jsonl"))  # orphan the worker
    parent.close()
    current = stored_session(tmp_path, "current")
    current.settings.session_retention_days = 1

    assert SessionSnapshotStore.clean_expired(parent.config.data_dir, current.uid, 1) == 1
    assert not os.path.exists(os.path.join(directory, worker.uid + ".jsonl"))


async def test_worker_borrows_the_parent_capability_and_cannot_release_it(tmp_path):
    parent = stored_session(tmp_path, "parent")
    await parent.save_snapshot()
    worker = Session(cwd=str(tmp_path), config=parent.config, settings=parent.settings, uid=parent.uid + ".w", listed=False)
    worker.messages.append({"role": "user", "content": "worker"})
    worker.borrow_ownership(parent)
    await worker.save_snapshot()
    worker.close()

    parent.assert_ownership()  # the borrowed lease is not the worker's to release
    parent.messages.append({"role": "user", "content": "more"})
    await parent.save_snapshot()

    foreign = Session(cwd=str(tmp_path), config=parent.config, settings=parent.settings, uid="other.w", listed=False)
    with pytest.raises(SessionOwnershipError):
        foreign.borrow_ownership(parent)

    with pytest.raises(WizoltError, match="worker session"):
        Session.load_snapshot(worker.uid, config=parent.config, cwd=str(tmp_path))


async def test_a_worker_cannot_write_after_its_parent_capability_closes(tmp_path):
    parent = stored_session(tmp_path, "parent")
    await parent.save_snapshot()
    worker = Session(cwd=str(tmp_path), config=parent.config, settings=parent.settings, uid=parent.uid + ".w", listed=False)
    worker.messages.append({"role": "user", "content": "worker"})
    worker.borrow_ownership(parent)
    await worker.save_snapshot()
    parent.close()

    worker.messages.append({"role": "user", "content": "too late"})
    with pytest.raises(SessionOwnershipError):
        await worker.save_snapshot()


async def test_an_existing_snapshot_gains_a_lock_file_without_migration(tmp_path):
    """Snapshots written before this protocol load as they are; only a sidecar lock appears."""

    original = stored_session(tmp_path, "legacy")
    await original.save_snapshot()
    original.close()
    path = SessionSnapshotStore.session_path(original.config.data_dir, original.cwd, original.uid)
    before = await asyncio.to_thread(read_bytes, path)
    shutil.rmtree(os.path.join(original.config.data_dir, "session-locks"))  # as if written by an older build

    loaded = Session.load_snapshot(original.uid, config=original.config, cwd=str(tmp_path))
    spoken = [message.get("content") for message in loaded.messages if message.get("role") == "user" and not message.get(SESSION_EVENT_KEY)]
    assert spoken == ["legacy"]
    loaded.close()
    assert await asyncio.to_thread(read_bytes, path) == before
    assert os.listdir(os.path.join(original.config.data_dir, "session-locks"))


async def test_read_only_inspection_stays_available_while_owned(tmp_path):
    owner = stored_session(tmp_path, "inspect me")
    await owner.save_snapshot()

    entries = SessionSnapshotStore.list_sessions(owner.config.data_dir, owner.cwd)
    assert [entry.uid for entry in entries] == [owner.uid]
    assert SessionSnapshotStore.tail_summary(entries[0].path)[0][1] == "inspect me"


async def test_permission_failure_fails_closed_without_claiming_another_instance(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("permission checks do not apply to root")
    owner = stored_session(tmp_path)
    await owner.save_snapshot()
    owner.close()
    lock_dir = os.path.join(owner.config.data_dir, "session-locks")
    os.makedirs(lock_dir, exist_ok=True)
    locks = [os.path.join(lock_dir, name) for name in os.listdir(lock_dir)]
    for lock in locks:
        os.chmod(lock, 0o400)
    try:
        with pytest.raises(WizoltError) as error:
            Session.load_snapshot(owner.uid, config=owner.config, cwd=str(tmp_path))
        assert not isinstance(error.value, SessionBusyError)
    finally:
        for lock in locks:
            os.chmod(lock, 0o600)


async def test_a_fresh_uid_collision_cannot_truncate_an_existing_log(tmp_path):
    original = stored_session(tmp_path, "keep me")
    await original.save_snapshot()
    path = SessionSnapshotStore.session_path(original.config.data_dir, original.cwd, original.uid)
    before = await asyncio.to_thread(read_bytes, path)
    original.close()

    colliding = Session(cwd=str(tmp_path), config=original.config, uid=original.uid)
    colliding.messages.append({"role": "user", "content": "replacement"})
    with pytest.raises(WizoltError, match="already exists"):
        await colliding.save_snapshot()
    assert await asyncio.to_thread(read_bytes, path) == before


async def test_ownership_never_becomes_model_visible_state(tmp_path):
    from wizolt.session import SessionSnapshotCodec

    session = stored_session(tmp_path)
    marker_before = SessionSnapshotCodec.marker(session)
    session.ensure_ownership()
    assert SessionSnapshotCodec.marker(session) == marker_before
    await session.save_snapshot()
    assert "lease" not in marker_before and "ownership" not in marker_before
    records = await asyncio.to_thread(read_records, SessionSnapshotStore.session_path(session.config.data_dir, session.cwd, session.uid))
    assert all("lease" not in record and "ownership" not in record for record in records)
    assert all("lease" not in json.dumps(message) for message in session.messages)


@pytest.mark.parametrize("failure", ["bootstrap", "missing"])
async def test_failed_open_releases_reserved_ownership_at_every_stage(tmp_path, monkeypatch, failure):
    owner = stored_session(tmp_path)
    await owner.save_snapshot()
    owner.close()
    path = SessionSnapshotStore.session_path(owner.config.data_dir, owner.cwd, owner.uid)
    lease = SessionLease.acquire(owner.config.data_dir, path)
    if failure == "missing":
        os.unlink(path)
    else:

        def fail_bootstrap(_session):
            raise WizoltError("bootstrap failed")

        monkeypatch.setattr("wizolt.session.bootstrap_features", fail_bootstrap)
    with pytest.raises(WizoltError):
        Session.load_snapshot(owner.uid, config=owner.config, cwd=owner.cwd, lease=lease)
    assert lease.closed
    acquired = SessionLease.acquire(owner.config.data_dir, path)
    acquired.close()


@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
async def test_failed_handoff_save_does_not_reserve_or_request_a_switch(tmp_path, monkeypatch, failure):
    current = stored_session(tmp_path)
    await current.save_snapshot()
    target = stored_session(tmp_path, "target")
    await target.save_snapshot()
    target.close()
    loop = picker_loop(current, tmp_path)
    monkeypatch.setattr(commands_mod, "choice_application", async_callable(lambda *_args, **_kwargs: target.uid))

    async def fail_save():
        raise failure()

    monkeypatch.setattr(loop, "save_and_emit_resume", fail_save)
    try:
        with pytest.raises(failure):
            await commands_mod.sessions_command(loop, "")
        assert loop.resume_request == "" and loop.resume_lease is None
        opened = Session.load_snapshot(target.uid, config=target.config, cwd=target.cwd)
        opened.close()
    finally:
        current.close()


async def test_raw_snapshot_inspection_cannot_later_write_a_stale_baseline(tmp_path):
    owner = stored_session(tmp_path)
    await owner.save_snapshot()
    inspected = SessionSnapshotStore.load(owner.uid, owner.config, owner.settings, cwd=owner.cwd)
    owner.messages.append({"role": "user", "content": "newer"})
    await owner.save_snapshot()
    owner.close()
    with pytest.raises(SessionOwnershipError):
        await inspected.save_snapshot()


async def test_a_write_plan_cannot_target_a_different_session(tmp_path):
    from dataclasses import replace

    owner = stored_session(tmp_path)
    await owner.save_snapshot()
    plan = SessionSnapshotStore(owner).plan()
    target = tmp_path / "unowned.jsonl"
    target.write_text("keep me")
    try:
        with pytest.raises(SessionOwnershipError):
            replace(plan, log_path=str(target)).execute()
        assert target.read_text() == "keep me"
    finally:
        owner.close()


async def test_close_cannot_release_ownership_during_an_agent_request(tmp_path):
    owner = stored_session(tmp_path)
    agent = Agent(owner, output_fn=lambda _: None)
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}, [], "done"

    agent.model.request = request
    task = asyncio.create_task(agent.run("work"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(WizoltError, match="agent run is active"):
            owner.close()
        with pytest.raises(SessionBusyError):
            SessionLease.acquire(owner.config.data_dir, owner.ownership_root_path())
    finally:
        release.set()
        await task
        owner.close()


async def test_main_releases_a_reserved_target_when_config_reload_fails(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import wizolt.__main__ as cli
    from wizolt.config import ConfigError

    current = stored_session(tmp_path, "current")
    await current.save_snapshot()
    target = stored_session(tmp_path, "target")
    await target.save_snapshot()
    target.close()
    reserved = SessionLease.acquire(target.config.data_dir, target.ownership_root_path())
    monkeypatch.setattr(cli.Session, "from_config_file", lambda **_: current)
    monkeypatch.setattr(cli, "warm_provider_sdks", lambda: None)
    monkeypatch.setattr(
        cli,
        "CommandLoop",
        lambda _: SimpleNamespace(
            run=lambda **_: 0,
            close_background_output=lambda: None,
            resume_request=target.uid,
            resume_lease=reserved,
        ),
    )

    def fail_config(*_args):
        raise ConfigError("broken config")

    monkeypatch.setattr(cli.ConfigFile, "load", fail_config)
    assert cli.main([]) == 2
    assert reserved.closed
    assert current._lease is None


async def test_a_mismatched_reserved_lease_does_not_unlock_its_owner(tmp_path):
    owner = stored_session(tmp_path)
    target = stored_session(tmp_path, "target")
    await owner.save_snapshot()
    await target.save_snapshot()
    target.close()
    try:
        with pytest.raises(SessionOwnershipError):
            Session.load_snapshot(target.uid, config=target.config, cwd=target.cwd, lease=owner.assert_ownership())
        owner.assert_ownership()
        with pytest.raises(SessionBusyError):
            Session.load_snapshot(owner.uid, config=owner.config, cwd=owner.cwd)
    finally:
        owner.close()
