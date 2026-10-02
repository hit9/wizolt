"""Model-facing operations on the shared agent group."""

from wizolt.base import ApprovalView, Json, ToolError
from wizolt.tools.base import Tool


class SubagentTool(Tool):
    NAME = "Subagent"
    MUTATES = True
    DESCRIPTION = (
        "Run parallel agents with isolated conversations in the SAME working directory and filesystem. "
        "File changes are immediately visible to all agents; no separate worktree is created. "
        "Up to three child agents are retained per session; reuse send for follow-up work. "
        "spawn returns immediately; assign disjoint file boundaries and explicit verification. "
        "send steers a running agent or starts another turn in its existing context. "
        "Use start=false to queue without waking an idle agent. list shows state; wait returns the latest answer. "
        "Do not overwrite or revert other agents' edits. Inspect the actual changes before accepting a report."
    )

    @classmethod
    def params_schema(cls) -> Json:
        return cls.object_schema(
            {
                "action": {"type": "string", "enum": ["spawn", "send", "list", "wait", "stop"]},
                "name": {"type": "string", "description": "Short agent name for spawn"},
                "message": {"type": "string", "description": "Standalone task or additional steering input"},
                "agent_id": {"type": "string"},
                "start": {"type": "boolean", "description": "Wake an idle agent on send (default true)"},
                "timeout": {"type": "integer", "minimum": 0, "maximum": 60},
            },
            ["action"],
        )

    async def call(self) -> str:
        import json

        payload = self.single_dict_arg("Subagent requires named fields")
        group = self.session.subagents
        if group is None:
            raise ToolError("Subagents are unavailable")
        action = payload.get("action")
        uid = payload.get("agent_id", "")
        if action == "spawn":
            entry = await group.spawn(self.session, payload.get("name", ""), payload.get("message", ""))
            uid = entry.agent.session.uid
        elif action == "send":
            message = payload.get("message", "")
            if not isinstance(message, str) or not message.strip():
                raise ToolError("send requires message")
            await group.send(uid, message, start=payload.get("start", True))
        elif action == "wait":
            await group.wait(uid, payload.get("timeout", 30))
        elif action == "stop":
            if uid == self.session.uid:
                raise ToolError("Cannot stop the calling agent")
            group.stop(uid)
        elif action != "list":
            raise ToolError(f"Unknown Subagent action: {action}")
        entries = list(group.entries.values()) if action == "list" else [group.entry(uid)]
        return json.dumps(
            [
                {
                    "agent_id": entry.agent.session.uid,
                    "name": entry.agent.session.agent_name,
                    "parent": entry.parent,
                    "status": entry.status,
                    "context_percent": entry.agent.session.usage.context_percent(entry.agent.session.state.context_percent),
                    "error": entry.error,
                    "answer": next(
                        (message.get("content", "") for message in reversed(entry.agent.session.messages) if message.get("role") == "assistant"), ""
                    )[-12000:],
                }
                for entry in entries
            ],
            ensure_ascii=False,
        )

    def needs_confirmation(self) -> bool:
        return self.single_dict_arg("Subagent requires named fields").get("action") in {"spawn", "send", "stop"}

    def approval_view(self) -> ApprovalView | None:
        payload = self.single_dict_arg("Subagent requires named fields")
        message = payload.get("message", "")
        if payload.get("action") in {"spawn", "send"} and isinstance(message, str) and message.strip():
            return ApprovalView("agent task", message, "markdown", [("workspace", self.session.cwd), ("files", "shared with all agents")])
        return None

    def blocks_agent(self) -> bool:
        return self.single_dict_arg("Subagent requires named fields").get("action") == "wait"
