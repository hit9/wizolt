"""Model-initiated compaction: the `Context` tool, status readings, and what a compaction keeps."""

import json

import pytest
from agent_harness import call, session, session_with_provider
from test_context import _CountingModel

from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import load_session
from wizolt.agent.prompts import COMPACTION_SUMMARY_TITLE
from wizolt.base import Billing, ToolError
from wizolt.skill.library import SkillLibrary
from wizolt.tools import TOOL_REGISTRY, Tool
from wizolt.tools.memory import ContextTool
from wizolt.ui.cli.loop import CommandLoop
from wizolt.ui.cli.status import StatusReport


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


def test_context_tool_rejects_anything_but_the_two_actions(tmp_path):
    s = session(tmp_path)
    with pytest.raises(ToolError, match="action must be remaining or compact"):
        ContextTool(s, [{"action": "reset"}]).call()
    with pytest.raises(ToolError, match="action must be remaining or compact"):
        ContextTool(s, [{"action": "wipe"}]).call()
    with pytest.raises(ToolError, match="unexpected field"):
        ContextTool(s, [{"action": "compact", "scope": "all"}]).call()


def test_a_second_compact_request_in_the_same_batch_is_one_request(tmp_path):
    s = session(tmp_path)
    s.messages = [{"role": "user", "content": "old conversation"}]
    assert s.request_context_compact() is True
    assert ContextTool(s, [{"action": "compact"}]).call() == "Compaction is already scheduled for the next request."
    assert s.context_compact_requested


def test_compact_refuses_when_there_is_no_prior_conversation(tmp_path):
    s = session(tmp_path)
    with pytest.raises(ToolError, match="no prior conversation to compact"):
        ContextTool(s, [{"action": "compact"}]).call()
    assert not s.context_compact_requested


async def test_a_compact_runs_at_the_next_request_whatever_the_fill(tmp_path):
    """The compaction is deferred to the next request, not to a turn settlement: summarizing
    settled history drops nothing the batch still owes the model, and the request after the
    batch carries the summary instead of the history it replaced -- even far under the
    automatic threshold."""
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    s = session_with_provider(tmp_path)
    s.skills = SkillLibrary({})
    s.messages = [
        {"role": "user", "content": "earlier work"},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "later work"},
        {"role": "assistant", "content": "noted later"},
    ]
    s.transcript_messages = list(s.messages)
    s.state.goal = "ship the feature"
    s.state.known = ["tests use pytest"]
    s.store_tool_result("Read", [{"path": "a.py"}], "tool tr.1 Read a.py")

    class CompactingModel:
        """Answers the turn's requests and the compaction summary request."""

        def __init__(self, session):
            self.session = session
            self.last_compaction_model = ""
            self.turns = 0
            self.summaries = 0

        async def request(self, messages, tools=None):
            self.turns += 1
            if self.turns == 1:
                return {}, [call("Context", [{"action": "compact"}]), call("Read", [{"path": "a.txt", "ranges": [[1, 1]]}])], ""
            contents = [str(message.get("content") or "") for message in messages]
            # The batch's own results are still in the window, and prior conversation arrives
            # as a summary, not as the history it replaced.
            assert any("Compaction scheduled" in content for content in contents)
            assert any("alpha" in content for content in contents)
            assert any(content.startswith(COMPACTION_SUMMARY_TITLE) for content in contents)
            assert not any(content == "earlier work" for content in contents)
            assert any(content == "later work" for content in contents)
            return {"role": "assistant", "content": "done"}, [], "done"

        async def api_request(self, _messages, _tools, *, allow_stream, response_timeout, provider, json_object, billing=Billing.MAIN):
            self.summaries += 1
            return None, [], '{"summary": "the spent bulk summarized"}'

        @staticmethod
        def parse_json_object(text):
            return json.loads(text)

    agent = Agent(s, output_fn=lambda _text: None)
    agent.model = CompactingModel(s)

    assert await agent.run("carry on") == "done"
    assert agent.model.summaries == 1
    assert s.context_compact_requested is False

    # Everything outside the replaced history survives, and the replaced span is exported.
    assert s.state.goal == "ship the feature"
    assert s.state.known == ["tests use pytest"]
    assert "earlier work" in s.history[0].text
    assert "tr.2" in s.tool_results  # the batch's own result survives; tr.1 belonged to the replaced history
    assert any(message.get("content") == "earlier work" for message in s.transcript_messages)
    assert s.cwd == str(tmp_path)


@pytest.mark.parametrize("prior_snapshot", [False, True])
async def test_a_pending_compact_survives_a_crash_snapshot_and_runs_once(tmp_path, prior_snapshot):
    s = session_with_provider(tmp_path)
    s.messages = [
        {"role": "user", "content": "old conversation"},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "later conversation"},
        {"role": "assistant", "content": "noted later"},
    ]
    s.transcript_messages = list(s.messages)
    s.state.goal = "durable intent"
    if prior_snapshot:
        await s.save_snapshot()
    ContextTool(s, [{"action": "compact"}]).call()
    # This is the snapshot after a successful tool batch, before the next request.
    await s.save_snapshot()
    s.close()  # release the writer before reloading
    restored = load_session(s.uid, config=s.config)
    assert restored.context_compact_requested

    context = ContextManager(restored)
    model = _CountingModel(restored)
    await context.prepare_messages(model, "system")
    assert not restored.context_compact_requested
    assert restored.state.compaction_count == 1
    assert not any(message.get("content") == "old conversation" for message in restored.messages)
    # One request cannot repeat the pass: later requests are ordinary under-threshold requests.
    for _ in range(3):
        await context.prepare_messages(model, "system")
    assert model.calls == 1

    await restored.save_snapshot()
    restored.close()  # release the writer before reloading
    again = load_session(restored.uid, config=restored.config)
    assert not again.context_compact_requested


def test_status_recomputes_context_before_the_first_request(tmp_path):
    """A resumed session must not report an empty window while its fixed prefix fills it."""
    s = session_with_provider(tmp_path)
    s.messages = [{"role": "user", "content": "earlier"}]
    s.usage.last_prompt_tokens = 0
    s.usage.last_prompt_budget = 0
    s.state.context_percent = 0
    agent = Agent(s, output_fn=lambda _text: None)
    loop = CommandLoop(agent, input_fn=lambda _prompt: "", output_fn=lambda _text: None)

    status = StatusReport.of(loop)
    percent = json.loads(ContextTool(s, [{"action": "remaining"}]).call())["percent"]
    assert percent > 0
    assert status.snapshot.context.percent == percent


def test_status_reports_the_last_request_context_fill(tmp_path):
    s = session_with_provider(tmp_path)
    s.messages = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "noted"}]
    s.usage.last_prompt_tokens = 250
    s.usage.last_prompt_budget = 1000
    agent = Agent(s, output_fn=lambda _text: None)
    loop = CommandLoop(agent, input_fn=lambda _prompt: "", output_fn=lambda _text: None)

    assert StatusReport.of(loop).snapshot.context.percent == 25


@pytest.mark.parametrize(("reported_used", "reported_budget"), [(0, 0), (1234, 0), (0, 4000)])
def test_remaining_uses_exact_projection_when_provider_pair_is_missing(tmp_path, reported_used, reported_budget):
    s = session(tmp_path)
    ctx = ContextManager(s)
    messages = [{"role": "user", "content": "small request"}]
    ctx.update_percent(messages, None)
    estimated = ctx.request_tokens(messages, None)
    assert 0 < estimated < s.request_token_budget() // 100
    s.usage.last_prompt_tokens = reported_used
    s.usage.last_prompt_budget = reported_budget
    reading = json.loads(ContextTool(s, [{"action": "remaining"}]).call())
    assert reading == {"percent": 0, "used": estimated, "budget": s.request_token_budget(), "remaining": s.request_token_budget() - estimated}
