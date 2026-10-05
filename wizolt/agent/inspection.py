"""Read-only, bounded agent snapshots, independent of the terminal frontend.

Inspect copies selected semantic fields, never credentials, hidden reasoning, image bytes or
the entire conversation. Live token streams belong to presentation; current tool batches
belong to the runner and are explicitly labelled as batches rather than completed calls.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from wizolt.base import Json, ToolCall
from wizolt.session import Session
from wizolt.session.codec import SessionSnapshotCodec


def inspect_session(session: Session, *, status: str, instruction: str, calls: Sequence[ToolCall] = ()) -> Json:
    def message_row(message: Json) -> Json:
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(part.get("text", "[image]") for part in content if isinstance(part, dict) and isinstance(part.get("text", ""), str))
        row: Json = {"role": message.get("role", ""), "text": str(content or "")[:1000]}
        if message.get("tool_calls"):
            row["tool_calls"] = [
                {"name": call.get("function", {}).get("name", ""), "arguments": str(call.get("function", {}).get("arguments", ""))[:400]}
                for call in message["tool_calls"][:4]
            ]
        return row

    messages = [message for message in [*session.messages, *session._active_turn_messages] if not SessionSnapshotCodec.is_internal_message(message)]
    provider = session.config.provider
    result = dict(session.state.turn_result)
    if result.get("truncated"):
        # This snapshot is bounded on purpose; the answer itself is read whole with `report`.
        result["hint"] = "Use Subagent(action='report') for the full answer"
    return {
        "agent_id": session.uid,
        "name": session.agent_name,
        "parent": session.agent_parent,
        "status": status,
        "last_turn_status": session.state.last_turn_status,
        "elapsed_seconds": round(session.state.elapsed, 1),
        "context_percent": session.usage.context_percent(session.state.context_percent),
        "provider": session.config.active_provider,
        "model": provider.model,
        "api": provider.api,
        "wire": session.policy.resolve(provider).api,
        "effort": provider.reasoning,
        "task": instruction[:2000],
        "plan": [row[:200] for row in session.state.plan_rows_for(session.state.plan, status=True)[:20]],
        "recent_messages": [message_row(message) for message in messages[-8:]],
        "current_tool_batch": [{"name": call.name, "arguments": json.dumps(call.args, ensure_ascii=False)[:400]} for call in calls[:4]],
        "recent_tools": [{"name": record.name, "output": record.output[:1000]} for record in session.tool_records[-4:]],
        "recent_errors": [{"name": record.name, "error": record.error[:500]} for record in session.tool_errors[-4:]],
        "result": result,
        "error": session.state.last_turn_error[:1000],
    }
