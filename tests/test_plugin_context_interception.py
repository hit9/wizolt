"""context.compose through the real engine: editable blocks, plugin blocks, read-only history."""

import pytest
from test_session_persistence import session_with_data_dir

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.base import ModelError


@pytest.fixture
async def agent(tmp_path):
    session = session_with_data_dir(tmp_path)
    bootstrap_features(session)
    session.system_prompt = "You are wizolt."
    instance = Agent(session, output_fn=lambda _text: None)
    instance.requests = []

    async def request(messages, tools, *, reason="normal"):
        instance.requests.append(messages)
        return {"role": "assistant", "content": "ok"}, [], "ok"

    instance.model.request = request
    yield instance
    await session.plugins.close()


async def enable(agent, tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text("from wizolt.sdk.operations import Blocks\nSDK_VERSION = 1\ndef setup(p):\n    async def h(ctx, blocks, next):\n" + body + "\n    p.intercept('context.compose', h)\n")
    await agent.session.plugins.manage("enable", str(path))


NOTES = "        blocks = blocks.with_text('system', blocks.get('system').text + ' Prefer small diffs.')\n        return await next(blocks.add('today', 'Release freeze is on.'))"


async def test_edits_and_plugin_blocks_reach_the_request_not_the_history(agent, tmp_path):
    agent.session.settings.language = "French"
    await enable(agent, tmp_path, "notes", NOTES)
    await agent.run("hello")
    sent = agent.requests[0]
    assert sent[0]["role"] == "system" and "You are wizolt. Prefer small diffs." in sent[0]["content"] and "French" in sent[0]["content"]
    contents = [message["content"] for message in sent]
    freeze, user = contents.index("Release freeze is on."), contents.index("hello")
    assert contents[freeze - 1].startswith("--- Environment ---") and freeze < user  # After the header.
    assert all("Release freeze" not in str(message.get("content")) for message in agent.session.messages)
    receipt = agent.session.operation_receipts[-1]
    assert receipt.operation == "context.compose" and '"system"' in receipt.effective and "plugin:notes:today" in receipt.effective


async def test_the_status_bar_counts_what_composition_sends(agent, tmp_path):
    await agent.run("hello")
    baseline = agent.session.state.context_tokens
    await enable(agent, tmp_path, "wide", "        return await next(blocks.add('notes', 'decision ' * 4000))")
    await agent.run("hello")
    # The added block is sent on this request, so the reported context includes it.
    assert agent.session.state.context_tokens - baseline >= 3000


@pytest.mark.parametrize(
    "body, reason",
    [
        ("        return await next(blocks.with_text('environment', 'forged'))", "environment is read-only"),
        ("        return await next(Blocks(tuple(b for b in blocks.blocks if b.id != 'conversation'), blocks.purpose))", "conversation must stay"),
    ],
)
async def test_read_only_blocks_cannot_change(agent, tmp_path, body, reason):
    await enable(agent, tmp_path, "rogue", body)
    with pytest.raises(Exception, match=reason):
        await agent.run("hello")


async def test_plugins_own_only_their_blocks_and_order_follows_the_user(agent, tmp_path):
    await enable(agent, tmp_path, "alpha", "        return await next(blocks.add('a', 'from alpha'))")
    await enable(agent, tmp_path, "beta", "        return await next(blocks.add('b', 'from beta'))")
    await agent.run("one")
    contents = [message["content"] for message in agent.requests[0]]
    assert contents.index("from alpha") < contents.index("from beta")
    agent.session.plugins.interception_order.save(["beta"])
    await agent.run("two")
    contents = [message["content"] for message in agent.requests[1]]
    assert contents.index("from beta") < contents.index("from alpha")
    await agent.session.plugins.manage("disable", "alpha")
    await enable(agent, tmp_path, "thief", "        return await next(blocks.with_text('plugin:beta:b', 'stolen'))")
    agent.session.plugins.interception_order.save(["beta", "thief"])
    with pytest.raises(Exception, match="Only beta may change"):
        await agent.run("three")


async def test_identical_input_gives_an_identical_projection(agent, tmp_path):
    await enable(agent, tmp_path, "notes", NOTES)
    await agent.run("same")
    agent.session.messages.clear()
    await agent.run("same")
    assert agent.requests[0] == agent.requests[1]  # No host timestamps or random IDs: cache-stable.


async def test_composition_over_budget_fails_explicitly(agent, tmp_path):
    agent.session.settings.max_context_tokens = 4000
    await enable(agent, tmp_path, "huge", "        return await next(blocks.add('big', 'word ' * 20000))")
    with pytest.raises(ModelError, match="does not fit the context budget"):
        await agent.run("hello")
    assert agent.requests == []


async def test_a_header_part_appearing_during_a_fruitless_compaction_keeps_the_conversation(agent, tmp_path, monkeypatch):
    from wizolt.agent import compaction
    from wizolt.agent.context import ContextManager

    await enable(agent, tmp_path, "relay", "        return await next(blocks)")
    connected = {"skills": ""}
    monkeypatch.setattr(ContextManager, "skills_context", lambda self: connected["skills"])

    async def fruitless(self, *args, **kwargs):
        connected["skills"] = "--- Skills ---\nreview"  # Arrives while the summary request is out.
        return False  # And the pass frees nothing.

    monkeypatch.setattr(compaction.Compactor, "run", fruitless)
    monkeypatch.setattr(ContextManager, "request_token_budget", lambda self: 1)  # Force an attempt.
    monkeypatch.setattr(ContextManager, "update_percent", lambda self, *args, **kwargs: 0)
    context = agent.context
    turn = [{"role": "user", "content": "OPENING-MESSAGE"}]
    messages = await context.prepare_messages(agent.model, agent.session.system_prompt, turn, [])
    monkeypatch.setattr(ContextManager, "request_token_budget", lambda self: 10**9)
    composed = await context.compose(agent.session.plugins, agent.session.system_prompt, messages, [])
    contents = [message["content"] for message in composed]
    # The opening message stays the conversation's first; no header block is resent as history.
    assert "OPENING-MESSAGE" in contents and contents.count("--- Skills ---\nreview") == 1


async def test_a_header_part_appearing_after_a_successful_pass_keeps_the_conversation(agent, tmp_path, monkeypatch):
    """A successful pass rebuilds the request, but the next pass awaits again: a header part that
    arrives during that window must reach the projection compose splits by the header's parts."""
    from wizolt.agent import compaction
    from wizolt.agent.context import ContextManager

    await enable(agent, tmp_path, "relay", "        return await next(blocks)")
    connected = {"skills": ""}
    monkeypatch.setattr(ContextManager, "skills_context", lambda self: connected["skills"])
    passes: list[str] = []

    async def run(self, *args, **kwargs):
        passes.append(self.ctx.session.uid)
        if len(passes) == 1:
            return True  # The history pass compacted: the request is rebuilt from the header it saw.
        connected["skills"] = "--- Skills ---\nreview"  # Arrives while the second pass is out.
        return False  # And this one frees nothing.

    monkeypatch.setattr(compaction.Compactor, "run", run)
    monkeypatch.setattr(ContextManager, "request_token_budget", lambda self: 1)  # Force the passes.
    monkeypatch.setattr(ContextManager, "update_percent", lambda self, *args, **kwargs: 0)
    context = agent.context
    turn = [{"role": "user", "content": "OPENING-MESSAGE"}]
    messages = await context.prepare_messages(agent.model, agent.session.system_prompt, turn, [])
    assert len(passes) == 2  # The fruitless turn pass ran after the successful history pass.
    monkeypatch.setattr(ContextManager, "request_token_budget", lambda self: 10**9)
    composed = await context.compose(agent.session.plugins, agent.session.system_prompt, messages, [])
    contents = [message["content"] for message in composed]
    # The opening message stays the conversation's first; no header block is resent as history.
    assert "OPENING-MESSAGE" in contents and contents.count("--- Skills ---\nreview") == 1


async def test_conversation_is_a_read_only_view(agent, tmp_path):
    seen = tmp_path / "seen.txt"
    await enable(agent, tmp_path, "reader", f"        open({str(seen)!r}, 'w').write(blocks.get('conversation').text)\n        return await next(blocks)")
    await agent.run("what changed?")
    assert "user: what changed?" in seen.read_text()
