"""Session, worker and compaction events at their actual execution boundaries."""

import asyncio
import json
import shlex

import pytest
from agent_harness import session, session_with_provider
from test_shell_hooks import _agent, _hook, _hooks

from wizolt.agent import compaction
from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import close_agent_resources, load_session
from wizolt.base import ModelError, WizoltError
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.commands import compact


def _record(event, matcher="", output=""):
    return _hook(event, "cat >> events.jsonl; echo >> events.jsonl" + ("; printf %s " + shlex.quote(output) if output else ""), matcher)


def _events(tmp_path):
    path = tmp_path / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_session_start_once_context_and_resume(tmp_path):
    table = {**_record("SessionStart", "startup|resume", "startup context"), **_record("SessionEnd", "other")}
    agent, requests = _agent(tmp_path, table, ["one", "two"])
    await agent.start_session()
    await agent.run("first")
    await agent.run("second")
    assert [item["hook_event_name"] for item in _events(tmp_path)] == ["SessionStart"]
    assert "startup context" in str(requests[0])
    assert str(requests[1]).count("startup context") == 1
    assert "startup context" not in str(agent.session.transcript_messages)
    await close_agent_resources(agent)
    await close_agent_resources(agent)
    assert [item["hook_event_name"] for item in _events(tmp_path)] == ["SessionStart", "SessionEnd"]
    uid, config = agent.session.uid, agent.session.config
    config.hooks = table
    agent.session.close()
    resumed = Agent(load_session(uid, config=config, cwd=str(tmp_path)), output_fn=lambda _: None)
    await resumed.start_session()
    assert _events(tmp_path)[-1]["source"] == "resume"
    await close_agent_resources(resumed)
    resumed.session.close()


async def test_idle_frontend_fires_start_and_end_without_model_request(tmp_path):
    s = _hooks(session(tmp_path), {**_record("SessionStart"), **_record("SessionEnd", "prompt_input_exit")})
    agent = Agent(s, output_fn=lambda _: None)
    loop = CommandLoop(agent, input_fn=lambda _: "/exit", output_fn=lambda _: None)
    await loop.run_simple(show_banner=False)
    assert [item["hook_event_name"] for item in _events(tmp_path)] == ["SessionStart", "SessionEnd"]


async def test_start_context_survives_a_refused_prompt(tmp_path):
    from wizolt.shellhooks import PromptBlocked

    agent, requests = _agent(
        tmp_path,
        {
            **_record("SessionStart", output="keep this context"),
            **_hook("UserPromptSubmit", "test -f allowed || exit 2"),
        },
        ["answer"],
    )
    with pytest.raises(PromptBlocked):
        await agent.run("refused")
    assert agent.session.messages == []
    assert agent.session.state.round_count == 0
    (tmp_path / "allowed").touch()
    await agent.run("accepted")
    assert "keep this context" in str(requests[0])
    assert len(_events(tmp_path)) == 1


async def test_session_notifications_cannot_block(tmp_path):
    agent, requests = _agent(
        tmp_path,
        {
            **_hook("SessionStart", """echo '{"decision":"block","reason":"ignored","hookSpecificOutput":{"additionalContext":"still context"}}' """),
            **_hook("SessionEnd", "echo ignored >&2; exit 2"),
        },
        ["answer"],
    )
    assert await agent.run("hello") == "answer"
    assert "still context" in str(requests[0])
    await close_agent_resources(agent)


async def test_model_failure_fires_stop_failure_after_settlement(tmp_path):
    agent, _ = _agent(tmp_path, {**_record("StopFailure", "unknown"), **_record("Stop")}, [])

    async def fail(*_):
        raise ModelError("provider is unavailable")

    agent.model.request = fail
    with pytest.raises(ModelError, match="provider is unavailable"):
        await agent.run("hello")
    [event] = _events(tmp_path)
    assert event["hook_event_name"] == "StopFailure"
    assert event["error_details"] == "provider is unavailable"
    assert not agent.session._active_turn_messages


async def test_cancel_does_not_fire_stop_failure(tmp_path):
    agent, _ = _agent(tmp_path, _record("StopFailure"), [])

    async def cancel(*_):
        raise asyncio.CancelledError

    agent.model.request = cancel
    with pytest.raises(asyncio.CancelledError):
        await agent.run("hello")
    assert _events(tmp_path) == []


async def test_worker_start_stop_and_context_stay_in_worker(tmp_path, monkeypatch):
    from test_worker_handoff import FakeModelClient, _delegate_call, _delegate_runner, _delegate_session

    parent = _hooks(
        _delegate_session(tmp_path),
        {
            **_record("SubagentStart", "worker", json.dumps({"hookSpecificOutput": {"additionalContext": "worker guidance"}})),
            **_hook("SubagentStop", "cat >> events.jsonl; echo >> events.jsonl; test -f verified && exit 0; touch verified; echo finish tests >&2; exit 2"),
            **_hook("Stop", "touch parent-stop"),
            **_hook("SessionStart", "touch parent-start"),
        },
    )
    model = FakeModelClient([({"role": "assistant", "content": answer}, [], answer) for answer in ["draft", "verified answer", "second report"]])
    monkeypatch.setattr("wizolt.agent.engine.ModelClient", lambda _: model)
    runner = _delegate_runner(parent)
    assert "verified answer" in await _delegate_call(parent, runner, action="send", order="work")
    await _delegate_call(parent, runner, action="send", order="continue")
    events = _events(tmp_path)
    assert [item["hook_event_name"] for item in events] == ["SubagentStart", "SubagentStop", "SubagentStop", "SubagentStart", "SubagentStop"]
    assert all(item["session_id"] == parent.uid and item["agent_id"] == parent.worker.uid for item in events)
    assert str(model.requests[-1]).count("worker guidance") == 1
    assert "finish tests" in str(model.requests[1])
    assert "worker guidance" not in str(parent.messages)
    assert not (tmp_path / "parent-stop").exists()
    assert not (tmp_path / "parent-start").exists()


class _CompactionModel:
    last_compaction_model = ""

    def __init__(self, session, fail=False):
        self.session = session
        self.fail = fail
        self.requests = []

    async def api_request(self, messages, tools, **kwargs):
        self.requests.append(messages)
        if self.fail:
            raise ModelError("summary unavailable")
        return "", "", json.dumps({"summary": "a concise summary"})

    @staticmethod
    def parse_json_object(content):
        return json.loads(content)


@pytest.mark.parametrize("trigger", ["auto", "manual"])
@pytest.mark.parametrize("fail", [False, True])
async def test_compaction_events_include_fallback_summary(tmp_path, trigger, fail):
    s = _hooks(session_with_provider(tmp_path), {**_record("PreCompact", trigger), **_record("PostCompact", trigger)})
    s.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"} for i in range(12)]
    model = _CompactionModel(s, fail)
    if trigger == "auto":
        compactor = compaction.Compactor(ContextManager(s), model)
        before, keep = compactor.parts()
        assert await compactor.run(before, keep, "trimmed")
    else:
        agent = Agent(s, output_fn=lambda _: None)
        agent.model = model
        loop = CommandLoop(agent, output_fn=lambda _: None)
        assert "Compacted context" in await compact(loop, "")
    before, after = _events(tmp_path)
    assert before["hook_event_name"] == "PreCompact"
    assert before["custom_instructions"] is None
    assert after["hook_event_name"] == "PostCompact"
    assert after["compact_summary"] == s.state.summary
    assert bool(s.state.summary)
    assert before["trigger"] == after["trigger"] == trigger


@pytest.mark.parametrize("trigger", ["auto", "manual"])
async def test_blocked_compaction_does_not_request_or_rewrite(tmp_path, trigger):
    s = _hooks(session_with_provider(tmp_path), {**_hook("PreCompact", "echo preserve context >&2; exit 2", trigger), **_record("PostCompact")})
    s.messages = [{"role": "user", "content": str(i)} for i in range(12)]
    original = list(s.messages)
    model = _CompactionModel(s)
    if trigger == "auto":
        compactor = compaction.Compactor(ContextManager(s), model)
        with pytest.raises(WizoltError, match="preserve context"):
            await compactor.run(*compactor.parts(), "trimmed")
    else:
        agent = Agent(s, output_fn=lambda _: None)
        agent.model = model
        assert "preserve context" in await compact(CommandLoop(agent, output_fn=lambda _: None), "")
    assert s.messages == original
    assert model.requests == []
    assert _events(tmp_path) == []


async def test_parent_skill_subagent_hooks_refresh_between_sends(tmp_path, monkeypatch, isolate_home):
    from test_skill_scope import _user_skill
    from test_worker_handoff import FakeModelClient, _delegate_call, _delegate_runner, _delegate_session

    from wizolt.tools import SkillTool

    _user_skill(isolate_home, "guide", "hooks:\n  SubagentStart:\n    - matcher: worker\n      hooks:\n        - command: touch inherited-hook\n")
    parent = _delegate_session(tmp_path)
    model = FakeModelClient([({"role": "assistant", "content": "done"}, [], "done")] * 3)
    monkeypatch.setattr("wizolt.agent.engine.ModelClient", lambda _: model)
    runner = _delegate_runner(parent)
    await _delegate_call(parent, runner, action="send", order="first")
    assert not (tmp_path / "inherited-hook").exists()
    await SkillTool(parent, ["guide"]).call()
    await _delegate_call(parent, runner, action="send", order="second")
    assert (tmp_path / "inherited-hook").exists()
    assert parent.worker.active_skills == []
    (tmp_path / "inherited-hook").unlink()
    parent.active_skills.clear()
    await _delegate_call(parent, runner, action="send", order="third")
    assert not (tmp_path / "inherited-hook").exists()
