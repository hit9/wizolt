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
