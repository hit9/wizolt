"""Adapter rules that depend only on operation values, shared by live adapters and offline trials.

The chain executor owns the generic rules (read-only fields, single-use ``next``, refusal
timing). These are each operation's own: what a candidate or result may change relative to what
the step received. Routing (``model.request`` provider entries, credentials, context budget)
needs the live configuration and stays in its adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from wizolt.plugins.interception import ResultCheck, Transition
from wizolt.sdk import PluginError
from wizolt.sdk.operations import Block, Blocks, ModelRequest, ModelResponse, Prompt, ToolOffer, Value, check_read_only


def attachments_kept(previous: Value, candidate: Value, _owner: str = "") -> None:
    """``prompt.submit``: a handler may omit admitted attachments, never add one."""
    assert isinstance(previous, Prompt)
    if not isinstance(candidate, Prompt):
        return  # A refusal.
    check_read_only(previous, candidate)  # A returned result skips the executor's next() check.
    if not set(candidate.attachments) <= set(previous.attachments):
        raise PluginError("prompt.submit can omit attachments, never add them")


def offered_tools(request: ModelRequest) -> ResultCheck:
    """``model.request``: a response may call only the tools this request offers."""

    def check(_received: Value, result: Value, _downstream: Value | None, _owner: str) -> None:
        if not isinstance(result, ModelResponse):
            return
        seen: set[str] = set()
        for call in result.tool_calls:
            if call.name not in request.tools:
                raise PluginError(f"Response calls a tool this request does not offer: {call.name}")
            if not call.id or call.id in seen:
                raise PluginError("Response tool calls need unique, nonempty IDs")
            seen.add(call.id)

    return check


def composed(order: Callable[[Iterable[str]], list[str]]) -> Transition:
    """``context.compose``: host blocks stay in place, and only editable ones change text.

    Plugins add their own ``plugin:<name>:<id>`` blocks and change or remove only those. Plugin
    blocks are placed after the header, before the conversation (the last host block), by
    ``order`` of their owners.
    """

    def normalized(previous: Value, candidate: Value, owner: str) -> Blocks:
        assert isinstance(previous, Blocks) and isinstance(candidate, Blocks)
        before = {block.id: block for block in previous.blocks}
        given = {block.id: block for block in candidate.blocks}
        core = [block.id for block in previous.blocks if not block.id.startswith("plugin:")]
        if not core:
            raise PluginError("context.compose needs at least one host block ahead of the plugin blocks")
        for name in core:
            block = given.get(name)
            if block is None or block.editable != before[name].editable:
                raise PluginError(f"Context block {name} must stay")
            if not block.editable and block.text != before[name].text:
                raise PluginError(f"Context block {name} is read-only")
        own = f"plugin:{owner}:"
        for block_id, block in before.items():
            if block_id.startswith("plugin:") and not block_id.startswith(own) and given.get(block_id) != block:
                raise PluginError(f"Only {block_id.split(':')[1]} may change or remove {block_id}")
        plugin_blocks = []
        for block in candidate.blocks:
            if block.id in core:
                continue
            if block.id not in before:
                if not block.id.isidentifier():
                    raise PluginError("New context blocks need an identifier ID (Blocks.add)")
                block = Block(own + block.id, block.text, True)
            plugin_blocks.append(block)
        ranks = {name: rank for rank, name in enumerate(order({block.id.split(":")[1] for block in plugin_blocks}))}
        plugin_blocks.sort(key=lambda block: (ranks[block.id.split(":")[1]], block.id))
        return Blocks((*(given[name] for name in core[:-1]), *plugin_blocks, given[core[-1]]), previous.purpose)

    return normalized


def offer_kept(previous: Value, candidate: Value, _owner: str = "") -> ToolOffer:
    """``tools.offer``: remove any name, add only an available one, in the host's order.

    The order is the received names first, then added ones in ``available`` order, so a handler
    that only reorders leaves the request's bytes, and its cache, unchanged."""
    assert isinstance(previous, ToolOffer) and isinstance(candidate, ToolOffer)
    check_read_only(previous, candidate)  # A returned result skips the executor's next() check.
    chosen = set(candidate.tools)
    if unknown := sorted(chosen - set(previous.tools) - set(previous.available)):
        raise PluginError(f"tools.offer can add only available plugin tools, not: {', '.join(unknown)}")
    added = tuple(name for name in previous.available if name in chosen and name not in previous.tools)
    return ToolOffer((*(name for name in previous.tools if name in chosen), *added), previous.available)


def adapter_rules(value: Value, order: Callable[[Iterable[str]], list[str]]) -> tuple[Transition | None, ResultCheck | None]:
    """The value-only rules for the operation ``value`` starts, as the live adapter applies them."""
    if isinstance(value, ToolOffer):
        return offer_kept, lambda received, result, _downstream, _owner: offer_kept(received, result)
    if isinstance(value, Prompt):
        return attachments_kept, lambda received, result, _downstream, _owner: attachments_kept(received, result)
    if isinstance(value, ModelRequest):
        return None, offered_tools(value)
    if isinstance(value, Blocks):
        normalized = composed(order)
        # Inner plugins' blocks arrive in the downstream result; check against that.
        return normalized, lambda received, result, downstream, owner: normalized(downstream or received, result, owner)
    return None, None
