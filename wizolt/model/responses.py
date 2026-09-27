"""Responses adapter: history conversion, request construction, sending and parsing.

Pure conversion helpers remain usable independently. ModelClient owns the shared request
lifecycle, retries and accounting; SDK imports stay deferred until a request.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from wizolt.base import (
    PROVIDER_ORIGIN_KEY,
    RESPONSES_OUTPUT_KEY,
    SEARCH_SOURCES_KEY,
    Billing,
    Json,
    ModelError,
    ModelOutputTruncated,
    ModelStreamIncomplete,
    Text,
    ToolCall,
    builtin_tool_label,
)
from wizolt.config import ProviderConfig
from wizolt.image import ImageInputs
from wizolt.model.protocol import keeps_reasoning, omit_request_fields

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from wizolt.model.client import ModelClient


def responses_input(
    messages: list[Json],
    origin: str,
    *,
    provider_origin: Callable[[ProviderConfig | None], str],
    replayable_echo: Callable[[Json, str], bool],
    images: ImageInputs,
    reasoning_history: str = "all",
    latest_user_position: Callable[[list[Json]], int] | None = None,
    text_only: bool = False,
    image_payloads: dict[str, bytes] | None = None,
) -> list[Json]:
    """Convert normalized messages to Responses input items.

    Replays saved output items when the origin still matches the endpoint, rebuilds function
    calls from normalized tool_calls, and substitutes image content for local image refs.
    `provider_origin` is the caller's endpoint identity (ModelClient.provider_origin) and
    `replayable_echo` its replay gate (ModelClient.replayable_echo). `text_only` is the route's
    image-delivery verdict: raw blocks are suppressed but readable labels and asset paths stay.
    `image_payloads` is the request-local ref→bytes mapping; image parts read from it instead of
    reopening each asset file on the loop.
    """
    origin = origin or provider_origin(None)
    converted: list[Json] = []
    seen_output_ids: set[str] = set()
    latest_user = latest_user_position(messages) if latest_user_position is not None else -1
    for index, message in enumerate(messages):
        role = str(message.get("role") or "")
        content = message.get("content")
        saved_output = message.get(RESPONSES_OUTPUT_KEY) if replayable_echo(message, origin) else None
        if role == "assistant" and isinstance(saved_output, list):
            for item in saved_output:
                if not isinstance(item, dict) or not replayable_output_item(item):
                    continue
                if item.get("type") == "reasoning" and not keeps_reasoning(reasoning_history, message, index, latest_user):
                    continue
                if content is None and item.get("type") == "message":
                    continue
                item_id = str(item.get("id") or "")
                if item_id and item_id in seen_output_ids:
                    continue
                if item_id:
                    seen_output_ids.add(item_id)
                converted.append(item)
            continue
        if role == "tool":
            converted.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        if role not in ("system", "developer", "user", "assistant"):
            continue
        if content is not None:
            converted.append(
                {
                    "role": role,
                    "content": (
                        images.responses_content(message, text_only=text_only, payloads=image_payloads)
                        if role == "user" and images.refs(message)
                        # A pre-built content list (a vision-bridge request) is already in the
                        # Responses wire shape; str() would flatten it into one text blob.
                        else content
                        if isinstance(content, list)
                        else str(content)
                    ),
                }
            )
        if role == "assistant":
            for raw in message.get("tool_calls") or []:
                if not isinstance(raw, dict):
                    continue
                raw_function = raw.get("function")
                function = raw_function if isinstance(raw_function, dict) else {}
                converted.append(
                    {
                        "type": "function_call",
                        "call_id": str(raw.get("id") or uuid.uuid4().hex),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
    return converted


def replayable_output_item(item: Json) -> bool:
    """Whether a saved output item still carries something a later request can use.

    Stateless reasoning travels in the encrypted payload, which the id alone cannot stand in
    for once the response was never stored. A host that returns neither that payload nor any
    readable reasoning leaves an empty shell, so it is dropped instead of replayed."""
    return item.get("type") != "reasoning" or any(item.get(key) for key in ("encrypted_content", "content", "summary"))


def responses_tool_schemas(tools: list[Json]) -> list[Json]:
    """Convert normalized tool schemas to Responses `function` entries."""
    converted: list[Json] = []
    for schema in tools:
        raw_function = schema.get("function")
        function = raw_function if isinstance(raw_function, dict) else {}
        converted.append(
            {
                "type": "function",
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") if isinstance(function.get("parameters"), dict) else {},
                "strict": bool(function.get("strict", False)),
            }
        )
    return converted


def responses_extra_body(extra_body: Json, params: Json) -> Json:
    """Fold configured `reasoning` fields into the managed object instead of replacing it.

    `extra_body` is merged over the request body, so a whole object configured there would drop
    the fields wizolt manages inside it — settling `reasoning.context` would silently take the
    resolved `effort` with it. Merging per field keeps a documented extra reachable while
    `/reason` stays authoritative, mirroring how the Chat path folds `thinking`.
    """
    merged = dict(extra_body)
    configured = merged.pop("reasoning", None)
    managed = params.get("reasoning")
    if isinstance(configured, dict):
        params["reasoning"] = {**configured, **managed} if isinstance(managed, dict) else configured
    elif configured is not None:
        # Nothing to merge field by field: a scalar keeps the plain override an unknown host may
        # want, rather than being second-guessed here.
        merged["reasoning"] = configured
    return merged


async def reassemble_stream(
    client: AsyncOpenAI,
    params: Json,
    *,
    message_field: Callable[[Any, str], Any],
    raise_if_inactive: Callable[[], None],
    emit: Callable[[str, str], None],
    report_builtin_call: Callable[[str, object], None],
) -> Any:
    """Consume a Responses stream, promoting completed text before tool arguments finish.

    Text completion and function-call discovery are independent events and either can arrive
    first. Promotion is therefore a two-condition state transition, not an ordering assumption;
    the terminal response is still consumed normally for history, tool calls, and usage.

    `message_field` reads a field off an SDK object or dict, `raise_if_inactive` aborts a
    cancelled request, `emit` publishes stream deltas, and `report_builtin_call` records
    provider-side tool calls — all ModelClient hooks passed in by the caller.
    """

    terminal: Any = None
    output: list[str] = []
    text_done = handoff_seen = output_promoted = False

    def promote_output() -> None:
        nonlocal output_promoted
        if text_done and handoff_seen and output and not output_promoted:
            emit("output_done", "".join(output))
            output_promoted = True

    try:
        async for event in await client.responses.create(**params):
            raise_if_inactive()
            event_type = str(message_field(event, "type") or "")
            # Two wire spellings of the same event: summarized reasoning and raw reasoning text.
            # Accept both shapes so a standards-compatible extension does not lose its preview.
            if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                emit("reasoning", str(message_field(event, "delta") or ""))
            elif event_type in ("response.output_text.delta", "response.refusal.delta"):
                delta = str(message_field(event, "delta") or "")
                output.append(delta)
                emit("output", delta)
            elif event_type in ("response.output_text.done", "response.refusal.done"):
                text_done = True
                promote_output()
            elif event_type == "response.output_item.added":
                item = message_field(event, "item")
                item_type = str(message_field(item, "type") or "")
                if item_type == "function_call":
                    handoff_seen = True
                    promote_output()
                elif item_type.endswith("_call"):
                    # A provider-side tool is the same durable tool boundary as a local function
                    # call: completed text before it must be handed off now, so the live status
                    # below never covers a finished answer. A provider-side tool also runs inside
                    # the request with no local tool line to show for it, so the status label is
                    # the only sign the turn is still moving.
                    handoff_seen = True
                    promote_output()
                    emit(builtin_tool_label(item_type), "")
            elif event_type == "response.output_item.done":
                item = message_field(event, "item")
                item_type = str(message_field(item, "type") or "")
                # A provider-side call has no local tool line of its own, so report it the moment
                # the stream completes it and the transcript shows it live. The stream and the
                # terminal output carry the same calls, so the parsed-result scan stays silent on
                # streaming requests; reporting here is the one and only record for them.
                if item_type.endswith("_call") and item_type != "function_call":
                    # Some compatible providers omit the matching output_item.added event, so the
                    # durable report below must also establish the promotion boundary itself.
                    handoff_seen = True
                    promote_output()
                    action = message_field(item, "action")
                    query = message_field(action, "query") if action is not None else ""
                    report_builtin_call(item_type, str(query or ""))
            elif event_type == "response.function_call_arguments.delta":
                handoff_seen = True
                promote_output()
            elif event_type in ("response.completed", "response.incomplete"):
                # Compatible providers may omit response.output_text.done; the accepted terminal
                # response proves the streamed text is final, so it is the terminal fallback for
                # text completion. The tool boundary guard keeps plain responses unpromoted.
                text_done = True
                promote_output()
                terminal = message_field(event, "response")
            elif event_type == "response.failed":
                terminal = message_field(event, "response")
    finally:
        emit("", "")
    if terminal is None:
        raise ModelStreamIncomplete("Responses stream ended without a terminal response")
    return terminal


def responses_result(
    result: Any,
    streamed: bool,
    *,
    message_field: Callable[[Any, str], Any],
    dump_message_item: Callable[[Any], Json],
    tool_call: Callable[[str, str, object], ToolCall],
    report_builtin_call: Callable[[str, object], None],
    truncated_output_error: Callable[[Any], ModelOutputTruncated],
    collect_sources: Callable[..., list[Json]],
) -> tuple[Json, list[ToolCall], str]:
    """Parse a Responses result into the normalized (assistant message, tool calls, text) triple.

    Replays the parsed output items under RESPONSES_OUTPUT_KEY, reports provider-side calls when
    the request was not streamed (the stream already reported them live), and raises the
    truncated-output error when the response stopped empty at the output cap.
    """
    if message_field(result, "status") == "failed":
        error = message_field(result, "error") or "unknown error"
        raise ModelError(f"Responses request failed: {error}")
    output = message_field(result, "output") or []
    saved_output = [dump_message_item(item) for item in output]
    text_parts: list[str] = []
    tool_calls: list[Json] = []
    calls: list[ToolCall] = []
    for item in output:
        item_type = message_field(item, "type")
        if item_type == "message":
            for part in message_field(item, "content") or []:
                part_type = message_field(part, "type")
                if part_type == "output_text":
                    text_parts.append(str(message_field(part, "text") or ""))
                elif part_type == "refusal":
                    text_parts.append(str(message_field(part, "refusal") or ""))
        elif item_type == "function_call":
            name = str(message_field(item, "name") or "")
            call_id = str(message_field(item, "call_id") or message_field(item, "id") or uuid.uuid4().hex)
            arguments = str(message_field(item, "arguments") or "{}")
            try:
                payload = json.loads(arguments, strict=False)
            except json.JSONDecodeError:
                payload = {}
            tool_calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}})
            calls.append(tool_call(call_id, name, payload))
    text = "".join(text_parts) or str(message_field(result, "output_text") or "")
    # Streaming already reported every provider-side call live, and the stream and the terminal
    # output carry the same calls, so scanning again would double each one — and a call without an
    # id could not be de-duplicated at all. The scan is the only source for non-streaming requests.
    if not streamed:
        for item in saved_output:
            item_type = str(item.get("type") or "")
            if item_type.endswith("_call") and item_type != "function_call":
                action = item.get("action")
                query = action.get("query") if isinstance(action, dict) else ""
                report_builtin_call(item_type, query if isinstance(query, str) else "")
    if not calls and not text.strip() and message_field(result, "status") == "incomplete":
        details = message_field(result, "incomplete_details")
        if message_field(details, "reason") == "max_output_tokens":
            raise truncated_output_error(message_field(result, "usage"))
    assistant: Json = {"role": "assistant", "content": text or None, RESPONSES_OUTPUT_KEY: saved_output}
    if sources := responses_sources(saved_output, collect_sources):
        assistant[SEARCH_SOURCES_KEY] = sources
    if tool_calls:
        assistant["tool_calls"] = tool_calls
    return assistant, calls, text


def responses_sources(saved_output: list[Json], collect_sources: Callable[..., list[Json]]) -> list[Json]:
    """Sources a Responses host attached to one response.

    Responses implementations attach them either to message annotations or to the server-tool
    item. Reading both wire locations keeps one renderer honest across them."""
    groups: list[Any] = []
    for item in saved_output:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict):
                    groups.append(part.get("annotations"))
            continue
        action = item.get("action")
        groups.append(action.get("sources") if isinstance(action, dict) else None)
        groups.append(item.get("results"))
    return collect_sources(*groups)


def dump_message_item(item: Any) -> Json:
    """Normalize one SDK message item (dict or model) to JSON, or {} when it carries nothing usable."""
    if isinstance(item, dict):
        return Text.value(item)
    if hasattr(item, "model_dump"):
        dumped = item.model_dump(mode="json", exclude_none=True)
        if isinstance(dumped, dict):
            return Text.value(dumped)
    return {}


class ResponsesWire:
    """The OpenAI Responses wire."""

    def __init__(self, client: ModelClient):
        self._client = client

    async def request(
        self,
        messages: list[Json],
        tools: list[Json] | None,
        *,
        provider: ProviderConfig | None = None,
        allow_stream: bool = True,
        response_timeout: float | None = None,
        json_object: bool = False,
        billing: Billing = Billing.MAIN,
    ) -> tuple[Json, list[ToolCall], str]:
        provider = provider if provider is not None else self._client.session.config.provider
        resolved = self._client.resolved(provider)
        text_only = self._client.session.image_route.is_text_only()
        payloads = await self._client.session.images.load_payloads(messages) if not text_only else {}
        stream = allow_stream and provider.stream and self._client.hooks.on_stream is not None
        params: Json = {
            "model": provider.model,
            "input": self.messages(
                Text.value(messages), self._client.provider_origin(provider), provider=provider, text_only=text_only, image_payloads=payloads
            ),
            "stream": stream,
            # Stateless by design, not to save anything: the wire can retain the conversation and
            # let a later request name it by id, but session messages are the source of truth, a
            # sent message is irrevocable, and a resume must rebuild from the snapshot alone.
            # Server-held state moves the truth off the machine that owns it. Prefix caching is
            # unaffected -- it keys on the rendered prefix, not on stored conversations. See
            # DESIGN.md "Cache epochs and breakpoints".
            "store": False,
        }
        if resolved.output_max_tokens > 0:
            params["max_output_tokens"] = resolved.output_max_tokens
        if request_tools := [*responses_tool_schemas(tools or []), *self._client.builtin_tools(resolved)]:
            params["tools"] = request_tools
            params["tool_choice"] = "auto"
            params["parallel_tool_calls"] = True
        if prompt_cache_key := self._client.prompt_cache_key(provider, tools):
            params["prompt_cache_key"] = prompt_cache_key
        # Stateless requests return encrypted reasoning items by default on the wire's own API, so
        # the replay below needs no `include` here; a host that withholds them until asked says so
        # in its catalog recipe. Effort goes through the same request-recipe fold as the chat path,
        # and a host that defines an explicit "off" spelling still gets it when reasoning is off.
        if resolved.responses_reasoning and provider.reasoning == "off" and resolved.reasoning_effort is None:
            raise ModelError("reasoning off is not defined for this Responses model; use a supported effort or configure a documented provider endpoint")
        # Fold user extensions first. A catalog recipe may then add managed extra_body paths
        # without a later assignment replacing them, matching the Chat wire's precedence.
        if provider.extra_body and (extra_body := responses_extra_body(provider.extra_body, params)):
            params["extra_body"] = extra_body
        self._client.apply_request(params, provider, resolved, wire="responses")
        if provider.temperature is not None and not resolved.suppress_temperature:
            params["temperature"] = provider.temperature
        omit_request_fields(params, provider.omit_body)
        client = self._client.client(provider=provider)
        if stream:
            result = await self._client.call_client(client, lambda: self._stream(client, params), response_timeout=response_timeout)
            streamed = True
        else:
            result = await self._client.call_client(client, lambda: client.responses.create(**params), response_timeout=response_timeout)
            streamed = False
        self._client._record_usage(self._client.message_field(result, "usage"), billing=billing)
        assistant, calls, text = self.result(result, streamed)
        assistant[PROVIDER_ORIGIN_KEY] = self._client.provider_origin(provider)
        return assistant, calls, text

    async def _stream(self, client: Any, params: Json) -> Any:
        """Consume a Responses stream, promoting completed text before tool arguments finish."""
        return await reassemble_stream(
            client,
            params,
            message_field=self._client.message_field,
            raise_if_inactive=self._client._raise_if_request_inactive,
            emit=self._client._emit_stream,
            report_builtin_call=self._client.report_builtin_call,
        )

    def messages(
        self,
        messages: list[Json],
        origin: str = "",
        *,
        text_only: bool | None = None,
        image_payloads: dict[str, bytes] | None = None,
        provider: ProviderConfig | None = None,
    ) -> list[Json]:
        provider = provider if provider is not None else self._client.session.config.provider
        resolved = self._client.resolved(provider)
        text_only = self._client.session.image_route.is_text_only() if text_only is None else text_only
        return responses_input(
            messages,
            origin,
            provider_origin=self._client.provider_origin,
            replayable_echo=self._client.replayable_echo,
            images=self._client.session.images,
            reasoning_history=resolved.reasoning_history,
            latest_user_position=self._client.latest_user_position,
            text_only=text_only,
            image_payloads=image_payloads,
        )

    def estimation_payload(self, messages: list[Json], tools: list[Json] | None, builtin: list[Json]) -> Json:
        """The wire-shaped payload for one token estimate, matching what request() would send."""
        payload: Json = {"input": self.messages(Text.value(messages))}
        if request_tools := [*responses_tool_schemas(tools or []), *builtin]:
            payload["tools"] = request_tools
        return payload

    def result(self, result: Any, streamed: bool = False) -> tuple[Json, list[ToolCall], str]:
        return responses_result(
            result,
            streamed,
            message_field=self._client.message_field,
            dump_message_item=dump_message_item,
            tool_call=self._client.tool_call,
            report_builtin_call=self._client.report_builtin_call,
            truncated_output_error=self._client.truncated_output_error,
            collect_sources=self._client.collect_sources,
        )
