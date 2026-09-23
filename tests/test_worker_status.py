"""worker status (split from tests/test_worker_handoff.py)."""

import os
import shutil

from test_worker_handoff import FakeModelClient, _delegate_call, _delegate_runner, _delegate_session

from wizolt.cli.worker import worker_command


async def test_status_bar_follows_the_inflight_worker(tmp_path, monkeypatch):
    from wizolt.config import (
        Config,
        ProviderConfig,
    )
    from wizolt.render import StatusBar
    from wizolt.session import Session

    # The row clips to the terminal width, and CI runs at 80 columns: pin a wide one so these
    # assertions read the whole row instead of its ellipsis.
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((200, 24)))
    parent = _delegate_session(tmp_path)
    parent.usage.last_prompt_tokens = 200
    parent.usage.last_prompt_budget = 400
    parent.usage.last_cached_prompt_tokens = 50
    bar = StatusBar(parent)

    def row():
        return "".join(text for _, text in bar.fragments())

    original = row()
    parent_lead = parent.config.active_provider + "/" + (parent.config.provider.model.rsplit("/", 1)[-1] or "(no model)")
    assert parent_lead in original and "[worker]" not in original
    assert "ctx 50% \u00b7 cache 25%" in original

    # A worker with its own usage and context, attached but idle.
    worker_config = Config()
    worker_config.providers["default"] = ProviderConfig(model="worker-model")
    worker = Session(cwd=str(tmp_path), config=worker_config, settings=parent.settings, uid=parent.uid + ".w", listed=False)
    worker.usage.last_prompt_tokens = 50
    worker.usage.last_prompt_budget = 100
    worker.usage.last_cached_prompt_tokens = 25
    parent.worker = worker
    # A live but idle worker does not take over the bar: marker, provider/model, and usage all
    # apply only while a delegation is in flight (the engine clears _active_turn_messages in
    # finish_turn), so an idle worker leaves the parent's values exactly as before it existed.
    # Its one contribution is its own context water level, appended as a separate group.
    with_worker = row()
    assert parent_lead in with_worker and "[worker]" not in with_worker
    assert "ctx 50% \u00b7 cache 25%" in with_worker
    assert "worker ctx 50%" in with_worker

    # A worker without real context (never delegated to, or reset) adds nothing: the row is
    # exactly the one from before the worker existed.
    worker.usage.last_prompt_tokens = 0
    worker.usage.last_prompt_budget = 0
    worker.state.context_percent = 0
    assert row() == original
    worker.usage.last_prompt_tokens = 50
    worker.usage.last_prompt_budget = 100

    # In flight: the row names the model actually running and its context, behind a [worker] marker.
    worker._active_turn_messages.append({"role": "user", "content": "order"})
    delegating = row()
    assert "[worker] default/worker-model" in delegating
    assert "ctx 50% \u00b7 cache 50%" in delegating
    assert parent_lead not in delegating
    # The row already shows the worker's numbers behind the marker, so the extra group is gone.
    assert "worker ctx" not in delegating

    # Session-wide groups stay the parent's, and the row returns to the parent when the worker answers.
    assert "skills " in delegating and "index" in delegating
    worker._active_turn_messages.clear()
    assert row() == with_worker


async def test_working_divider_marks_inflight_worker(tmp_path):
    from wizolt.cli import CommandLoop
    from wizolt.engine import Agent
    from wizolt.session import Session

    parent = _delegate_session(tmp_path)
    agent = Agent(parent, output_fn=lambda text: None)
    loop = CommandLoop(agent, input_fn=lambda prompt: "", output_fn=lambda text: None)
    worker = Session(cwd=str(tmp_path), config=parent.config, settings=parent.settings, uid=parent.uid + ".w", listed=False)
    parent.worker = worker

    def label():
        return "".join(text for _, text in loop.view.queue_divider_fragments())

    assert "[worker]" not in label()
    worker._active_turn_messages.append({"role": "user", "content": "order"})
    assert "[worker]" in label()


async def test_worker_model_stream_is_wired_from_the_runner(tmp_path, monkeypatch):
    parent = _delegate_session(tmp_path)
    model = FakeModelClient([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    runner = _delegate_runner(parent)
    calls = []
    runner.hooks.on_stream = lambda kind, text: calls.append((kind, text))
    await _delegate_call(parent, runner, action="send", order="o")
    on_stream = parent.worker._agent.hooks.on_stream
    assert on_stream is not runner.hooks.on_stream  # wrapped: `output_done` must not promote
    assert callable(on_stream)
    on_stream("output", "x")
    on_stream("output_done", "t")
    assert calls == [("output", "x"), ("", "")]


async def test_worker_stream_updates_parent_thinking_and_status_while_request_is_live(tmp_path, monkeypatch):
    from wizolt.cli import CommandLoop
    from wizolt.engine import Agent

    parent = _delegate_session(tmp_path)
    loop = CommandLoop(Agent(parent, output_fn=lambda _text: None), input_fn=lambda _prompt: "", output_fn=lambda _text: None)
    observed = []

    class StreamingModel(FakeModelClient):
        async def request(self, messages, request_tools=None):
            self.hooks.on_stream("reasoning", "checking the worker task")
            observed.append(
                (
                    "".join(text for _, text in loop.view.model_stream_fragments()),
                    "".join(text for _, text in loop.view.queue_divider_fragments()),
                )
            )
            self.hooks.on_stream("output", "writing the result")
            observed.append(
                (
                    "".join(text for _, text in loop.view.model_stream_fragments()),
                    "".join(text for _, text in loop.view.queue_divider_fragments()),
                )
            )
            return await super().request(messages, request_tools)

    model = StreamingModel([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda _session: model)

    await _delegate_call(parent, loop.agent.tools, action="send", order="inspect it")

    assert "checking the worker task" in observed[0][0]
    assert "thinking" in observed[0][0] and "[worker]" in observed[0][1]
    assert "writing the result" in observed[1][0]
    assert "responding" in observed[1][0] and "[worker]" in observed[1][1]


async def test_status_reports_worker_delegation_state(tmp_path):
    from wizolt.cli import CommandLoop
    from wizolt.engine import Agent
    from wizolt.session import Session

    parent = _delegate_session(tmp_path)
    agent = Agent(parent, output_fn=lambda text: None)
    outputs: list = []
    loop = CommandLoop(agent, input_fn=lambda prompt: "", output_fn=outputs.append)

    async def status_text():
        outputs.clear()
        await loop.command("/status")
        return "\n".join(str(text) for text in outputs)

    # No worker session: one `worker` row naming the configured [worker] provider. Everything is
    # one flat table — the session's own rows, the parent's, then the worker's under `worker*`.
    text = await status_text()
    assert text.lstrip().startswith("|  |  |\n")  # rendered in the content column
    assert "###" not in text
    assert "[worker] provider" in text and "default" in text

    worker = Session(cwd=str(tmp_path), config=parent.config, settings=parent.settings, uid=parent.uid + ".w", listed=False)
    parent.worker = worker

    # A fresh worker has no requests yet: the worker context row says so instead of inventing
    # tokens, and the model row mirrors the parent's (provider/model, api, reasoning).
    text = await status_text()
    assert "| worker | `default/" in text
    assert "| worker ctx | (no requests yet) · idle, rounds `0` |" in text

    worker.usage.last_prompt_tokens = 50
    worker.usage.last_prompt_budget = 100
    worker.usage.last_cached_prompt_tokens = 25
    worker.usage.prompt_tokens = 50
    worker.usage.cached_prompt_tokens = 25
    text = await status_text()
    # Scope to the worker rows: the parent's own cache row also says "(no requests yet)".
    worker_section = text.split("| worker |", 1)[1]
    assert "~50 / 100" in worker_section and "(no requests yet)" not in worker_section
    assert "| worker cache | " in worker_section and "last `50.0%` · session `50.0%`" in worker_section

    worker._active_turn_messages.append({"role": "user", "content": "order"})
    text = await status_text()
    assert "delegating, rounds `0`" in text


async def test_worker_status_command_is_human_readable(tmp_path):
    from wizolt.cli import CommandLoop
    from wizolt.engine import Agent
    from wizolt.session import Session

    parent = _delegate_session(tmp_path)
    agent = Agent(parent, output_fn=lambda text: None)
    loop = CommandLoop(agent, input_fn=lambda prompt: "", output_fn=lambda text: None)

    assert await worker_command(loop, "") == chr(10).join(["worker: no active session", "worker provider: default"])
    assert await worker_command(loop, "status") == await worker_command(loop, "")

    worker = Session(cwd=str(tmp_path), config=parent.config, settings=parent.settings, uid=parent.uid + ".w", listed=False)
    parent.worker = worker
    worker.config.provider.model = "worker-model-x"
    worker.config.provider.reasoning = "high"
    worker.state.round_count = 3
    worker.usage.last_prompt_tokens = 50
    worker.usage.last_prompt_budget = 100
    text = await worker_command(loop, "status")
    assert "worker: default/worker-model-x" in text
    assert "worker reasoning: high" in text
    assert "worker state: idle" in text
    assert "worker rounds: 3" in text
    assert "worker context: 50%" in text
    assert "<Delegate" not in text

    worker._active_turn_messages.append({"role": "user", "content": "order"})
    assert "worker state: delegating" in await worker_command(loop, "status")

    # Without provider-reported usage the state estimate is the fallback, like the envelope.
    worker.usage.last_prompt_budget = 0
    worker.state.context_percent = 42
    assert "worker context: 42%" in await worker_command(loop, "status")


async def test_worker_output_wraps_model_text_for_the_log_stream(tmp_path, monkeypatch):
    from wizolt.base import LogBlock, ToolCall
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    tool_call = ToolCall(id="call1", name="Note", args={"action": "view"})
    model = FakeModelClient(
        [
            ({"role": "assistant", "content": "thinking out loud"}, [tool_call], "thinking out loud"),
            ({"role": "assistant", "content": "done"}, [], "done"),
        ]
    )
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    outputs = []
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=outputs.append)
    await _delegate_call(parent, runner, action="send", order="o")

    assert outputs, "the worker turn produced no output"
    rendered = [str(block) for block in outputs if isinstance(block, LogBlock)]  # str items raised before the fix
    assert any("thinking out loud" in text for text in rendered)


async def test_worker_interim_model_text_routes_to_worker_answer_when_wired(tmp_path, monkeypatch):
    from wizolt.base import LogBlock, ToolCall
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    parent = _delegate_session(tmp_path)
    tool_call = ToolCall(id="call1", name="Note", args={"action": "view"})
    model = FakeModelClient(
        [
            ({"role": "assistant", "content": "**thinking out loud**"}, [tool_call], "**thinking out loud**"),
            ({"role": "assistant", "content": "done"}, [], "done"),
        ]
    )
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda session: model)
    log_outputs = []
    answer_outputs = []
    append_answer = answer_outputs.append
    runner = ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=log_outputs.append)
    runner.hooks.worker_answer = append_answer

    await _delegate_call(parent, runner, action="send", order="o")

    # Interim model text and final answer route to worker_answer for markdown rendering.
    assert answer_outputs == ["**thinking out loud**", "done"]
    # Worker tool output still flows through the ordinary log stream as LogBlock.
    assert any(isinstance(block, LogBlock) for block in log_outputs)
    # The bindings that make the split work: the worker agent's model text goes to the markdown
    # hook, and its tool runner is pinned to the plain log wrapper -- dropping the pin would let
    # tool output slip into the markdown channel.
    agent = parent.worker._agent
    assert agent.output_fn is append_answer
    assert agent.tools.output_fn is not append_answer


async def test_worker_output_passes_memory_shaped_text_through_for_highlighting():
    from types import SimpleNamespace

    from wizolt.base import LogBlock, LogLine, LogRole
    from wizolt.tools.delegate import _worker_output

    outputs = []
    emit = _worker_output(SimpleNamespace(output_fn=outputs.append))
    note = "goal: do x\nplan:\n  - [x] done"
    emit(note)
    emit("plain model text")
    emit(LogLine("", "tool line", LogRole.TOOL))

    # The memory-shaped string passes through as a bare str so the parent's segments() ->
    # memory_segments() applies the per-line colors; other strings and LogLine items stay wrapped.
    assert outputs[0] is note
    assert isinstance(outputs[1], LogBlock)
    assert isinstance(outputs[2], LogBlock)


async def test_status_bar_names_the_worker_model_during_a_real_delegation(tmp_path, monkeypatch):
    """Regression guard: the row must describe the request actually on the wire.

    Sampled from inside the worker's model request, through the real Delegate path, so a change
    that quietly re-points the bar's identity or usage back at the parent session fails here
    rather than only in the unit-level test that drives `_active_turn_messages` by hand.
    """
    from wizolt.render import StatusBar

    # The row clips to the terminal width, and CI runs at 80 columns: pin a wide one so these
    # assertions read the whole row instead of its ellipsis.
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((200, 24)))
    parent = _delegate_session(tmp_path)
    parent.config.provider.model = "parent-model"
    parent.config.worker_model = "worker-model"
    parent.usage.last_prompt_tokens = 200
    parent.usage.last_prompt_budget = 400
    parent.usage.last_cached_prompt_tokens = 50
    bar = StatusBar(parent)

    def row():
        return "".join(text for _, text in bar.fragments())

    idle = row()
    assert "default/parent-model" in idle and "ctx 50% · cache 25%" in idle

    sampled: list[str] = []

    class SamplingModel(FakeModelClient):
        async def request(self, messages, request_tools=None):
            worker = parent.worker
            assert worker is not None
            worker.usage.last_prompt_tokens = 30
            worker.usage.last_prompt_budget = 100
            worker.usage.last_cached_prompt_tokens = 15
            sampled.append(row())
            return await super().request(messages, request_tools)

    model = SamplingModel([({"role": "assistant", "content": "done"}, [], "done")])
    monkeypatch.setattr("wizolt.engine.ModelClient", lambda _session: model)

    await _delegate_call(parent, _delegate_runner(parent), action="send", order="inspect it")

    assert len(sampled) == 1
    assert "[worker] default/worker-model" in sampled[0]
    assert "parent-model" not in sampled[0]
    # The worker's own fill and cache ratio, never the parent's.
    assert "ctx 30% · cache 50%" in sampled[0]
    # The delegation is over: the row returns to the parent's identity and usage, plus the
    # parked worker's own water level as the one worker fact it now carries.
    final = row()
    assert "default/parent-model" in final and "ctx 50% \u00b7 cache 25%" in final
    assert "[worker]" not in final and "worker ctx 30%" in final
