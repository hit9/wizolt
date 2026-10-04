"""Summary strategies cannot acquire authority over history, notes or tool boundaries."""

import asyncio

import pytest
from agent_harness import session_with_provider

from wizolt.agent.compaction import Compactor
from wizolt.agent.engine import Agent
from wizolt.plugins.testing import PluginTrial, Stimulus
from wizolt.sdk import PluginError


def source(tmp_path, body='return "Reviewed the implementation; one test remains."', name="summaries"):
    path = tmp_path / f"{name}.py"
    path.write_text(f"""SDK_VERSION = 1
import asyncio
def setup(p):
    async def summarize(ctx, text):
        {body}
    p.summarizer(summarize)
""")
    return str(path)


async def test_summarizer_receives_a_window_sized_non_english_span(tmp_path):
    session = session_with_provider(tmp_path)
    try:
        await session.plugins.manage("enable", source(tmp_path, "return f'digest of {len(text)} characters'"))
        # About 200k characters of Chinese fits a 200k-token window, yet escapes past 1 MiB of JSON.
        text = "用户：请检查解析器的错误处理，并补充测试。\n" * 9000
        assert await session.plugins.summarize(text, lambda _: None) == ("summaries", f"digest of {len(text)} characters")
        assert session.plugins.has_summarizer  # Still healthy, not marked failed by a host limit.
    finally:
        await session.plugins.close()
        session.close()


async def test_core_applies_plugin_summary_and_retains_recent_tail_and_notes(tmp_path):
    session = session_with_provider(tmp_path)
    agent = Agent(session, output_fn=lambda _: None)
    old = [{"role": "user", "content": "Investigate old code"}, {"role": "assistant", "content": "Checked implementation"}]
    keep = [{"role": "user", "content": "Now fix the test"}]
    session.messages = old + keep
    session.transcript_messages = list(session.messages)
    session.state.goal = "Keep this goal"
    try:
        await session.plugins.manage("enable", source(tmp_path))
        compactor = Compactor(agent.context, agent.model)
        assert compactor.request(old) is None
        assert await compactor.run(old, keep, "fallback")
        assert session.state.summary == "Reviewed the implementation; one test remains."
        assert session.state.goal == "Keep this goal"
        assert session.messages[-1] == keep[0]
        assert session.transcript_messages == old + keep
        assert session.history[-1].model == "plugin:summaries"
        assert session.usage.calls == session.compaction_usage.calls == 0
    finally:
        await session.plugins.close()
        await agent.model.close()
        session.close()


@pytest.mark.parametrize("body", ['return ""', 'raise RuntimeError("broken")', 'return "repeated source " * 50', "return 123"])
async def test_invalid_summary_falls_back_to_builtin_without_poisoning_history(tmp_path, monkeypatch, body):
    session = session_with_provider(tmp_path)
    agent = Agent(session, output_fn=lambda _: None)

    async def response(*args, **kwargs):
        return {}, [], '{"summary": "Builtin recovery"}'

    monkeypatch.setattr(agent.model, "api_request", response)
    try:
        await session.plugins.manage("enable", source(tmp_path, body))
        compactor = Compactor(agent.context, agent.model)
        data = await compactor.compact("source", echo_source="repeated source " * 50)
        assert data == {"summary": "Builtin recovery"}
        assert not session.plugins.has_summarizer
        assert "summarizer:" in (await session.plugins.manage("inspect", "summaries"))["plugins"][0]["error"]
        assert session.state.summary == ""
    finally:
        await session.plugins.close()
        await agent.model.close()
        session.close()


async def test_one_strategy_and_cancelled_manual_compaction_releases_reload(tmp_path):
    session = session_with_provider(tmp_path)
    try:
        await session.plugins.manage("enable", source(tmp_path, "await asyncio.Event().wait()"))
        with pytest.raises(PluginError, match="already supplied"):
            await session.plugins.manage("enable", source(tmp_path, name="other"))
        task = asyncio.create_task(session.plugins.summarize("source", lambda _: None))
        await asyncio.sleep(0)  # Begin the invocation before staging a replacement.
        source(tmp_path)
        result = await session.plugins.manage("reload", "summaries")
        assert result["status"] == "reloading"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await session.plugins.summarize("source", lambda _: None)
        assert result == ("summaries", "Reviewed the implementation; one test remains.")
    finally:
        await session.plugins.close()
        session.close()


async def test_summary_authoring_trial_returns_text(tmp_path):
    session = session_with_provider(tmp_path)
    try:
        report = await PluginTrial(session.plugins.snapshot()).run(source(tmp_path), stimuli=(Stimulus("summarizer", "", {"text": "source"}),))
        assert report.status == "passed", report.error
        assert report.results == ["Reviewed the implementation; one test remains."]
    finally:
        await session.plugins.close()
        session.close()


async def test_summary_can_use_host_models_with_working_state(tmp_path):
    session = session_with_provider(tmp_path)
    agent = Agent(session, output_fn=lambda _: None)
    captured = []

    async def complete(operation, arguments):
        captured.append(arguments["prompt"])
        return {"text": "One check remains", "model": "helper", "usage": {}}

    session.plugins.host_service = complete
    session.state.goal = "Preserve this goal"
    session.state.summary = "Earlier finding"
    old = [{"role": "user", "content": "Review the code"}]
    try:
        await session.plugins.manage("enable", source(tmp_path, "return (await p.models.complete(text)).text"))
        compactor = Compactor(agent.context, agent.model)
        assert await compactor.run(old, [], "fallback")
        assert "Preserve this goal" in captured[0]
        assert "Earlier finding" in captured[0]
        assert session.state.summary == "One check remains"
    finally:
        await session.plugins.close()
        await agent.model.close()
        session.close()
