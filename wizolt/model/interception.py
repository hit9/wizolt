"""The one logical model-request boundary that ``model.request`` interceptors wrap.

Every caller that sends a model request routes it here exactly once: agent steps, image-fallback
resends, textual-tool corrections, vision observations, builtin compaction and plugins'
``models.complete``. ``send(provider)`` performs the actual call, including its bounded transport
retries, below this boundary. Purpose, billing owner and identities are host-owned; a route
override is request-local and validated before anything is sent. Responses are complete and
bounded; live preview is suppressed only when a response-replacing registration matches.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from wizolt.base import Json, ModelError, ToolCall
from wizolt.sdk import PluginError

if TYPE_CHECKING:
    from wizolt.config import Config, ProviderConfig
    from wizolt.sdk.operations import ModelResponse, Value
    from wizolt.session.types import OperationReceipt

Result = tuple[Json, list[ToolCall], str]
Send = Callable[["ProviderConfig"], Awaitable[Result]]


def tool_names(tools: list[Json] | None) -> tuple[str, ...]:
    names = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else tool.get("name") if isinstance(tool, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def response_of(result: Result, model: str, entry: str) -> ModelResponse:
    from wizolt.sdk.operations import ModelResponse, ModelToolCall
    from wizolt.sdk.settings import freeze

    _, calls, content = result
    return ModelResponse(
        content,
        tuple(ModelToolCall(call.id, call.name, freeze(dict(call.payload) if isinstance(call.payload, dict) else {})) for call in calls),
        entry,
        model,
    )


def result_of(response: ModelResponse) -> Result:
    """A replacement response in the host's own shape; tool calls are parsed like the model's."""
    from wizolt.model.client import ModelClient
    from wizolt.sdk.operations import thaw

    calls = [ModelClient.tool_call(call.id, call.name, thaw(call.arguments)) for call in response.tool_calls]
    assistant: Json = {"role": "assistant", "content": response.text}
    if calls:
        assistant["tool_calls"] = [
            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(thaw(item.arguments), ensure_ascii=False)}}
            for call, item in zip(calls, response.tool_calls, strict=True)
        ]
    return assistant, calls, response.text


def route_check(session: Any, client: Any, messages: list[Json], tools: list[Json] | None) -> Callable[[ProviderConfig, str], None]:
    """A rerouted request's model limits; credentials are checked where the route is built.

    Only an effort the handler chose is checked: a stored one keeps its usual nearest-level mapping.
    """

    def check(effective: ProviderConfig, chosen_effort: str) -> None:
        if chosen_effort and chosen_effort not in (choices := session.policy.reasoning_choices(effective)):
            raise PluginError(f"{effective.model} does not accept effort {chosen_effort!r}; choose {', '.join(choices)}")
        limit = effective.context_token_limit(session.settings.max_context_tokens)
        if limit and client.estimated_request_tokens(messages, tools) > limit:
            raise PluginError(f"The request does not fit {effective.model}'s {limit}-token context")

    return check


async def client_request(
    client: Any,
    purpose: str,
    messages: list[Json],
    tools: list[Json] | None,
    *,
    provider: ProviderConfig,
    entry: str,
    send: Send,
    reason: str = "normal",
    retry_of: str = "",
    on_request: Callable[[str], None] | None = None,
) -> Result:
    """A session-bound client's logical request: its plugins, budget check, preview and receipts."""
    session = client.session
    return await logical_request(
        session.plugins,
        session.config,
        purpose=purpose,
        messages=messages,
        tools=tools,
        provider=provider,
        entry=entry,
        send=send,
        reason=reason,
        retry_of=retry_of,
        check_route=route_check(session, client, messages, tools),
        suppress_preview=getattr(client, "suppressed_preview", None),
        record=session.record_operation,
        checkpoint=session.save_snapshot,
        on_request=on_request,
    )


async def logical_request(
    plugins: Any,
    config: Config,
    *,
    purpose: str,
    messages: list[Json],
    tools: list[Json] | None,
    provider: ProviderConfig,
    entry: str,
    send: Send,
    reason: str = "normal",
    retry_of: str = "",
    check_route: Callable[[ProviderConfig, str], None] | None = None,
    suppress_preview: Callable[[], AbstractContextManager] | None = None,
    record: Callable[[OperationReceipt], OperationReceipt] | None = None,
    checkpoint: Callable[[], Awaitable[object]] | None = None,
    on_request: Callable[[str], None] | None = None,
) -> Result:
    interception = getattr(plugins, "interception", None)
    if interception is None or not interception.would_match("model.request", purpose=purpose, reason=reason):
        return await send(provider)
    # Imported once a registration matches: requests without interceptors never pay for them.
    from wizolt.plugins.rules import offered_tools
    from wizolt.sdk.operations import ModelRequest, ModelResponse, Refusal
    from wizolt.session.types import OperationReceipt

    value = ModelRequest(uuid.uuid4().hex, purpose, reason, entry, provider.model, provider.reasoning or "", tool_names(tools), len(messages), retry_of)
    if on_request is not None:
        on_request(value.id)

    def route(candidate: Value) -> ProviderConfig:
        """The provider a candidate names. Overrides never touch session defaults."""
        assert isinstance(candidate, ModelRequest)
        if (candidate.provider, candidate.model, candidate.effort) == (value.provider, value.model, value.effort):
            return provider
        name = candidate.provider or entry
        base = provider if name == entry else config.providers.get(name)
        if base is None:
            raise PluginError(f"Unknown provider entry: {name}")
        # Only fields the handler changed override; switching entry alone uses that entry's model.
        model = candidate.model if candidate.model and candidate.model != value.model else base.model
        chosen = candidate.effort if candidate.effort and candidate.effort != value.effort else ""
        effective = replace(base, model=model, reasoning=chosen or base.reasoning)
        if missing := effective.missing_fields():
            raise PluginError(f"Provider {name} is missing {', '.join(missing)}")
        if check_route is not None:
            check_route(effective, chosen)
        return effective

    downstream: dict = {}

    async def core(candidate: Value) -> Value:
        assert isinstance(candidate, ModelRequest)
        effective = route(candidate)
        receipt.core = "started"
        if checkpoint is not None:
            await checkpoint()  # Durable before sending: a crash from here resumes as unknown, never not_run.
        result = await send(effective)
        downstream["raw"] = result
        downstream["response"] = response = response_of(result, effective.model, candidate.provider or entry)
        receipt.effective = OperationReceipt.clip({"provider": candidate.provider or entry, "model": effective.model, "effort": effective.reasoning or ""})
        return response

    original = OperationReceipt.clip({"provider": entry, "model": provider.model, "effort": provider.reasoning or ""})
    receipt = OperationReceipt(value.id, "model.request", f"{purpose}/{reason}", original, retry_of=retry_of)
    if record is not None:
        record(receipt)
    replaces = interception.replaces("model.request", value)
    trace: dict = {}
    try:
        with suppress_preview() if replaces and suppress_preview is not None else contextlib.nullcontext():
            result = await interception.run(
                "model.request",
                value,
                core,
                transition=lambda _previous, candidate, _owner: route(candidate) and None,
                result_check=offered_tools(value),
                trace=trace,
            )
    except BaseException:
        # A plugin failure before next() never reached the provider: core stays not_run.
        receipt.core = "completed" if "raw" in downstream else "failed" if receipt.core == "started" else "not_run"
        raise
    finally:
        receipt.origin, receipt.shaped, receipt.delivered = trace.get("origin", "core"), tuple(trace.get("shaped", ())), True
    receipt.core = "completed" if "raw" in downstream else "not_run"
    if isinstance(result, Refusal):
        raise ModelError(f"model request refused by plugin {receipt.origin.partition('/')[0]}: {result.reason}")
    assert isinstance(result, ModelResponse)
    if "response" in downstream and result == downstream["response"]:
        return downstream["raw"]  # Unchanged: the provider's own message, echo keys included.
    return result_of(result)
