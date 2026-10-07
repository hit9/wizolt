"""context.compact: plugins supply or shape summaries; core keeps authority and nothing falls back."""

import asyncio

import pytest
from agent_harness import session_with_provider

from wizolt.agent.compaction import Compactor
from wizolt.agent.engine import Agent
from wizolt.sdk import PluginError


def source(tmp_path, body='return Summary("Reviewed the implementation; one test remains.")', name="summaries", match=""):
    path = tmp_path / f"{name}.py"
    path.write_text(f"""from wizolt.sdk.operations import Summary
SDK_VERSION = 1
import asyncio
def setup(p):
    async def summarize(ctx, span, next):
        {body}
    p.intercept("context.compact", summarize{match})
""")
    return str(path)


@pytest.fixture
async def agent(tmp_path, monkeypatch):
    session = session_with_provider(tmp_path)
    instance = Agent(session, output_fn=lambda _: None)

    async def builtin(*args, **kwargs):
        return {}, [], '{"summary": "Builtin summary", "plan": ["finish the test"]}'

    monkeypatch.setattr(instance.model, "api_request", builtin)
    yield instance
    await session.plugins.close()
    await instance.model.close()
    session.close()


async def test_a_manual_compact_refreshes_the_window_parts_plugins_read(agent, tmp_path):
    """`/compact` changes the context without a request: a meter reading `Context.window` must see
    the shrunk conversation at once, not the evicted one beside the new total until the next send."""
    from wizolt.ui.cli import CommandLoop

    session = agent.session
    session.messages = [
        *({"role": role, "content": f"{role} {index} " + "detail " * 400} for index in range(12) for role in ("user", "assistant")),
        {"role": "user", "content": "now"},
    ]
    session.transcript_messages = list(session.messages)
    await session.plugins.manage("enable", source(tmp_path))
    output = []
    loop = CommandLoop(agent, output_fn=output.append)

    def messages_part() -> int:
        return dict(session.plugins.snapshot().window.parts)["messages"]

    before = messages_part()
    assert before > 20_000 // 4  # the plugin launch read the long conversation

    assert await loop.command("/compact") == (True, False)
    assert any("Compacted context" in str(line) for line in output)

    assert messages_part() < before // 2
    assert messages_part() == dict(agent.context.breakdown(session.system_prompt))["messages"]


async def test_a_window_sized_non_english_span_reaches_the_plugin(agent, tmp_path):
    await agent.session.plugins.manage("enable", source(tmp_path, "return Summary(f'digest of {len(span.text)} characters')"))
    # About 200k characters of Chinese fits a 200k-token window, yet escapes past 1 MiB of JSON.
    text = "用户：请检查解析器的错误处理，并补充测试。\n" * 9000
    data = await Compactor(agent.context, agent.model).compact(text)
    assert data == {"summary": f"digest of {len(text)} characters"}
    assert not agent.session.plugins.entries["summaries"].active.failures


async def test_core_applies_a_plugin_summary_and_keeps_tail_and_notes(agent, tmp_path):
    session = agent.session
    old = [{"role": "user", "content": "Investigate old code"}, {"role": "assistant", "content": "Checked implementation"}]
    keep = [{"role": "user", "content": "Now fix the test"}]
    session.messages = old + keep
    session.transcript_messages = list(session.messages)
    session.state.goal = "Keep this goal"
    await session.plugins.manage("enable", source(tmp_path))
    compactor = Compactor(agent.context, agent.model)
    assert compactor.request(old) is None  # An interceptor gets the flattened span, not the cache slice.
    assert await compactor.run(old, keep, "fallback")
    assert session.state.summaries[-1].endswith("]\nReviewed the implementation; one test remains.")
    assert session.state.goal == "Keep this goal" and session.messages[-1] == keep[0]
    assert session.transcript_messages == old + keep
    assert session.history[-1].model == "plugin:summaries"


async def test_wrapping_the_builtin_keeps_its_checkpoint_data(agent, tmp_path):
    await agent.session.plugins.manage("enable", source(tmp_path, "result = await next(span)\n        return result.replace(text=result.text + ' (reviewed)')"))
    data = await Compactor(agent.context, agent.model).compact("source")
    assert data == {"summary": "Builtin summary (reviewed)", "plan": ["finish the test"]}


@pytest.mark.parametrize("body", ['return Summary("")', 'raise RuntimeError("broken")', 'return Summary("repeated source " * 50)', "return 123"])
async def test_a_failed_interceptor_fails_compaction_without_any_fallback(agent, tmp_path, body):
    session = agent.session
    old = [{"role": "user", "content": "repeated source " * 50}]
    session.messages = list(old)
    await session.plugins.manage("enable", source(tmp_path, body))
    with pytest.raises(PluginError):
        await Compactor(agent.context, agent.model).run(old, [], "fallback")
    assert session.messages == old and session.state.summary == "" and not session.history  # Nothing trimmed.
    assert "intercept:context.compact" in session.plugins.entries["summaries"].active.failures


async def test_compaction_records_what_actually_ran(agent, tmp_path):
    receipts = agent.session.operation_receipts
    await agent.session.plugins.manage("enable", source(tmp_path))  # Answers without the builtin.
    await Compactor(agent.context, agent.model).compact("source")
    assert (receipts[-1].operation, receipts[-1].core, receipts[-1].origin) == ("context.compact", "not_run", "summaries/context.compact")
    await agent.session.plugins.manage("disable", "summaries")
    body = "await next(span)\n        raise RuntimeError('lost the summary')"
    await agent.session.plugins.manage("enable", source(tmp_path, body, name="late"))
    with pytest.raises(PluginError):
        await Compactor(agent.context, agent.model).compact("source")
    # The builtin summary had run when the plugin failed; the receipt says so, and why it failed.
    assert receipts[-1].core == "completed" and "lost the summary" in receipts[-1].wrapper_failure and receipts[-1].delivered


async def test_trigger_matcher_and_manual_compaction(agent, tmp_path):
    await agent.session.plugins.manage("enable", source(tmp_path, match=", match={'trigger': 'manual'}"))
    compactor = Compactor(agent.context, agent.model)
    assert (await compactor.compact("source"))["summary"] == "Builtin summary"
    assert (await compactor.compact("source", trigger="manual"))["summary"] == "Reviewed the implementation; one test remains."


async def test_cancelled_compaction_releases_a_pending_reload(agent, tmp_path):
    session = agent.session
    await session.plugins.manage("enable", source(tmp_path, "await asyncio.Event().wait()"))
    task = asyncio.create_task(Compactor(agent.context, agent.model).compact("source"))
    for _ in range(100):
        if session.plugins.entries["summaries"].active.invocations:
            break
        await asyncio.sleep(0.01)
    source(tmp_path)
    assert (await session.plugins.manage("reload", "summaries"))["status"] == "reloading"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await Compactor(agent.context, agent.model).compact("source"))["summary"].startswith("Reviewed")


async def test_a_summary_can_use_host_models_with_working_state(agent, tmp_path):
    session = agent.session
    captured = []

    async def complete(operation, arguments):
        captured.append(arguments["prompt"])
        return {"text": "One check remains", "model": "helper", "usage": {}}

    session.plugins.host_service = complete
    session.state.goal = "Preserve this goal"
    session.state.summary = "Earlier finding"
    await session.plugins.manage("enable", source(tmp_path, "return Summary((await p.models.complete(span.text)).text)"))
    assert await Compactor(agent.context, agent.model).run([{"role": "user", "content": "Review the code"}], [], "fallback")
    assert "Preserve this goal" in captured[0] and "Earlier finding" in captured[0]
    # A plugin summary is kept beside the earlier one, as the builtin's is; neither is rewritten.
    assert session.state.summaries == ["Earlier finding", "[seg.1: Review the code]\nOne check remains"]
