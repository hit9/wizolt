"""The shared wire contract and request/history policy helpers.

Concrete adapters live beside their conversions in chat.py, responses.py and anthropic.py.
"""

from __future__ import annotations

import json
from typing import Protocol

from wizolt.base import Billing, Json, ToolCall
from wizolt.config import ProviderConfig


def replayable_arguments(arguments: object) -> str:
    """A stored tool call's arguments as history may send them back: a JSON object, else `{}`.

    A stream cut off mid-call leaves arguments that are not JSON. The call already failed with a
    tool result saying so, and hosts that check history reject every later request carrying the
    broken text, so a session would stay stuck. History keeps the raw text; only the wire is
    cleaned, the same way Anthropic replay parses into an object.
    """

    text = str(arguments or "{}")
    try:
        # strict=False: argument strings often carry literal newlines (a multi-line commit message).
        return text if isinstance(json.loads(text, strict=False), dict) else "{}"
    except json.JSONDecodeError:
        return "{}"


def cut_off_call(call_id: str, name: str) -> ToolCall:
    """A call whose arguments did not parse, failing with a result that says the call was lost.

    Empty args would fail as an argument-count error, which reads as the model's own mistake."""

    return ToolCall(id=call_id, name=name, args=[], error=f"{name} arguments were not complete JSON (the call was cut off); send it again")


def omit_request_fields(params: Json, names: tuple[str, ...]) -> Json:
    """Remove configured endpoint-rejected fields from a completed wire request, in place."""

    extra = params.get("extra_body")
    for name in names:
        params.pop(name, None)
        if isinstance(extra, dict):
            extra.pop(name, None)
    if isinstance(extra, dict) and not extra:
        params.pop("extra_body", None)
    return params


class WireProtocol(Protocol):
    """One provider wire's complete adapter: construction, sending, and parsing.

    `request` is what api_request hands every request to; `messages` builds the wire's history
    payload and is shared by all three adapters (the token estimator uses it). Per-wire
    construction/parsing the other adapters do not share stays off the interface.

    `json_object` reaches the Chat wire only. Responses spells the same thing differently and
    Anthropic spells it differently again (output_format); neither is wired yet, so both accept
    the flag and ignore it rather than passing an unknown keyword to their SDK.
    """

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
    ) -> tuple[Json, list[ToolCall], str]: ...

    def messages(self, messages: list[Json], *, text_only: bool | None = None) -> list[Json]: ...

    def estimation_payload(self, messages: list[Json], tools: list[Json] | None, builtin: list[Json]) -> Json: ...


def keeps_reasoning(policy: str, message: Json, index: int, latest_user: int) -> bool:
    """Whether one assistant turn keeps its provider reasoning on the next request.

    Reasoning attached to a tool call is continuation state. ``current_turn`` keeps only that
    state after the latest real user message; ``tool_calls`` keeps it across turns; ``all`` also
    keeps final-answer reasoning. Each wire decides which of its opaque blocks count as reasoning.
    """

    return policy == "all" or (bool(message.get("tool_calls")) and (policy == "tool_calls" or (policy == "current_turn" and index > latest_user)))
