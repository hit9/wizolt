"""Bounded child result envelopes and parent-owned delivery receipts.

Only the latest settled turn is advertised per child. The child's snapshot is the durable
source; a parent receipt and its context event are checkpointed together. This is explicit
communication, not shared conversation state, and never starts a parent turn.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from uuid import uuid4

from wizolt.base import SESSION_EVENT_KEY, Json
from wizolt.session import Session

RESULT_EVENT = "subagent_result"


def settled_result(session: Session, answer: str) -> Json:
    status = session.state.last_turn_status
    text = answer if status == "completed" else session.state.last_turn_error or "Turn interrupted"
    return {
        "agent_id": session.uid,
        "result_id": uuid4().hex,
        "name": session.agent_name,
        "status": status,
        "text": text[:1000],
        "truncated": len(text) > 1000,
    }


def receive_results(session: Session, turn: list[Json], results: Iterable[Json]) -> None:
    """Append only unseen results at a request boundary, never during an in-flight request.

    A successful list/wait may already carry the exact result. Read its visible JSON rather
    than acknowledging at tool execution time: a cancelled batch may never enter context.
    Truncated/non-JSON tool output cannot prove receipt and does not suppress notification.
    """
    seen = session.state.child_results_seen
    results = list(results)
    available = {result["agent_id"]: result["result_id"] for result in results}
    for message in turn:
        if message.get("role") != "tool" or not isinstance(content := message.get("content"), str):
            continue
        try:
            rows = json.loads(content.partition("\noutput:\n")[2])
        except ValueError:
            continue
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and isinstance(uid := row.get("agent_id"), str) and row.get("result_id") == available.get(uid) and uid in available:
                    seen[uid] = available[uid]
    for result in results:
        uid, receipt = result["agent_id"], result["result_id"]
        if seen.get(uid) == receipt:
            continue
        turn.append(
            {
                "role": "user",
                "content": "Subagent result (reported by the child; verify its claims):\n" + json.dumps(result, ensure_ascii=False),
                SESSION_EVENT_KEY: RESULT_EVENT,
            }
        )
        seen[uid] = receipt
    # Staged turns are copies. Update the checkpoint before any await can let another
    # family member save main, otherwise a receipt could survive without its event.
    session._active_turn_messages = list(turn)
