"""model.request: one logical boundary for every purpose, routing, retries and preview rules."""

import json
from dataclasses import replace

import pytest
from model_harness import _MockClientFactory, _session, record_backoff

from wizolt.agent.engine import Agent
from wizolt.base import ModelError, ModelRequestRetry
from wizolt.model import ModelClient
from wizolt.sdk import PluginError

LOGGER = """
import json
from wizolt.sdk.operations import ModelResponse, ModelToolCall, Refusal
SDK_VERSION = 1
def setup(p):
    async def h(ctx, request, next):
        with open(LOG, "a") as log:
            log.write(json.dumps({{"purpose": request.purpose, "reason": request.reason, "id": request.id, "retry_of": request.retry_of}}) + "\\n")
{body}
    p.intercept("model.request", h{options})
"""


@pytest.fixture
def session(tmp_path):
    instance = _session(tmp_path)
    instance.config.providers["alt"] = replace(instance.config.providers["default"], model="alt-model")
    return instance


async def enable(session, tmp_path, body="        return await next(request)", options=""):
    log = tmp_path / "requests.log"
    path = tmp_path / "router.py"
    path.write_text(f"LOG = {str(log)!r}\n" + LOGGER.format(body=body, options=options))
    await session.plugins.manage("enable", str(path))
    return log


def entries(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def fake_transport(model, sent, text="hello"):
    async def api_request(messages, tools, **kwargs):
        provider = kwargs.get("provider") or model.session.config.provider
        sent.append(provider.model)
        model._emit_stream("text", text)
        return {"role": "assistant", "content": text}, [], text

    model.api_request = api_request


async def test_router_changes_the_route_for_this_request_only(session, tmp_path):
    await enable(session, tmp_path, body="        return await next(request.replace(provider='alt'))", options=", match={'purpose': 'turn'}")
    model, sent = ModelClient(session), []
    fake_transport(model, sent)
    _, _, content = await model.request([{"role": "user", "content": "hi"}], [])
    assert content == "hello" and sent == ["alt-model"]
    assert session.config.active_provider == "default"  # No session-default mutation.
    receipt = session.operation_receipts[-1]
    assert receipt.operation == "model.request" and '"default"' in receipt.original and '"alt"' in receipt.effective


async def test_a_failing_model_interceptor_records_why_on_the_receipt(session, tmp_path):
    """A plugin that fails before next() never reaches the provider. The receipt must say so, and
    name the wrapper failure, or resume/replay shows the failed request with no reason while a
    failed tool call shows one (the receipt contract in design/PLUGIN_INTERCEPTION.md)."""
    await enable(session, tmp_path, body="        raise RuntimeError('bad route')")
    model = ModelClient(session)

    with pytest.raises(PluginError, match="bad route"):
        await model.request([{"role": "user", "content": "hi"}], [])

    receipt = session.operation_receipts[-1]
    assert receipt.core == "not_run" and "bad route" in receipt.wrapper_failure


async def test_transport_retries_reuse_the_request_without_rerunning_handlers(session, tmp_path, monkeypatch):
    log = await enable(session, tmp_path)
    model = ModelClient(session)
    ok = {"id": "c", "object": "chat.completion", "created": 1, "model": "gpt-4", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
    factory = _MockClientFactory([(429, {"error": {"message": "rate limited", "type": "rate_limit_error"}}), (200, ok)])
    monkeypatch.setattr(model, "client", factory)
    record_backoff(monkeypatch)
    _, _, content = await model.request([{"role": "user", "content": "hi"}], None)
    assert content == "ok" and len(factory.calls) == 2 and len(entries(log)) == 1


async def test_manual_retry_starts_a_new_request_linked_to_the_old_one(session, tmp_path):
    log = await enable(session, tmp_path)
    model, attempts = ModelClient(session), []

    async def transport(messages, tools, provider):
        attempts.append(provider)
        if len(attempts) == 1:
            raise ModelRequestRetry()
        return {"role": "assistant", "content": "second"}, [], "second"

    model._transport_request = transport
    with pytest.raises(ModelRequestRetry):
        await model.request([{"role": "user", "content": "hi"}], [])
    await model.request([{"role": "user", "content": "hi"}], [])
    first, second = entries(log)
    assert second["retry_of"] == first["id"] and first["retry_of"] == ""


async def test_a_retry_never_resent_does_not_link_the_next_turn(session, tmp_path):
    log = await enable(session, tmp_path)
    agent = Agent(session, output_fn=lambda _text: None)
    attempts = []

    async def transport(messages, tools, provider):
        attempts.append(provider)
        if len(attempts) == 1:
            raise ModelRequestRetry()  # Claimed, but its turn ends before the rebuilt request.
        return {"role": "assistant", "content": "ok"}, [], "ok"

    agent.model._transport_request = transport
    with pytest.raises(ModelRequestRetry):
        await agent.model.request([{"role": "user", "content": "hi"}], [])
    await agent.run("a fresh turn")
    abandoned, fresh = entries(log)
    assert fresh["retry_of"] == "" and abandoned["id"]  # The new turn replaces nothing.


async def test_a_retry_links_only_to_a_request_interception_saw(session, tmp_path):
    log = await enable(session, tmp_path)
    model, attempts = ModelClient(session), []

    async def transport(messages, tools, provider):
        attempts.append(provider)
        if len(attempts) == 2:
            raise ModelRequestRetry()
        return {"role": "assistant", "content": "ok"}, [], "ok"

    model._transport_request = transport
    await model.request([{"role": "user", "content": "hi"}], [])  # Seen: it has an ID.
    await session.plugins.manage("disable", "router")
    with pytest.raises(ModelRequestRetry):
        await model.request([{"role": "user", "content": "hi"}], [])  # Unseen: no ID to link.
    await enable(session, tmp_path)
    await model.request([{"role": "user", "content": "hi"}], [])
    first, retried = entries(log)
    assert retried["retry_of"] == "" and retried["id"] != first["id"]


async def test_preserve_streams_while_replace_shows_only_the_final_response(session, tmp_path):
    streamed = []
    model, sent = ModelClient(session), []
    model.hooks.on_stream = lambda kind, delta: streamed.append(delta)
    fake_transport(model, sent, text="original")
    await enable(session, tmp_path)
    await model.request([{"role": "user", "content": "hi"}], [])
    assert streamed == ["original"]
    await session.plugins.manage("disable", "router")
    replacing = "        result = await next(request)\n        return result.replace(text=result.text.upper())"
    await enable(session, tmp_path, body=replacing, options=", response='replace'")
    streamed.clear()
    _, _, content = await model.request([{"role": "user", "content": "hi"}], [])
    assert content == "ORIGINAL" and streamed == []  # The rewritten answer never streamed its draft.


@pytest.mark.parametrize(
    "body, reason",
    [
        ("        return await next(request.replace(provider='missing'))", "Unknown provider entry: missing"),
        ("        return await next(request.replace(effort='ludicrous'))", "does not accept effort 'ludicrous'"),
        ("        return ModelResponse('x', (ModelToolCall('1', 'Nope', {}),))", "does not offer: Nope"),
    ],
)
async def test_invalid_routes_and_responses_block_the_registration(session, tmp_path, body, reason):
    await enable(session, tmp_path, body=body, options=", response='replace'")
    model, sent = ModelClient(session), []
    fake_transport(model, sent)
    with pytest.raises(Exception, match=reason):
        await model.request([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "Bash"}}])
    assert sent == []
    with pytest.raises(Exception, match="blocked until it is reloaded"):
        await model.request([{"role": "user", "content": "hi"}], [])


async def test_refusal_fails_the_request_before_anything_is_sent(session, tmp_path):
    await enable(session, tmp_path, body="        return Refusal('over budget today')")
    model, sent = ModelClient(session), []
    fake_transport(model, sent)
    with pytest.raises(ModelError, match="refused by plugin router: over budget today"):
        await model.request([{"role": "user", "content": "hi"}], [])
    assert sent == []


async def test_receipts_tell_a_sent_request_from_one_that_never_left(session, tmp_path):
    await enable(session, tmp_path, body="        raise RuntimeError('broken router')")
    model, sent = ModelClient(session), []
    fake_transport(model, sent)
    with pytest.raises(Exception, match="broken router"):
        await model.request([{"role": "user", "content": "hi"}], [])
    assert sent == [] and session.operation_receipts[-1].core == "not_run"

    await session.plugins.manage("disable", "router")
    await enable(session, tmp_path)
    seen = []

    async def transport(messages, tools, provider):
        # Mid-send the receipt says started, so a crash here resumes as unknown, never not_run.
        seen.append(session.operation_receipts[-1].core)
        raise ModelError("provider down")

    model._transport_request = transport
    with pytest.raises(ModelError, match="provider down"):
        await model.request([{"role": "user", "content": "hi"}], [])
    assert seen == ["started"] and session.operation_receipts[-1].core == "failed"


async def test_every_purpose_reaches_the_one_boundary(session, tmp_path, monkeypatch):
    from wizolt.agent.compaction import Compactor
    from wizolt.agent.context import ContextManager

    log = await enable(session, tmp_path)
    Agent(session, output_fn=lambda _text: None)  # Wires plugin host services, as the CLI does.

    async def api_request(self, messages, tools, **kwargs):
        text = json.dumps({"summary": "short"}) if kwargs.get("json_object") else "fine"
        return {"role": "assistant", "content": text}, [], text

    monkeypatch.setattr(ModelClient, "api_request", api_request)
    model = ModelClient(session)
    await model.request([{"role": "user", "content": "hi"}], [])
    await model.request([{"role": "user", "content": "hi"}], [], reason="tool_correction")
    await Compactor(ContextManager(session), model).compact("user: a long conversation")
    await session.plugins.host_service("model.complete", {"prompt": "aux", "system": "", "provider": "", "model": "", "effort": "", "api": ""})
    assert [(item["purpose"], item["reason"]) for item in entries(log)] == [("turn", "normal"), ("turn", "tool_correction"), ("compaction", "normal"), ("plugin", "normal")]


async def test_plugin_requests_get_the_same_route_checks(session, tmp_path, monkeypatch):
    await enable(session, tmp_path, body="        return await next(request.replace(effort='ludicrous'))", options=", match={'purpose': 'plugin'}")
    Agent(session, output_fn=lambda _text: None)  # Wires plugin host services, as the CLI does.
    sent = []

    async def api_request(self, messages, tools, **kwargs):
        sent.append(kwargs.get("provider"))
        return {"role": "assistant", "content": "fine"}, [], "fine"

    monkeypatch.setattr(ModelClient, "api_request", api_request)
    with pytest.raises(Exception, match="does not accept effort 'ludicrous'"):
        await session.plugins.host_service("model.complete", {"prompt": "aux", "system": "", "provider": "", "model": "", "effort": "", "api": ""})
    assert sent == []


async def test_a_handler_auxiliary_request_skips_its_own_registration(session, tmp_path, monkeypatch):
    body = (
        "        if request.purpose == 'turn':\n"
        "            await p.models.complete('classify this')\n"
        "        return await next(request)"
    )
    log = await enable(session, tmp_path, body=body)
    Agent(session, output_fn=lambda _text: None)

    async def api_request(self, messages, tools, **kwargs):
        return {"role": "assistant", "content": "fine"}, [], "fine"

    monkeypatch.setattr(ModelClient, "api_request", api_request)
    await ModelClient(session).request([{"role": "user", "content": "hi"}], [])
    assert [item["purpose"] for item in entries(log)] == ["turn"]  # The nested plugin request skipped it.
