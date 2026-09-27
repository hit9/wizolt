"""The shared wire contract and request/history policy helpers.

Concrete adapters live beside their conversions in chat.py, responses.py and anthropic.py.
"""

from __future__ import annotations

from typing import Protocol

from wizolt.base import Billing, Json, ToolCall
from wizolt.config import ProviderConfig


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
