"""Host model access through real worker RPC, with network uncertainty mocked at HTTP."""

import asyncio
import json

import httpx2
import pytest
from agent_harness import session_with_provider
from openai import AsyncOpenAI

from wizolt.agent.engine import Agent
from wizolt.model import ModelClient
from wizolt.plugins.hostcalls import HostCalls
from wizolt.plugins.testing import PluginTrial, Stimulus
from wizolt.sdk import PluginError


def model_plugin(tmp_path):
    path = tmp_path / "helper.py"
    path.write_text("""SDK_VERSION = 1
def setup(p):
    async def ask(ctx, args):
        reply = await p.models.complete(args.get("input", "hello"), system="Be brief", model="helper-model", api="chat")
        return f"{reply.text}:{reply.model}:{reply.usage.input_tokens}"
    p.command("ask-helper", "Ask the helper", ask)
""")
    return str(path)


async def test_model_rpc_reuses_config_but_not_main_context_or_usage(tmp_path, monkeypatch):
    session = session_with_provider(tmp_path, api="chat")
    agent = Agent(session, output_fn=lambda _: None)
    session.messages.append({"role": "user", "content": "PRIVATE MAIN HISTORY"})
    received = []
    clients = []

    async def respond(request):
        received.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx2.Response(
            200,
            json={
                "id": "completion",
                "object": "chat.completion",
                "created": 1,
                "model": "helper-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
            },
        )

    def client(self, provider=None):
        result = AsyncOpenAI(api_key=provider.key, base_url=provider.url, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
        clients.append(result)
        return result

    monkeypatch.setattr(ModelClient, "client", client)
    try:
        await session.plugins.manage("enable", model_plugin(tmp_path))
        result = await session.plugins.invoke("helper", "command", "ask-helper", {"input": "question"})
        assert result == "answer:helper-model:12"
        assert received[0]["messages"] == [{"role": "system", "content": "Be brief"}, {"role": "user", "content": "question"}]
        assert not received[0].get("tools")
        assert session.config.provider.model == "gpt-4"
        assert session.usage.calls == session.usage.prompt_tokens == 0
        assert session.messages == [{"role": "user", "content": "PRIVATE MAIN HISTORY"}]
        assert all(client.is_closed() for client in clients)
    finally:
        await session.plugins.close()
        await agent.model.close()
        session.close()


async def test_cancelling_action_cancels_inflight_host_request(tmp_path):
    session = session_with_provider(tmp_path)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def request(operation, arguments):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    session.plugins.host_service = request
    try:
        await session.plugins.manage("enable", model_plugin(tmp_path))
        task = asyncio.create_task(session.plugins.invoke("helper", "command", "ask-helper", {}))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert not session.plugins.entries["helper"].active.worker.host_calls.pending
    finally:
        await session.plugins.close()
        session.close()


async def test_offline_trials_never_silently_make_paid_model_requests(tmp_path):
    session = session_with_provider(tmp_path)
    try:
        trial = PluginTrial(session.plugins.snapshot())
        report = await trial.run(model_plugin(tmp_path), stimuli=(Stimulus("command", "ask-helper"),))
        assert report.status == "failed"
        assert "offline trials" in report.error
    finally:
        await session.plugins.close()
        session.close()


@pytest.mark.parametrize("arguments", [{}, {"prompt": "hello", "system": "", "provider": "missing", "model": "", "effort": "", "api": ""}])
async def test_invalid_route_fails_before_provider_request(tmp_path, arguments):
    from wizolt.agent.plugin_models import PluginModels

    session = session_with_provider(tmp_path)
    try:
        with pytest.raises(PluginError):
            await PluginModels(session).call("model.complete", arguments)
    finally:
        session.close()


async def test_host_call_admission_and_concurrency_are_bounded():
    responses = []
    calls = HostCalls(responses.append, lambda parent: parent == 1)
    blocked = asyncio.Event()

    async def handler(operation, arguments):
        await blocked.wait()
        return {"ok": True}

    calls.handler = handler
    calls.dispatch({"service_id": 0, "parent": 2, "service": "model.complete", "arguments": {}})
    assert "active explicit action" in responses[-1]["error"]
    for identity in range(1, 6):
        calls.dispatch({"service_id": identity, "parent": 1, "service": "model.complete", "arguments": {}})
    assert len(calls.pending) == 4
    assert "Too many" in responses[-1]["error"]
    await calls.cancel(1)
    assert not calls.pending


async def test_continuation_cancel_clears_a_task_never_started():
    """A task cancelled before its first step never enters run()'s finally block.

    Cancel() must sweep it from `running` itself; the token is already consumed, so nothing
    else ever would, and every later cancel would re-cancel the dead task."""
    from wizolt.plugins.hostcalls import Continuations

    continuations = Continuations(lambda message: None, lambda parent: True)

    async def resume(value):
        return value

    continuations.register("t", 7, resume)
    continuations.dispatch({"continue": "t", "parent": 7, "input": {}})
    assert list(continuations.running) == ["t"]
    await continuations.cancel(7)
    assert not continuations.running and not continuations.tokens
    await continuations.cancel()  # A second sweep re-cancels nothing.
    assert not continuations.running


async def test_a_result_too_big_to_send_fails_next_at_once():
    """A frame the host cannot send degrades to a small error frame, as host services do.

    Otherwise the worker's next() future never resolves and the handler hangs to its own
    deadline with no reason."""
    from wizolt.plugins.hostcalls import Continuations
    from wizolt.plugins.process import FrameLimitError

    def send(message: dict) -> None:
        if "error" not in message:
            raise FrameLimitError("Plugin request exceeds protocol frame limit")
        sent.append(message)

    sent: list[dict] = []
    continuations = Continuations(send, lambda parent: True)

    async def resume(value):
        return {"huge": True}

    continuations.register("t", 7, resume)
    continuations.dispatch({"continue": "t", "parent": 7, "input": {}})
    await asyncio.sleep(0)
    assert sent == [{"continue_result": "t", "error": "FrameLimitError: Plugin request exceeds protocol frame limit"}]


async def test_a_result_the_host_cannot_encode_fails_next_at_once():
    """The rule is the frame, not the failure's class: a result the host cannot encode settles the
    same way, instead of being swallowed while the worker waits out its own deadline."""
    from wizolt.plugins.hostcalls import Continuations

    def send(message: dict) -> None:
        if "error" not in message:
            raise TypeError("Object of type set is not JSON serializable")
        sent.append(message)

    sent: list[dict] = []
    continuations = Continuations(send, lambda parent: True)

    async def resume(value):
        return {"unserializable": True}

    continuations.register("t", 7, resume)
    continuations.dispatch({"continue": "t", "parent": 7, "input": {}})
    await asyncio.sleep(0)
    assert sent == [{"continue_result": "t", "error": "TypeError: Object of type set is not JSON serializable"}]


async def test_a_service_result_the_host_cannot_send_is_reported():
    """Both reverse paths settle their call: a service result the host cannot send is an error frame
    the caller can raise, never an exception nobody sees."""
    from wizolt.plugins.hostcalls import HostCalls

    def send(message: dict) -> None:
        if "error" not in message:
            raise TypeError("Object of type set is not JSON serializable")
        sent.append(message)

    sent: list[dict] = []
    calls = HostCalls(send, lambda parent: True)

    async def handler(operation, arguments):
        return {"ok": True}

    calls.handler = handler
    calls.dispatch({"service_id": 1, "parent": 1, "service": "model.complete", "arguments": {}})
    await asyncio.sleep(0)
    assert sent == [{"service_result": 1, "error": "TypeError: Object of type set is not JSON serializable"}]
    await calls.cancel()
