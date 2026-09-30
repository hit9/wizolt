"""Zellij CLI response handling, without requiring an installed multiplexer."""

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the PTY adapter requires POSIX")


@pytest.fixture
def query(tmp_path, monkeypatch):
    import zellij_terminal

    pane = zellij_terminal.ZellijPane.__new__(zellij_terminal.ZellijPane)
    pane.session = "test-session"
    pane.config = tmp_path / "zellij.kdl"
    pane.env = {}
    pane.raw = bytearray()
    run = Mock()
    monkeypatch.setattr(zellij_terminal.subprocess, "run", run)
    monkeypatch.setattr(zellij_terminal.time, "sleep", lambda _: None)
    return pane, run


def result(stdout, returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr="query failed" if returncode else "")


def test_keys_delivers_control_and_named_keys_as_bytes_and_preserves_literal_text(query):
    pane, run = query
    run.return_value = result("")
    pane.keys("j", "C-c", "Space", "Tab", "Enter", "Escape", "C-u")
    pane.literal("C-c")
    assert [call.args[0][-3:] for call in run.call_args_list] == [
        ["action", "write-chars", "j"],
        ["action", "write", "3"],
        ["action", "write", "32"],
        ["action", "write", "9"],
        ["action", "write", "13"],
        ["action", "write", "27"],
        ["action", "write", "21"],
        ["action", "write-chars", "C-c"],
    ]


@pytest.mark.parametrize("empty", ["", " \n"])
def test_geometry_recovers_from_empty_successful_cli_response(query, empty):
    pane, run = query
    run.side_effect = [result(empty), result('[{"id": 7, "is_focused": true}]')]
    assert pane.geometry() == [{"id": 7, "is_focused": True}]
    assert run.call_count == 2
    assert all(call.args[0][-3:] == ["action", "list-panes", "--json"] for call in run.call_args_list)


def test_geometry_empty_response_eventually_fails(query):
    pane, run = query
    run.return_value = result("")
    with pytest.raises(AssertionError, match="test-session.*empty.*5 attempts"):
        pane.geometry()
    assert run.call_count == 5


def test_geometry_accepts_empty_pane_list_without_retry(query):
    pane, run = query
    run.return_value = result("[]")
    assert pane.geometry() == []
    assert run.call_count == 1


@pytest.mark.parametrize("response, error", [(result("{broken"), json.JSONDecodeError), (result("", 1), AssertionError)])
def test_geometry_does_not_retry_malformed_json_or_command_failure(query, response, error):
    pane, run = query
    run.return_value = response
    with pytest.raises(error):
        pane.geometry()
    assert run.call_count == 1


def test_readiness_recovers_from_query_timeout(query):
    pane, _run = query
    pane.capture = Mock(side_effect=[subprocess.TimeoutExpired("list-panes", 10), ["READY>"]])
    assert pane.wait_for("READY>") == "READY>"


def test_readiness_reports_query_timeout_at_deadline(query, monkeypatch):
    import zellij_terminal

    pane, _run = query
    pane.capture = Mock(side_effect=subprocess.TimeoutExpired("list-panes", 10))
    monkeypatch.setattr(zellij_terminal.time, "monotonic", Mock(side_effect=[0, 0, 16]))
    with pytest.raises(AssertionError, match="(?s)missing 'READY>'.*list-panes.*timed out"):
        pane.wait_for("READY>")


@pytest.fixture
def startup_cli(tmp_path, monkeypatch):
    """Replace only the external executable; exercise the real PTY, reader and cleanup."""
    import os

    executable = tmp_path / "zellij"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "path = Path(os.environ['FAKE_ZELLIJ_PATH'])\n"
        "args = sys.argv[1:]\n"
        "if '--new-session-with-layout' in args:\n"
        "    (path / 'environment.json').write_text(json.dumps(dict(os.environ)))\n"
        "    if os.environ.get('FAKE_ZELLIJ_EXIT'):\n"
        "        sys.exit(23)\n"
        "    time.sleep(0.2)\n"
        "    (path / 'started').touch()\n"
        "    print('READY> ', flush=True)\n"
        "    time.sleep(30)\n"
        "elif 'list-panes' in args:\n"
        "    if not (path / 'started').exists():\n"
        "        (path / 'early-query').touch()\n"
        "        sys.exit(1)\n"
        "    print(json.dumps([{'id': 1, 'is_focused': True, 'is_plugin': False}]))\n"
        "elif 'subscribe' in args:\n"
        "    calls = path / 'subscribe-calls'\n"
        "    calls.write_text((calls.read_text() if calls.exists() else '') + 'x')\n"
        "    mode = os.environ.get('FAKE_ZELLIJ_SUBSCRIBE', 'snapshot')\n"
        "    if mode == 'close-once' and calls.read_text() == 'x':\n"
        "        sys.exit(0)\n"
        "    if mode == 'unknown-pane':\n"
        "        sys.stderr.write('Pane terminal_1 not found\\n')\n"
        "        sys.exit(2)\n"
        "    if mode == 'wrong-event':\n"
        "        print(json.dumps({'event': 'pane_closed', 'pane_id': 'terminal_1'}), flush=True)\n"
        "    else:\n"
        "        print(json.dumps({'event': 'pane_update', 'is_initial': True,\n"
        "                          'scrollback': [], 'viewport': ['READY> ']}), flush=True)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ZELLIJ_PATH", str(tmp_path))
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    return tmp_path, runtime


def test_startup_observes_pty_before_querying_session(startup_cli, monkeypatch):
    from zellij_terminal import ZellijPane

    path, runtime = startup_cli
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", "/unrelated/developer/session")
    pane = ZellijPane(path, runtime)
    try:
        assert not (path / "early-query").exists()
        assert pane.capture(full=False) == ["READY>"]
        env = json.loads((path / "environment.json").read_text())
        assert env["ZELLIJ_SOCKET_DIR"] == str(runtime)
        assert env["TMPDIR"] == str(path / "tmp")
        assert (path / "tmp").is_dir()
    finally:
        pane.close()
    assert pane.client.poll() is not None
    assert not pane.reader.is_alive()
    assert b"READY>" in (path / "client.ansi").read_bytes()


def test_startup_reports_early_client_exit_and_saves_failure(startup_cli, monkeypatch):
    from zellij_terminal import ZellijPane

    path, runtime = startup_cli
    monkeypatch.setenv("FAKE_ZELLIJ_EXIT", "1")
    with pytest.raises(AssertionError, match="exited during startup: returncode=23"):
        ZellijPane(path, runtime)
    assert not (path / "early-query").exists()
    assert "returncode=23" in (path / "startup-error.txt").read_text()
    assert (path / "client.ansi").exists()


def test_startup_timeout_does_not_probe_session(query, monkeypatch):
    import zellij_terminal

    pane, run = query
    pane.client = Mock()
    pane.client.poll.return_value = None
    pane.raw.extend(b"partial terminal output")
    monkeypatch.setattr(zellij_terminal.time, "monotonic", Mock(side_effect=[0, 0, 16]))
    with pytest.raises(AssertionError, match="startup timed out.*returncode=None.*partial terminal output"):
        pane._wait_for_startup()
    run.assert_not_called()


def test_capture_recovers_from_a_subscription_closed_before_its_snapshot(startup_cli):
    # CI's two-core runners drop a pane's first subscription under load; the pane is fine, so the
    # read-only query is retried instead of failing the scenario that asked for it.
    from zellij_terminal import ZellijPane

    path, runtime = startup_cli
    pane = ZellijPane(path, runtime)
    try:
        pane.env["FAKE_ZELLIJ_SUBSCRIBE"] = "close-once"
        (path / "subscribe-calls").write_text("")
        assert pane.capture(full=False) == ["READY>"]
    finally:
        pane.close()
    assert (path / "subscribe-calls").read_text() == "xx"


def test_capture_reports_a_subscription_that_keeps_closing(startup_cli):
    from zellij_terminal import ZellijPane

    path, runtime = startup_cli
    pane = ZellijPane(path, runtime)
    try:
        pane.env["FAKE_ZELLIJ_SUBSCRIBE"] = "unknown-pane"
        (path / "subscribe-calls").write_text("")
        with pytest.raises(AssertionError) as error:
            pane.capture(full=False)
    finally:
        pane.close()
    message = str(error.value)
    assert "closed without a snapshot" in message and "after 5 attempts" in message
    assert "exit=2" in message and "Pane terminal_1 not found" in message
    assert (path / "subscribe-calls").read_text() == "xxxxx"


def test_capture_does_not_retry_an_unexpected_first_event(startup_cli):
    from zellij_terminal import ZellijPane

    path, runtime = startup_cli
    pane = ZellijPane(path, runtime)
    try:
        pane.env["FAKE_ZELLIJ_SUBSCRIBE"] = "wrong-event"
        (path / "subscribe-calls").write_text("")
        with pytest.raises(AssertionError, match="pane_closed"):
            pane.capture(full=False)
    finally:
        pane.close()
    assert (path / "subscribe-calls").read_text() == "x"
