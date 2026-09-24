"""delegate send (split from tests/test_worker_handoff.py)."""

import pytest
from test_worker_handoff import FakeModelClient, _delegate_call, _delegate_runner, _delegate_session

from wizolt.prompts import WORKER_PROMPT
from wizolt.tools import tooloutput


async def test_delegate_send_logs_a_worker_start_marker(tmp_path, monkeypatch):
    from wizolt.base import LogBlock, LogRole, oneline
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    parent.config.providers["default"].model = "worker-model-x"
    order = "Rewrite the worker handoff plan to cover the start marker, then check it. " * 8
    model = FakeModelClient([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    outputs = []
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=outputs.append)
    await _delegate_call(parent, runner, action="send", order=order)

    blocks = [block for block in outputs if isinstance(block, LogBlock)]
    marker = next(block for block in blocks if any(item.role is LogRole.WORKER for item, _ in block.walk()))
    rendered = str(marker)
    assert "[worker]" in rendered
    assert "▶" in rendered
    assert "default/worker-model-x" in rendered
    assert oneline(order, 200) in rendered


async def test_delegate_send_worker_rule_start_label(tmp_path, monkeypatch):
    from wizolt.base import oneline
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    parent.config.providers["default"].model = "worker-model-x"
    order = "Rewrite the worker handoff plan to cover the start rule, then check it. " * 8
    model = FakeModelClient([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    labels = []
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=lambda text: None)
    runner.hooks.worker_rule = lambda label: labels.append(label)
    await _delegate_call(parent, runner, action="send", order=order)

    assert labels, "the worker_rule callback never fired"
    assert labels[0].startswith("worker start · default/worker-model-x · ")
    assert oneline(order, 60) in labels[0]
    assert not any("[worker]" in rendered for rendered in labels)  # the rule label replaces the [worker] ▶ line


async def test_delegate_send_worker_rule_start_label_with_title(tmp_path, monkeypatch):
    from wizolt.base import oneline
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    parent.config.providers["default"].model = "worker-model-x"
    order = "Rewrite the worker handoff plan to cover the start rule, then check it. " * 8
    model = FakeModelClient([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    labels = []
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=lambda text: None)
    runner.hooks.worker_rule = lambda label: labels.append(label)
    await _delegate_call(parent, runner, action="send", order=order, title="fix /status blank line")

    assert labels, "the worker_rule callback never fired"
    assert labels[0].startswith("worker start · default/worker-model-x · ")
    assert "fix /status blank line" in labels[0]
    assert oneline(order, 60) not in labels[0]


async def test_delegate_send_worker_start_marker_with_title(tmp_path, monkeypatch):
    from wizolt.base import LogBlock, LogRole, oneline
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    parent.config.providers["default"].model = "worker-model-x"
    order = "Rewrite the worker handoff plan to cover the start marker, then check it. " * 8
    model = FakeModelClient([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    outputs = []
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=outputs.append)
    await _delegate_call(parent, runner, action="send", order=order, title="fix /status blank line")

    blocks = [block for block in outputs if isinstance(block, LogBlock)]
    marker = next(block for block in blocks if any(item.role is LogRole.WORKER for item, _ in block.walk()))
    rendered = str(marker)
    assert "[worker]" in rendered and "▶" in rendered
    assert "default/worker-model-x" in rendered
    assert "fix /status blank line" in rendered
    assert oneline(order, 200) not in rendered


async def test_delegate_send_worker_rule_start_label_falls_back_to_order(tmp_path, monkeypatch):
    from wizolt.base import oneline
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    parent.config.providers["default"].model = "worker-model-x"
    order = "Rewrite the worker handoff plan to cover the start rule, then check it. " * 8
    model = FakeModelClient([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    labels = []
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=lambda text: None)
    runner.hooks.worker_rule = lambda label: labels.append(label)
    await _delegate_call(parent, runner, action="send", order=order)

    assert labels, "the worker_rule callback never fired"
    assert labels[0].startswith("worker start · default/worker-model-x · ")
    assert oneline(order, 60) in labels[0]


async def test_delegate_rejects_empty_title(tmp_path):
    from wizolt.base import ToolError
    from wizolt.tools.delegate import DelegateTool

    parent = _delegate_session(tmp_path)
    with pytest.raises(ToolError, match="non-empty string"):
        await DelegateTool(parent, [{"action": "send", "order": "work", "title": "   "}]).call()


async def test_delegate_send_language_directive_is_injected_into_the_order(tmp_path, monkeypatch):
    parent = _delegate_session(tmp_path)
    model = FakeModelClient([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    runner = _delegate_runner(parent)
    await _delegate_call(parent, runner, action="send", order="fix the parser", language="Chinese")

    worker_order = model.requests[0][-1]["content"]
    assert worker_order.startswith("fix the parser")
    assert "Reply language: Chinese" in worker_order and "live stream" in worker_order


async def test_delegate_send_rejects_a_blank_language(tmp_path):
    from wizolt.base import ToolError

    parent = _delegate_session(tmp_path)
    runner = _delegate_runner(parent)
    with pytest.raises(ToolError, match="language"):
        await _delegate_call(parent, runner, action="send", order="o", language="   ")


async def test_worker_inherits_forced_reply_language_from_parent(tmp_path, monkeypatch):
    from wizolt.context import ContextManager

    parent = _delegate_session(tmp_path)
    parent.settings.language = "Chinese"
    model = FakeModelClient([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    runner = _delegate_runner(parent)
    await _delegate_call(parent, runner, action="send", order="fix the parser")

    worker = parent.worker
    assert worker.settings.language == "Chinese"
    system = model.requests[0][0]["content"]
    assert system.startswith(WORKER_PROMPT.strip())
    assert "LANGUAGE OVERRIDE:" in system and "Chinese" in system
    # the projection the worker uses matches the request the fake model received
    assert ContextManager(worker).model_messages(worker.system_prompt)[0]["content"] == system


async def test_delegate_envelope_reports_max_steps_from_runtime_fact(tmp_path, monkeypatch):
    from wizolt.engine import Agent

    parent = _delegate_session(tmp_path)
    runner = _delegate_runner(parent)

    async def run_stopped(self, order):
        self.stopped_at_max_steps = True
        return "done"

    async def run_normal(self, order):
        self.stopped_at_max_steps = False
        return "Stopped after max_agent_steps=3 (cosmetic wording only)"

    monkeypatch.setattr(Agent, "run", run_stopped)
    result = await _delegate_call(parent, runner, action="send", order="o")
    assert 'stopped_at_max_steps="true"' in result

    monkeypatch.setattr(Agent, "run", run_normal)
    result = await _delegate_call(parent, runner, action="send", order="o")
    assert 'stopped_at_max_steps="false"' in result  # the words are irrelevant; the fact is not set


async def test_delegate_envelope_reports_token_spend_and_summary_renders(tmp_path, monkeypatch):
    from wizolt.engine import Agent

    parent = _delegate_session(tmp_path)
    runner = _delegate_runner(parent)

    async def run_quiet(self, order):
        self.stopped_at_max_steps = False
        return "done"

    monkeypatch.setattr(Agent, "run", run_quiet)
    result = await _delegate_call(parent, runner, action="send", order="o")
    assert 'tokens="' in result
    assert 'rounds="' in result
    assert 'context_percent="0"' in result  # no usage budget with the fake model: the state fallback
    parent.worker.state.context_percent = 42  # the envelope reports the live fill, not a delta
    result = await _delegate_call(parent, runner, action="send", order="o")
    assert 'context_percent="42"' in result
    summary = tooloutput.delegate_result_summary(result)
    assert " in / " in summary and " out" in summary
    assert "0 in / 0 out" in summary


def test_delegate_summary_formats_tokens_and_tolerates_old_envelopes(tmp_path):
    summary = tooloutput.delegate_result_summary(
        '<Delegate action="send" steps="3" elapsed="2.5s" files="a.txt, b.txt" stopped_at_max_steps="false" tokens="8200/1300">'
    )
    assert "8.2K in / 1.3K out" in summary

    legacy = tooloutput.delegate_result_summary('<Delegate action="send" steps="3" elapsed="2.5s" files="a.txt, b.txt" stopped_at_max_steps="false">')
    assert "steps 3" in legacy
    assert "2.5s" in legacy
    assert "a.txt, b.txt" in legacy
    assert " in / " not in legacy
    assert "round " not in legacy and "ctx " not in legacy  # neither attribute existed back then


def test_delegate_summary_shows_rounds_and_context_fill(tmp_path):
    envelope = '<Delegate action="send" steps="3" elapsed="2.5s" files="a.txt" stopped_at_max_steps="false" tokens="10/20" rounds="4" context_percent="73">'
    fields = tooloutput.delegate_result_fields(envelope)
    assert fields is not None
    assert (fields.rounds, fields.context_percent) == ("4", "73")

    summary = tooloutput.delegate_result_summary(envelope)
    assert "round 4" in summary
    assert "ctx 73%" in summary


async def test_send_rejects_worker_calls_to_excluded_tools(tmp_path, monkeypatch):
    """End to end at the Delegate boundary: the worker's real tool block has ViewImage and
    ToolScript but not Ask/NextHints/Delegate, a hallucinated call to an excluded tool is
    rejected with a tool message instead of blocking on user input, and ViewImage executes
    as an ordinary tool (its failure here is a plain missing-file error)."""
    from wizolt.base import ToolCall
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    prompts = []

    def fail_on_user(*args):
        prompts.append(args)
        raise AssertionError("worker blocked on user input")

    model = FakeModelClient(
        [
            (
                {"role": "assistant", "content": ""},
                [ToolCall("a1", "Ask", [{"questions": [{"question": "hi?"}]}]), ToolCall("n1", "NextHints", [{"inputs": ["x"]}])],
                "",
            ),
            ({"role": "assistant", "content": ""}, [ToolCall("v1", "ViewImage", ["missing.png"])], ""),
            ({"role": "assistant", "content": "done"}, [], "done"),
        ]
    )
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    runner = ToolRunner(parent, ContextManager(parent), input_fn=fail_on_user, output_fn=lambda text: None)
    result = await _delegate_call(parent, runner, action="send", order="do the thing")

    names = {(schema.get("function") or schema).get("name") for schema in model.received_tools[0] or [] if isinstance(schema, dict)}
    assert {"ViewImage", "ToolScript"} <= names
    assert not {"Ask", "NextHints", "Delegate"} & names

    assert prompts == []  # no user prompt ever fired: the calls were rejected, not executed
    second = str(model.requests[1])
    assert "Ask is not available in this session" in second
    assert "NextHints is not available in this session" in second
    assert "Cannot read image" in str(model.requests[2])
    assert "done" in result


class _QueueingModel(FakeModelClient):
    """Enqueues one follow-up into the worker's queue on its first request: the user typing
    mid-delegation, at the only moment the runtime would."""

    def __init__(self, script, worker_of):
        super().__init__(script)
        self._worker_of = worker_of

    async def request(self, messages, request_tools=None):
        if not self.requests and self._worker_of() is not None:
            self._worker_of().enqueue_user_input("also check the tests")
        return await super().request(messages, request_tools)


async def test_delegate_send_claims_a_followup_queued_mid_run(tmp_path, monkeypatch):
    """A follow-up queued while the delegation ran reaches the worker's own next model request,
    and the flush echo marks it as the worker's so the transcript says who read it."""
    from wizolt.base import ToolCall
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    note = tmp_path / "note.txt"
    note.write_text("worker input")
    model = _QueueingModel(
        [
            ({"role": "assistant", "content": ""}, [ToolCall("r1", "Read", [str(note)])], ""),
            ({"role": "assistant", "content": "done"}, [], "done"),
        ],
        lambda: parent.worker,
    )
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    flushed = []
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=lambda text: None)
    runner.hooks.on_queue_flush = flushed.append
    await _delegate_call(parent, runner, action="send", order="read the note")

    second = str(model.requests[1])
    assert "also check the tests" in second and "Live follow-up" in second  # the worker's request carried it
    assert flushed == [["[worker] also check the tests"]]  # the echo names the side that read it
    assert parent.pending_user_inputs == []  # nothing fell back: the worker consumed the follow-up


async def test_delegate_send_returns_unclaimed_followups_to_the_parent(tmp_path, monkeypatch):
    """A follow-up that arrived after the worker's last request is not stranded: when the send
    ends it falls back to the parent's queue as an ordinary live follow-up."""
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    model = _QueueingModel([({"role": "assistant", "content": "done"}, [], "done")], lambda: parent.worker)
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=lambda text: None)
    await _delegate_call(parent, runner, action="send", order="answer at once")

    assert parent.worker is not None
    assert parent.worker.pending_user_inputs == []
    assert [item.text for item in parent.pending_user_inputs] == ["also check the tests"]


async def test_delegate_failure_hands_consumed_followups_back(tmp_path, monkeypatch):
    """A follow-up the worker's failed request already committed is not answered: the send hands
    it back to the parent instead of leaving the user with an echo and no reply. The failing
    request is the *second* one, so the follow-up really was claimed and acknowledged into the
    worker's dead turn -- the case `_return_unclaimed_inputs` alone cannot see."""
    from wizolt.base import ModelError, ToolCall, ToolError
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    note = tmp_path / "note.txt"
    note.write_text("worker input")

    class Failing(FakeModelClient):
        async def request(self, messages, request_tools=None):
            if not self.requests:  # first step: enqueue the follow-up, hand back a tool call
                parent.worker.enqueue_user_input("also check the tests")
                self.requests.append(messages)
                return {"role": "assistant", "content": ""}, [ToolCall("r1", "Read", [str(note)])], ""
            raise ModelError("provider exploded")  # second step: the one that carries it fails

    model = Failing([])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    flushed = []
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=lambda text: None)
    runner.hooks.on_queue_flush = flushed.append
    with pytest.raises(ToolError, match="provider exploded"):
        await _delegate_call(parent, runner, action="send", order="do the thing")

    # The failing request really did carry it (the echo proves the claim), yet nothing is left
    # in the worker's queue for _return_unclaimed_inputs to find.
    assert flushed == [["[worker] also check the tests"]]
    assert parent.worker is not None and parent.worker.pending_user_inputs == []
    assert [item.text for item in parent.pending_user_inputs] == ["also check the tests"]


async def test_delegate_clears_a_stale_inflight_marker(tmp_path, monkeypatch):
    """A send that dies before the engine's first settlement leaves the in-flight marker set;
    the send's own teardown clears it, or later follow-ups would keep routing to a dead turn."""
    from wizolt.base import ModelError, ToolError
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)

    class DiesInSetup(FakeModelClient):
        async def request(self, messages, request_tools=None):
            parent.worker._active_turn_messages.append({"role": "user", "content": "stale"})
            raise ModelError("died before settling")

    model = DiesInSetup([])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=lambda text: None)
    with pytest.raises(ToolError, match="died before settling"):
        await _delegate_call(parent, runner, action="send", order="do the thing")

    assert parent.worker is not None
    assert parent.worker._active_turn_messages == []
