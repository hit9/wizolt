"""session load (split from tests/test_session_persistence.py)."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from test_session_persistence import log_path, project_dir, read_jsonl, read_lines, rewrite_log, session_with_data_dir, visible_contents, write_log

from wizolt.agent.lifecycle import load_session
from wizolt.base import SESSION_EVENT_KEY, WizoltError
from wizolt.config import (
    Config,
    RuntimeSettings,
)
from wizolt.session import Session, SessionSnapshotCodec, SessionSnapshotStore, TurnDiff


async def test_compact_deltas_keep_agent_metadata_and_clear_default_state(tmp_path):
    s = session_with_data_dir(tmp_path)
    s.messages = [{"role": "user", "content": "hello"}]
    s.subagent_entries = [{"uid": s.uid + ".achild", "parent": s.uid, "instruction": "review"}]
    s.state.last_turn_status = "failed"
    s.state.last_turn_error = "old error"
    s.state.turn_result = {"result_id": "old"}
    s.state.child_results_seen = {"child": "old"}
    try:
        await s.save_snapshot()
        await s.save_snapshot()
        unchanged = read_jsonl(log_path(s))[-1]
        assert not {"agent_name", "agent_parent", "subagent_entries"} & unchanged.keys()
        loaded = SessionSnapshotStore.load(s.uid, config=s.config, settings=s.settings)
        assert loaded.subagent_entries == s.subagent_entries
        loaded.close()

        s.agent_name = "renamed"
        s.subagent_entries.clear()
        s.state.last_turn_status = "idle"
        s.state.last_turn_error = ""
        s.state.turn_result.clear()
        s.state.child_results_seen.clear()
        await s.save_snapshot()
        cleared = read_jsonl(log_path(s))[-1]
        assert cleared["subagent_entries"] == []
        assert "last_turn_error" not in cleared["state"]
        loaded = SessionSnapshotStore.load(s.uid, config=s.config, settings=s.settings)
        assert loaded.agent_name == "renamed" and loaded.subagent_entries == []
        assert loaded.state.last_turn_status == "idle" and loaded.state.last_turn_error == ""
        assert loaded.state.turn_result == loaded.state.child_results_seen == {}
        loaded.close()
    finally:
        s.close()


def test_read_merged_skips_a_torn_record_and_non_object_lines(tmp_path):
    """A crash (or a full disk) mid-append leaves a torn trailing line: no newline, never
    committed -- the write markers do not advance on a failed append, so the next save re-appends
    the same record. Reading must skip the fragment instead of failing every later load of the
    log, while a complete line that is not valid JSON stays a corruption error."""
    s = session_with_data_dir(tmp_path)
    header = SessionSnapshotStore.header(s)
    snapshot = {"messages": [{"role": "user", "content": "hello"}], "transcript_sync": 1}
    delta = {"messages": [{"role": "user", "content": "appended"}]}
    intact = "\n".join(json.dumps(record) for record in (header, snapshot, delta)) + "\n"
    path = tmp_path / "log.jsonl"
    path.write_text(intact + '{"messages": [{"role": "user", "content": "tor')

    merged, _blobs, hdr = SessionSnapshotStore.read_merged(str(path))

    assert [item["content"] for item in merged["messages"]] == ["hello", "appended"]
    assert hdr["v"] == SessionSnapshotStore.FORMAT_VERSION

    # a complete line that fails to parse is committed corruption, not a torn tail
    path.write_text(intact + "{not json}\n")
    with pytest.raises(json.JSONDecodeError):
        SessionSnapshotStore.read_merged(str(path))


def test_read_merged_refuses_a_truncated_header(tmp_path):
    """A torn first line leaves no header to check, so the log is refused cleanly rather than
    surfacing a JSONDecodeError from inside the loader."""
    path = tmp_path / "log.jsonl"
    path.write_text('{"v": 3, "uid": "tor')

    with pytest.raises(WizoltError, match="Corrupt session file header"):
        SessionSnapshotStore.read_merged(str(path))


@pytest.mark.parametrize("tail", [b'{"messages": [', b'{"blob": "' + b'x' * 20000, b'{"text": "\xe4\xb8'])
async def test_save_retry_removes_uncommitted_tail_before_appending(tmp_path, tail):
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "first"})
    await s.save_snapshot()
    path = SessionSnapshotStore.session_path(s.config.data_dir, s.cwd, s.uid)
    def append_tail():
        with open(path, "ab") as file:
            file.write(tail)

    await asyncio.to_thread(append_tail)
    s.messages.append({"role": "user", "content": "second"})
    await s.save_snapshot()
    merged, _, _ = SessionSnapshotStore.read_merged(path)
    assert [m["content"] for m in merged["messages"]] == ["first", "second"]


@pytest.mark.parametrize("status", ["running", "completed", "interrupted", "failed"])
async def test_reloaded_turn_status_only_normalizes_unfinished_work(tmp_path, status):
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "task"})
    s.state.last_turn_status = status
    await s.save_snapshot()
    restored = SessionSnapshotStore.load(s.uid, config=s.config, settings=s.settings, cwd=s.cwd)
    assert restored.state.last_turn_status == ("interrupted" if status == "running" else status)


@pytest.mark.parametrize("reverse", [False, True])
def test_restore_applies_replacements_before_appends_and_ignores_unknown_fields(tmp_path, reverse):
    path = tmp_path / "log.jsonl"
    fields = [("messages", [{"role": "user", "content": "appended"}]),
              ("messages_replace", [{"role": "user", "content": "replacement"}]),
              ("transcript_messages", [{"role": "assistant", "content": "visible"}]),
              ("unexpected_replace", ["bad"]), ("unexpected", ["bad"]),
              ("state", {"goal": "restored"}), ("transcript_sync", 1)]
    if reverse:
        fields.reverse()
    s = session_with_data_dir(tmp_path)
    header = SessionSnapshotStore.header(s)
    snapshot = {"messages": [{"role": "user", "content": "old"}], "transcript_sync": 1}
    path.write_text("\n".join(json.dumps(record) for record in (header, snapshot, dict(fields))) + "\n")
    restored, _, _ = SessionSnapshotStore.read_merged(str(path))
    assert [item["content"] for item in restored["messages"]] == ["replacement", "appended"]
    assert restored["transcript_messages"] == [{"role": "assistant", "content": "visible"}]
    assert restored["state"] == {"goal": "restored"}
    assert "unexpected" not in restored and "unexpected_replace" not in restored


async def test_oversized_snapshots_are_dropped_before_reaching_the_log(tmp_path):
    """Snapshots over the size limit are still discarded, and leave no blob behind."""
    s = session_with_data_dir(tmp_path)
    huge = "x" * (TurnDiff.SNAPSHOT_CHAR_LIMIT + 1)
    s.store_turn_diff("tr.1", 1, "big.py", "-o\n+n\n", before=huge, after=huge, round=1)
    await s.save_snapshot()

    lines = read_lines(log_path(s))
    entry = [line for line in lines if "turn_diffs" in line][-1]["turn_diffs"][0]

    assert not [line for line in lines if "blob" in line]
    assert (entry["before_blob"], entry["after_blob"]) == ("", "")
    s.close()  # release the writer before reloading
    assert load_session(s.uid, config=s.config, cwd=str(tmp_path)).turn_diffs[0].before == ""

async def test_rewriting_the_retained_window_does_not_rewrite_snapshots(tmp_path):
    """Once the 100-entry cap starts evicting, every save rewrites the whole window. It must
    rewrite references only — the snapshots are already in the log."""
    s = session_with_data_dir(tmp_path)
    big = "x" * 100_000
    for i in range(100):
        s.store_turn_diff(f"tr.{i}", i, "a.py", "-o\n+n\n", before=big, after=big + str(i), round=i)
    await s.save_snapshot()
    size_before = os.path.getsize(log_path(s))

    s.store_turn_diff("tr.100", 100, "a.py", "-o\n+n\n", before=big, after=big + "100", round=100)
    await s.save_snapshot()

    lines = read_lines(log_path(s))
    assert "turn_diffs_replace" in lines[-1]  # the window was rewritten in full
    assert len(lines[-1]["turn_diffs_replace"]) == 100
    # One new snapshot (~100KB), not 100 of them (~10MB).
    assert os.path.getsize(log_path(s)) - size_before < 400_000

async def test_resumed_session_does_not_rewrite_existing_blobs(tmp_path):
    """A resumed session knows which snapshots its log already holds."""
    s = session_with_data_dir(tmp_path)
    s.store_turn_diff("tr.1", 1, "x.py", "-old\n+new\n", before="old\n", after="new\n", round=1)
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    restored = load_session(s.uid, config=s.config, cwd=str(tmp_path))
    restored.store_turn_diff("tr.2", 2, "x.py", "-new\n+newer\n", before="new\n", after="newer\n", round=2)
    await restored.save_snapshot()

    blobs = [line["text"] for line in read_lines(log_path(s)) if "blob" in line]
    assert sorted(blobs) == ["new\n", "newer\n", "old\n"]  # "new\n" not stored a second time

async def test_load_merges_init_and_deltas(tmp_path):
    """load_snapshot reads and merges all lines, returning the full session state."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "q1"})
    s.store_tool_result("Read", ["f.py"], "# f")
    await s.save_snapshot()  # init

    s.messages.append({"role": "assistant", "content": "a1"})
    s.store_tool_result("Search", ["pat"], "found")
    await s.save_snapshot()  # delta

    s.messages.append({"role": "user", "content": "q2"})
    await s.save_snapshot()  # delta (no new tool results)

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    # All messages across all lines
    assert [m["content"] for m in s2.messages[:3]] == ["q1", "a1", "q2"]
    # Fourth message is a durable user-role resume event.
    assert s2.messages[3]["role"] == "user"
    assert s2.messages[3][SESSION_EVENT_KEY] == "resumed"
    assert s2.messages[3]["content"].startswith('<session_event type="resumed" at="')
    # All tool results
    assert s2.tool_results["tr.1"] == "# f"
    assert s2.tool_results["tr.2"] == "found"
    assert s2.tool_counter == 2

async def test_load_preserves_uid(tmp_path):
    """load_snapshot preserves the original uid."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    assert s2.uid == s.uid

async def test_load_with_latest_alias(tmp_path):
    """load_snapshot with uid='latest' resolves this project's latest pointer."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    s2 = load_session("latest", config=s.config, cwd=str(tmp_path))
    assert s2.uid == s.uid

async def test_load_with_last_alias(tmp_path):
    """load_snapshot with uid='last' resolves this project's latest pointer."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    s2 = load_session("last", config=s.config, cwd=str(tmp_path))
    assert s2.uid == s.uid

async def test_latest_uid_ignores_newer_sessions_from_other_projects(tmp_path):
    data_dir = tmp_path / "data"
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    config = Config(data_dir=str(data_dir))

    project_session = Session(cwd=str(project), config=config)
    project_session.messages.append({"role": "user", "content": "project"})
    await project_session.save_snapshot()

    other_session = Session(cwd=str(other), config=config)
    other_session.messages.append({"role": "user", "content": "other"})
    await other_session.save_snapshot()
    os.utime(log_path(project_session), (1, 1))
    os.utime(log_path(other_session), (2, 2))

    assert SessionSnapshotStore.latest_uid(str(data_dir), str(project)) == project_session.uid
    assert SessionSnapshotStore.latest_uid(str(data_dir), str(other)) == other_session.uid

def test_latest_uid_returns_empty_without_project_session(tmp_path):
    assert SessionSnapshotStore.latest_uid(str(tmp_path / "missing"), str(tmp_path)) == ""

async def test_sessions_are_sharded_per_project(tmp_path):
    """Two projects sharing a data_dir get separate directories, so listing or deleting one
    project's history never touches the other's."""
    data_dir, project, other = tmp_path / "data", tmp_path / "project", tmp_path / "other"
    project.mkdir()
    other.mkdir()
    config = Config(data_dir=str(data_dir))

    first = Session(cwd=str(project), config=config)
    first.messages.append({"role": "user", "content": "project"})
    await first.save_snapshot()
    second = Session(cwd=str(other), config=config)
    second.messages.append({"role": "user", "content": "other"})
    await second.save_snapshot()

    assert project_dir(first) != project_dir(second)
    assert sorted(os.listdir(project_dir(first))) == sorted(["latest", first.uid + ".jsonl", first.uid + ".meta.json"])
    assert sorted(os.listdir(project_dir(second))) == sorted(["latest", second.uid + ".jsonl", second.uid + ".meta.json"])

def test_project_slug_separates_same_named_directories(tmp_path):
    """Two checkouts named alike hash to different shards."""
    left, right = tmp_path / "a" / "repo", tmp_path / "b" / "repo"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    slugs = [SessionSnapshotStore.project_slug(str(path)) for path in (left, right)]

    assert all(slug.startswith("repo-") for slug in slugs)
    assert slugs[0] != slugs[1]

async def test_load_finds_a_session_by_uid_from_any_directory(tmp_path):
    """An explicit UID resolves regardless of which project it belongs to."""
    data_dir, project = tmp_path / "data", tmp_path / "project"
    project.mkdir()
    config = Config(data_dir=str(data_dir))
    s = Session(cwd=str(project), config=config)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    loaded = load_session(s.uid, config=config, cwd=str(tmp_path))

    assert loaded.uid == s.uid
    assert loaded.cwd == str(project)

async def test_latest_never_crosses_into_another_project(tmp_path):
    """The regression the shard layout closes: a newer session elsewhere must not be resumable
    as this project's latest."""
    data_dir, project, other = tmp_path / "data", tmp_path / "project", tmp_path / "other"
    project.mkdir()
    other.mkdir()
    config = Config(data_dir=str(data_dir))
    elsewhere = Session(cwd=str(other), config=config)
    elsewhere.messages.append({"role": "user", "content": "other"})
    await elsewhere.save_snapshot()

    with pytest.raises(WizoltError, match="No previous session for this project"):
        load_session("latest", config=config, cwd=str(project))

async def test_latest_falls_back_to_newest_log_when_pointer_is_missing(tmp_path):
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()
    os.unlink(os.path.join(project_dir(s), "latest"))

    assert SessionSnapshotStore.latest_uid(str(tmp_path), str(tmp_path)) == s.uid

@pytest.mark.parametrize("pointer", ["missing", "stale", "child"])
async def test_latest_never_resolves_a_child_snapshot(tmp_path, pointer):
    root = session_with_data_dir(tmp_path)
    child = Session(uid=root.uid + ".a0123456789ab", cwd=root.cwd, config=root.config, agent_parent=root.uid, listed=False)
    child.borrow_ownership(root)
    try:
        root.messages.append({"role": "user", "content": "parent request"})
        child.messages.append({"role": "user", "content": "child task"})
        await root.save_snapshot()
        await child.save_snapshot()
        os.utime(log_path(root), (100, 100))
        os.utime(log_path(child), (200, 200))
        latest = os.path.join(project_dir(root), "latest")
        if pointer == "missing":
            os.unlink(latest)
        else:
            await asyncio.to_thread(Path(latest).write_text, child.uid if pointer == "child" else "nonexistent")
        assert SessionSnapshotStore.latest_uid(root.config.data_dir, root.cwd) == root.uid
        os.unlink(log_path(root))
        assert SessionSnapshotStore.latest_uid(root.config.data_dir, root.cwd) == ""
    finally:
        child.close()
        root.close()

async def test_header_line_precedes_the_snapshot(tmp_path):
    """Line 1 is a bounded header, so project queries never parse the conversation behind it."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()

    header = read_lines(log_path(s))[0]

    assert header == {"v": SessionSnapshotStore.FORMAT_VERSION, "uid": s.uid, "cwd": s.cwd, "created_at": header["created_at"]}
    assert header["created_at"] == s.created_at
    assert header["created_at"][-6:-5] in {"+", "-"}
    assert "messages" not in header

async def test_load_rejects_an_unknown_format_version(tmp_path):
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()
    lines = read_lines(log_path(s))
    lines[0]["v"] = 99
    await asyncio.to_thread(rewrite_log, log_path(s), lines)

    with pytest.raises(WizoltError, match="Unsupported session format v99"):
        s.close()  # release the writer before reloading
        load_session(s.uid, config=s.config)

async def test_load_appends_local_time_resume_event(tmp_path, monkeypatch):
    """Resume is durable user-role context with a local wall time and explicit offset."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()

    monkeypatch.setattr("wizolt.session.local_timestamp", lambda value=None: "2026-07-30T15:04:05+08:00")
    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    assert len(s2.messages) == 2  # hello + resume marker
    assert s2.messages[-1] == {
        "role": "user",
        "content": '<session_event type="resumed" at="2026-07-30T15:04:05+08:00" />',
        SESSION_EVENT_KEY: "resumed",
    }

async def test_save_after_load_produces_a_delta(tmp_path):
    """Save after load appends a delta (not re-init)."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()  # init (line 1)

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    # s2 now has messages = [hello, resume_marker]
    s2.messages.append({"role": "assistant", "content": "post-resume"})
    await s2.save_snapshot()  # delta (line 2)

    lines = read_jsonl(log_path(s))
    assert len(lines) == 2
    delta = lines[1]
    # Both the lifecycle event and subsequent assistant reply are new append-only history.
    assert delta["messages"][0][SESSION_EVENT_KEY] == "resumed"
    assert delta["messages"][1] == {"role": "assistant", "content": "post-resume"}

async def test_repeated_resume_preserves_history(tmp_path):
    """Repeated resume/save cycles keep appending new messages to the same history."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "m1"})
    await s.save_snapshot()

    expected = ["m1"]
    for role, content in (("assistant", "a1"), ("user", "m2"), ("assistant", "a2")):
        s.close()  # release the writer before reloading
        s = load_session(s.uid, config=s.config)
        assert visible_contents(s.messages) == expected
        s.messages.append({"role": role, "content": content})
        await s.save_snapshot()
        expected.append(content)

    s.close()  # release the writer before reloading
    loaded = load_session(s.uid, config=s.config)
    assert visible_contents(loaded.messages) == expected
    assert sum(message.get(SESSION_EVENT_KEY) == "resumed" for message in loaded.messages) == 4

async def test_resume_marker_is_never_persisted(tmp_path):
    """Resume markers are runtime-only, including when a message rewrite forces replace."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "m1"})
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    resumed = load_session(s.uid, config=s.config)
    resumed.messages = [
        {"role": "system", "content": f"[Session resumed: uid={s.uid}]"},
        {"role": "user", "content": "rewritten"},
    ]
    await resumed.save_snapshot()

    lines = read_jsonl(log_path(s))
    assert lines[-1]["messages_replace"] == [{"role": "user", "content": "rewritten"}]
    assert "[Session resumed:" not in json.dumps(lines)

def test_load_discards_persisted_resume_markers(tmp_path):
    """Older or malformed snapshots may contain resume markers; load should not keep them."""
    s = session_with_data_dir(tmp_path)
    path = log_path(s)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    marker = {"role": "system", "content": f"[Session resumed: uid={s.uid}]"}
    write_log(path, SessionSnapshotStore.header(s), mode="w")
    write_log(
        path,
        {
            "uid": s.uid,
            "cwd": str(tmp_path),
            "messages": [{"role": "user", "content": "m1"}, marker, {"role": "assistant", "content": "a1"}],
        },
        mode="a",
    )

    s.close()  # release the writer before reloading
    loaded = load_session(s.uid, config=s.config)

    assert visible_contents(loaded.messages) == ["m1", "a1"]
    assert sum(1 for m in loaded.messages if SessionSnapshotCodec.is_internal_message(m)) == 1

async def test_old_context_layout_migrates_with_one_full_state_checkpoint(tmp_path):
    s = session_with_data_dir(tmp_path)
    s.context_layout_version = 1
    s.state.goal = "ship cache layout"
    s.state.plan = [{"status": "doing", "text": "migrate"}]
    s.state.known = ["old snapshots have no layout field"]
    s.state.check = "one checkpoint"
    s.messages.append({"role": "user", "content": "original request"})
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    migrated = load_session(s.uid, config=s.config)
    checkpoints = [message for message in migrated.messages if message.get(SESSION_EVENT_KEY) == "state_checkpoint"]
    assert migrated.context_layout_version == 2
    assert len(checkpoints) == 1
    assert "Goal: ship cache layout" in checkpoints[0]["content"]
    assert "- doing: migrate" in checkpoints[0]["content"]
    assert "Check: one checkpoint" in checkpoints[0]["content"]

    await migrated.save_snapshot()
    migrated.close()  # release the writer before reloading
    resumed_again = load_session(s.uid, config=s.config)
    assert sum(message.get(SESSION_EVENT_KEY) == "state_checkpoint" for message in resumed_again.messages) == 1

def test_real_legacy_snapshot_without_layout_field_converts_numeric_local_time(tmp_path, monkeypatch):
    s = session_with_data_dir(tmp_path)
    path = log_path(s)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    legacy_created_at = 1_700_000_000.0
    write_log(
        path,
        {"v": SessionSnapshotStore.FORMAT_VERSION, "uid": s.uid, "cwd": s.cwd, "created_at": legacy_created_at},
        mode="w",
    )
    write_log(
        path,
        {
            "uid": s.uid,
            "cwd": s.cwd,
            "messages": [{"role": "user", "content": "legacy request"}],
            "state": {
                "goal": "migrate a real legacy record",
                "plan": [{"status": "doing", "text": "load"}],
                "known": ["context_layout_version is absent"],
                "check": "local timestamp converted",
            },
        },
        mode="a",
    )
    timestamp_calls = []

    def timestamp(value=None):
        timestamp_calls.append(value)
        return "2023-11-14T17:13:20-05:00" if value is not None else "2026-07-30T15:04:05+08:00"

    monkeypatch.setattr("wizolt.session.local_timestamp", timestamp)

    s.close()  # release the writer before reloading
    loaded = load_session(s.uid, config=s.config)

    assert timestamp_calls == [legacy_created_at, None]
    assert loaded.created_at == "2023-11-14T17:13:20-05:00"
    assert loaded.context_layout_version == 2
    checkpoint = next(message for message in loaded.messages if message.get(SESSION_EVENT_KEY) == "state_checkpoint")
    assert "Goal: migrate a real legacy record" in checkpoint["content"]
    assert checkpoint["content"].startswith("--- Working State Checkpoint ---")

async def test_empty_session_first_save_is_skipped(tmp_path):
    """A session with no recoverable content is not persisted."""
    s = session_with_data_dir(tmp_path)
    assert await s.save_snapshot() == ""

    assert not os.path.exists(project_dir(s))

async def test_tool_results_roundtrip(tmp_path):
    """Tool results survive save/load."""
    s = session_with_data_dir(tmp_path)
    s.store_tool_result("Bash", ["echo hi"], "hi")
    s.store_tool_result("Read", ["f.py"], "code")
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    assert s2.tool_results["tr.1"] == "hi"
    assert s2.tool_results["tr.2"] == "code"

async def test_tool_records_roundtrip(tmp_path):
    """Tool records survive save/load."""
    s = session_with_data_dir(tmp_path)
    s.store_tool_result("Bash", ["pwd"], "/tmp")
    s.store_tool_result("Search", ["x"], "match")
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    assert len(s2.tool_records) == 2
    assert s2.tool_records[0].key == "tr.1"
    assert s2.tool_records[0].name == "Bash"
    assert s2.tool_records[0].output == "/tmp"
    assert s2.tool_records[1].key == "tr.2"
    assert s2.tool_records[1].name == "Search"

async def test_tool_errors_roundtrip(tmp_path):
    """Tool errors survive save/load."""
    s = session_with_data_dir(tmp_path)
    s.record_tool_error("tr.1", "Bash", ["bad"], "command not found")
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    assert len(s2.tool_errors) == 1
    assert s2.tool_errors[0].key == "tr.1"
    assert s2.tool_errors[0].error == "command not found"

async def test_usage_roundtrip_with_prompt_and_completion_tokens(tmp_path):
    """All usage fields (including prompt_tokens/completion_tokens) survive save/load."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    s.usage.calls = 3
    s.usage.prompt_tokens = 100
    s.usage.completion_tokens = 50
    s.usage.total_tokens = 150
    s.usage.cached_prompt_tokens = 20
    s.usage.cache_write_prompt_tokens = 12
    s.usage.last_cached_prompt_tokens = 5
    s.usage.last_cache_write_prompt_tokens = 7
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    assert s2.usage.calls == 3
    assert s2.usage.prompt_tokens == 100
    assert s2.usage.completion_tokens == 50
    assert s2.usage.total_tokens == 150
    assert s2.usage.cached_prompt_tokens == 20
    assert s2.usage.cache_write_prompt_tokens == 12
    assert s2.usage.last_cached_prompt_tokens == 5
    assert s2.usage.last_cache_write_prompt_tokens == 7

async def test_agent_state_roundtrip(tmp_path):
    """Agent state (goal, plan, known, check, summary) survives save/load."""
    s = session_with_data_dir(tmp_path)
    s.state.goal = "fix bug"
    s.state.plan = ["step 1", "step 2"]
    s.state.known = ["file at src/a.py"]
    s.state.check = "assert x == 1"
    s.state.summary = "working on it"
    s.state.round_count = 7
    await s.save_snapshot()

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    assert s2.state.goal == "fix bug"
    assert [vars(item) for item in s2.state.plan] == [{"status": "todo", "text": "step 1"}, {"status": "todo", "text": "step 2"}]
    assert s2.state.known == ["file at src/a.py"]
    assert s2.state.check == "assert x == 1"
    assert s2.state.summary == "working on it"
    assert s2.state.round_count == 7

async def test_multiple_deltas_accumulate_correctly(tmp_path):
    """Multiple delta saves accumulate data correctly."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "m1"})
    await s.save_snapshot()  # init
    s.messages.append({"role": "assistant", "content": "a1"})
    await s.save_snapshot()  # delta 1
    s.messages.append({"role": "user", "content": "m2"})
    await s.save_snapshot()  # delta 2
    s.messages.append({"role": "assistant", "content": "a2"})
    await s.save_snapshot()  # delta 3

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    assert visible_contents(s2.messages) == ["m1", "a1", "m2", "a2"]

async def test_multiple_deltas_with_tool_calls(tmp_path):
    """Tool calls across multiple deltas accumulate correctly."""
    s = session_with_data_dir(tmp_path)
    s.store_tool_result("Read", ["a.py"], "# a")
    await s.save_snapshot()  # init: tr.1
    s.store_tool_result("Search", ["pat"], "hit")
    await s.save_snapshot()  # delta 1: tr.2
    s.store_tool_result("Bash", ["pwd"], "/tmp")
    await s.save_snapshot()  # delta 2: tr.3

    s.close()  # release the writer before reloading
    s2 = load_session(s.uid, config=s.config)
    assert s2.tool_results["tr.1"] == "# a"
    assert s2.tool_results["tr.2"] == "hit"
    assert s2.tool_results["tr.3"] == "/tmp"
    assert s2.tool_counter == 3
    assert len(s2.tool_records) == 3

def test_load_missing_snapshot_raises_error(tmp_path):
    """Loading a non-existent session raises WizoltError."""
    with pytest.raises(WizoltError, match="Session snapshot not found"):
        load_session("nonexistent-uid", config=Config(data_dir=str(tmp_path)))

@pytest.mark.parametrize("alias", ["latest", "last"])
def test_resolve_uid_without_a_project_session(tmp_path, alias):
    """Resolving an alias in a project with no sessions raises WizoltError."""
    with pytest.raises(WizoltError, match="No previous session for this project"):
        SessionSnapshotStore.resolve_uid(alias, str(tmp_path), str(tmp_path))

def test_resolve_uid_passthrough_normal_uid(tmp_path):
    """Resolving a normal uid (not an alias) returns it as-is."""
    assert SessionSnapshotStore.resolve_uid("my-uid", str(tmp_path), str(tmp_path)) == "my-uid"

async def test_jsonl_file_is_append_only(tmp_path):
    """Multiple saves only add lines, never rewrite the file."""
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "hello"})
    await s.save_snapshot()  # l1
    await s.save_snapshot()  # l2
    await s.save_snapshot()  # l3
    await s.save_snapshot()  # l4

    lines = read_jsonl(log_path(s))
    assert len(lines) == 4
    # First line has all fields (init)
    assert "uid" in lines[0]
    assert "messages" not in lines[1]
    assert "tool_records" not in lines[2]
    assert "tool_results" not in lines[3]

def test_runtime_session_retention_defaults_to_seven_days():
    settings = RuntimeSettings.from_dict({})

    assert settings.session_retention_days == 7

async def test_clean_expired_sessions_removes_old_files_and_latest(tmp_path):
    s = session_with_data_dir(tmp_path)
    old = session_with_data_dir(tmp_path)
    old.messages.append({"role": "user", "content": "old"})
    await old.save_snapshot()
    old_path = log_path(old)
    stale_time = time.time() - 8 * 86400
    os.utime(old_path, (stale_time, stale_time))
    old.close()  # an expired session is one nobody is running

    assert SessionSnapshotStore.clean_expired(s.config.data_dir, s.uid, s.settings.session_retention_days) == 1

    assert not os.path.exists(old_path)
    # The pointer named the expired session, and the emptied shard is pruned with it.
    assert not os.path.exists(project_dir(old))

async def test_clean_expired_sessions_skips_current_session(tmp_path):
    s = session_with_data_dir(tmp_path)
    s.messages.append({"role": "user", "content": "current"})
    await s.save_snapshot()
    path = log_path(s)
    stale_time = time.time() - 8 * 86400
    os.utime(path, (stale_time, stale_time))

    assert SessionSnapshotStore.clean_expired(s.config.data_dir, s.uid, s.settings.session_retention_days) == 0

    assert os.path.exists(path)
