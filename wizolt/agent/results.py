"""Bounded child result envelopes and parent-owned delivery receipts.

Only the latest settled turn is advertised per child. The child's snapshot is the durable
source; a parent receipt and its context event are checkpointed together. This is explicit
communication, not shared conversation state, and never starts a parent turn.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from uuid import uuid4

from wizolt.base import SESSION_EVENT_KEY, SUBAGENT_RECEIPTS_KEY, Json
from wizolt.session import Session

RESULT_EVENT = "subagent_result"


def result_receipts(output: str) -> dict[str, str]:
    """Read the Subagent tool's JSON contract before presentation framing or hook feedback.

    A `report` answers with the child's text alone, not with rows: it carries no result envelope,
    so it claims nothing and that result is still announced to the parent.
    """
    try:
        rows = json.loads(output)
    except ValueError:
        return {}
    if isinstance(rows, dict):
        rows = [rows.get("result", {})]  # inspect carries its result beside the snapshot.
    if not isinstance(rows, list):
        return {}
    return {
        row["agent_id"]: row["result_id"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("agent_id"), str) and isinstance(row.get("result_id"), str) and row["result_id"]
    }


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

    The runner attaches receipts only to successful, untruncated tool messages. A cancelled
    batch that never enters this turn cannot acknowledge delivery, even if its tools ran.
    """
    seen = session.state.child_results_seen
    results = list(results)
    available = {result["agent_id"]: result["result_id"] for result in results}
    for message in turn:
        if message.get("role") != "tool":
            continue
        for uid, receipt in message.get(SUBAGENT_RECEIPTS_KEY, {}).items():
            if uid in available and receipt == available[uid]:
                seen[uid] = receipt
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
    session.stage_active_turn(turn)
