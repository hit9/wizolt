"""Model-initiated context reset: the `Context` tool, `/context`, and what a reset keeps."""

import asyncio
import json
import re

import pytest
from agent_harness import call, session, session_with_provider

from wizolt.base import SESSION_EVENT_KEY, ToolError
from wizolt.cli import commands
from wizolt.cli.loop import CommandLoop
from wizolt.context import ContextManager
from wizolt.engine import Agent
from wizolt.session import Session
from wizolt.skill import SkillLibrary
from wizolt.tools import TOOL_REGISTRY, JobTool, Tool
from wizolt.tools.memory import ContextTool, NoteTool, RecallContextTool


def test_context_tool_is_registered(tmp_path):
    assert "Context" in TOOL_REGISTRY
    resolved = [schema["function"]["name"] for schema in Tool.resolved_schemas(session(tmp_path))]
    assert "Context" in resolved


def test_context_remaining_reports_the_status_bar_figure(tmp_path):
    """The tool and the status row answer from the same two numbers: the provider's last real
    request when there is one, the session's own estimate before that."""
    s = session(tmp_path)
    s.usage.last_prompt_tokens = 1234
    s.usage.last_prompt_budget = 4000

    assert json.loads(ContextTool(s, [{"action": "remaining"}]).call()) == {
        "percent": 30,
        "used": 1234,
        "budget": 4000,
        "remaining": 2766,
    }

    fresh = session(tmp_path)
    fresh.state.context_percent = 25
    budget = fresh.request_token_budget()
    assert fresh.context_fill() == {
        "percent": 25,
        "used": 25 * budget // 100,
        "budget": budget,
        "remaining": budget - 25 * budget // 100,
    }


def test_context_tool_rejects_anything_but_the_two_actions(tmp_path):
    s = session(tmp_path)
    with pytest.raises(ToolError, match="action must be remaining or reset"):
        ContextTool(s, [{"action": "wipe"}]).call()
    with pytest.raises(ToolError, match="unexpected field"):
        ContextTool(s, [{"action": "reset", "scope": "all"}]).call()


def test_a_second_reset_request_in_the_same_batch_is_one_reset(tmp_path):
    s = session(tmp_path)
    assert s.request_context_reset() is True
    assert ContextTool(s, [{"action": "reset"}]).call() == "Reset is already scheduled for the end of this turn."
    s.apply_context_reset()
    assert s.context_reset_requested is False
    assert s.apply_context_reset() is False


async def test_reset_takes_effect_when_the_turn_ends(tmp_path):
    """A reset is deferred, not immediate. The batch that asked for it still owes the model its
    results, and the request after that batch still carries the conversation; dropping it mid-turn
    would leave the assistant message whose calls are being answered without matching results."""
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    s = session(tmp_path)
    s.skills = SkillLibrary({})
    s.messages = [{"role": "user", "content": "earlier work"}, {"role": "assistant", "content": "noted"}]
    s.transcript_messages = list(s.messages)
    s.state.goal = "ship the feature"
    s.state.known = ["tests use pytest"]
    s.state.summary = "a summary of an evicted span"
    s.usage.last_prompt_tokens = 900
    s.usage.last_prompt_budget = 1000
    ContextManager(s).store_history_segment([{"role": "user", "content": "evicted span"}], scope="history", trigger="auto", fallback=False)
    s.store_tool_result("Read", [{"path": "a.py"}], "tool tr.1 Read a.py")

    class ResettingModel:
        def __init__(self):
            self.requests = []

        async def request(self, messages, tools=None):
            self.requests.append(messages)
            if len(self.requests) == 1:
                return {}, [call("Context", [{"action": "reset"}]), call("Read", [{"path": "a.txt", "ranges": [[1, 1]]}])], ""
            contents = [str(message.get("content") or "") for message in messages]
            # Still the old window, with the batch's own results in it.
            assert any("earlier work" in content for content in contents)
            assert any("Reset scheduled" in content for content in contents)
            assert any("alpha" in content for content in contents)
            return {"role": "assistant", "content": "done"}, [], "done"

    agent = Agent(s, output_fn=lambda _text: None)
    agent.model = ResettingModel()

    assert await agent.run("carry on") == "done"
    assert len(agent.model.requests) == 2

    assert s.messages == []
    assert s.transcript_messages == []
    assert s.state.summary == ""
    assert s.state.context_percent == 0
    assert s.state.turn_messages == 0
    assert s.usage.last_prompt_tokens == 0
    assert s.usage.last_prompt_budget == 0
    # Everything outside the conversation survives.
    assert s.state.goal == "ship the feature"
    assert s.state.known == ["tests use pytest"]
    assert [segment.key for segment in s.history] == ["seg.1"]
    assert "tr.1" in s.tool_results
    assert s.cwd == str(tmp_path)


async def test_a_reset_keeps_a_background_job_and_its_stdin(tmp_path):
    s = session(tmp_path)
    await JobTool(s, [{"action": "start", "command": "cat", "stdin": True}]).call()

    s.request_context_reset()
    assert s.apply_context_reset() is True

    assert "job.1" in await JobTool(s, [{"action": "list"}]).call()
    assert await JobTool(s, [{"action": "write", "job": "job.1", "chars": "hello\n"}]).call() == "Wrote 6 characters to job.1 stdin"
    await JobTool(s, [{"action": "kill", "job": "job.1"}]).call()


async def test_a_snapshot_taken_after_a_reset_resumes_without_the_conversation(tmp_path):
    s = session(tmp_path)
    s.messages = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "noted"}]
    s.transcript_messages = list(s.messages)
    s.state.goal = "keep me"
    s.request_context_reset()
    s.apply_context_reset()
    await s.save_snapshot()

    restored = Session.load_snapshot(s.uid, config=s.config)

    # Resume appends its own session event; the conversation itself is gone.
    conversation = [message for message in restored.messages if not message.get(SESSION_EVENT_KEY)]
    assert conversation == []
    assert [message for message in restored.transcript_messages if not message.get(SESSION_EVENT_KEY)] == []
    assert restored.state.goal == "keep me"
    assert restored.context_reset_requested is False


async def test_a_reset_survives_an_interrupted_turn(tmp_path):
    """The batch that asked for the reset already ran. An interrupt settles the turn, and the reset
    asked for inside it lands there rather than being left behind for a later turn to pick up."""
    s = session(tmp_path)
    s.skills = SkillLibrary({})
    s.messages = [{"role": "user", "content": "earlier work"}]
    agent = Agent(s, output_fn=lambda _text: None)

    class InterruptingModel:
        def __init__(self):
            self.calls = 0

        async def request(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return {}, [call("Context", [{"action": "reset"}])], ""
            raise asyncio.CancelledError

    agent.model = InterruptingModel()
    with pytest.raises(asyncio.CancelledError):
        await agent.run("carry on")

    assert s.messages == []
    assert s.context_reset_requested is False


async def test_a_reset_after_earlier_snapshots_survives_the_delta_chain(tmp_path):
    """The transcript is saved as an append-only delta. A reset shortens it, so the next save has to
    write a replacement rather than a delta a loader would append to the conversation that left."""
    s = session(tmp_path)
    for index in range(3):
        s.messages.append({"role": "user", "content": f"turn {index}"})
        s.transcript_messages.append({"role": "user", "content": f"turn {index}"})
        await s.save_snapshot()
    s.state.goal = "keep me"
    s.request_context_reset()
    s.apply_context_reset()
    await s.save_snapshot()

    restored = Session.load_snapshot(s.uid, config=s.config)

    assert [message for message in restored.messages if not message.get(SESSION_EVENT_KEY)] == []
    assert [message for message in restored.transcript_messages if not message.get(SESSION_EVENT_KEY)] == []
    assert restored.state.goal == "keep me"


async def test_recall_and_note_still_work_after_a_reset_and_resume(tmp_path):
    s = session(tmp_path)
    ContextManager(s).store_history_segment([{"role": "user", "content": "evicted span"}], scope="history", trigger="auto", fallback=False)
    s.state.goal = "keep me"
    s.request_context_reset()
    s.apply_context_reset()
    await s.save_snapshot()

    restored = Session.load_snapshot(s.uid, config=s.config)

    assert "evicted span" in RecallContextTool(restored, [{"action": "get", "keys": ["seg.1"]}]).call()
    assert json.loads(NoteTool(restored, [{"action": "view", "fields": ["goal"]}]).call()) == {"goal": "keep me"}


async def test_context_command_reports_the_same_figure_as_status(tmp_path):
    """Both commands answer the same question. `state.context_percent` is not persisted, so a
    resumed session has no figure until something recomputes one -- and `/context` must not report
    an empty window while the fixed prefix already fills part of it."""
    s = session_with_provider(tmp_path)
    s.messages = [{"role": "user", "content": "earlier"}]
    s.usage.last_prompt_tokens = 0
    s.usage.last_prompt_budget = 0
    s.state.context_percent = 0
    agent = Agent(s, output_fn=lambda _text: None)
    loop = CommandLoop(agent, input_fn=lambda _prompt: "", output_fn=lambda _text: None)

    report = await commands.context_command(loop, "")
    status = commands.status(loop, "")

    match = re.search(r"Context (\d+)% used", report)
    assert match is not None
    percent = int(match.group(1))
    assert percent > 0
    assert f"`{percent}%`" in status


async def test_context_command_reports_the_fill_and_resets_on_request(tmp_path):
    s = session_with_provider(tmp_path)
    s.messages = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "noted"}]
    s.usage.last_prompt_tokens = 250
    s.usage.last_prompt_budget = 1000
    agent = Agent(s, output_fn=lambda _text: None)
    loop = CommandLoop(agent, input_fn=lambda _prompt: "", output_fn=lambda _text: None)

    assert "25% used" in await commands.context_command(loop, "")
    assert "new context window" in await commands.context_command(loop, "reset")
    assert s.messages == []
    assert await commands.context_command(loop, "everything") == "Usage: /context [reset]"
