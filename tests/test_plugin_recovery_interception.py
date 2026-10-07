"""Recovery never depends on a working model or chain: core /plugins disable cancels and restores."""

import asyncio
from types import SimpleNamespace

import pytest
from test_session_persistence import session_with_data_dir

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.ui.cli.plugins import plugins_command


@pytest.fixture
async def agent(tmp_path):
    session = session_with_data_dir(tmp_path)
    bootstrap_features(session)
    instance = Agent(session, output_fn=lambda _text: None)

    async def request(messages, tools, *, reason="normal"):
        return {"role": "assistant", "content": "ok"}, [], "ok"

    instance.model.request = request
    yield instance
    await session.plugins.close()


async def enable(agent, tmp_path, operation, body):
    path = tmp_path / "broken.py"
    path.write_text(f"import asyncio\nSDK_VERSION = 1\ndef setup(p):\n    async def h(ctx, value, next):\n        {body}\n    p.intercept({operation!r}, h)\n")
    await agent.session.plugins.manage("enable", str(path))


def loop_for(agent):
    return SimpleNamespace(session=agent.session, interactive_input=False, presentation=SimpleNamespace(tui=None))


@pytest.mark.parametrize("operation", ["prompt.submit", "context.compose"])
async def test_core_disable_recovers_from_a_broken_interceptor(agent, tmp_path, operation):
    await enable(agent, tmp_path, operation, "raise RuntimeError('broken interceptor')")
    with pytest.raises(Exception, match="broken interceptor"):
        await agent.run("first")
    with pytest.raises(Exception, match="blocked until it is reloaded or disabled"):
        await agent.run("second")
    # Slash commands never enter prompt.submit or any execution chain.
    assert '"status": "off"' in await plugins_command(loop_for(agent), "disable broken")
    assert await agent.run("third") == "ok"


async def test_a_background_agent_recovers_through_its_own_core_disable(agent, tmp_path, monkeypatch):
    from wizolt.model import ModelClient

    async def request(client, messages, tools=None, *, reason="normal"):
        return {"role": "assistant", "content": "ok"}, [], "ok"

    monkeypatch.setattr(ModelClient, "request", request)
    # The main agent's runtime is live before the child enables the plugin; it adopts saved
    # changes only on reload, so the broken registration is the child's alone.
    assert await agent.run("warm up") == "ok"
    group = agent.session.subagents
    entry = await group.spawn(agent.session, "worker", "warm up")
    child = entry.agent
    uid = child.session.uid
    await group.wait([uid], 5)
    path = tmp_path / "broken.py"
    path.write_text("SDK_VERSION = 1\ndef setup(p):\n    async def h(ctx, value, next):\n        raise RuntimeError('broken interceptor')\n    p.intercept('context.compose', h)\n")
    await child.session.plugins.manage("enable", str(path))

    await group.send(uid, "first")
    await group.wait([uid], 5)
    assert child.session.state.last_turn_status == "failed" and "broken interceptor" in child.session.state.last_turn_error
    # The child's runtime is its own: the main agent keeps working while the child is blocked.
    assert await agent.run("main still works") == "ok"
    assert '"status": "off"' in await plugins_command(loop_for(child), "disable broken")
    await group.send(uid, "second")
    await group.wait([uid], 5)
    assert child.session.state.last_turn_status != "failed" and "broken" not in child.session.plugins.entries


async def test_disable_cancels_a_hung_intercepted_operation(agent, tmp_path):
    await enable(agent, tmp_path, "prompt.submit", "await asyncio.Event().wait()")
    turn = asyncio.create_task(agent.run("stuck"))
    generation = agent.session.plugins.entries["broken"].active
    for _ in range(200):
        if generation.operations:
            break
        await asyncio.sleep(0.01)
    await plugins_command(loop_for(agent), "disable broken")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, 5)  # Cancelled now, not after the 60-second handler budget.
    assert "broken" not in agent.session.plugins.entries
    assert await agent.run("after") == "ok"
