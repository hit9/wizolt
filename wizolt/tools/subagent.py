"""Model-facing operations on the shared agent group."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from functools import cached_property

from wizolt.base import ApprovalView, Json, ToolError
from wizolt.session import Session
from wizolt.tools.base import Tool


class SubagentTool(Tool):
    NAME = "Subagent"
    MUTATES = True
    DESCRIPTION = (
        "Run parallel agents with isolated conversations in the SAME working directory and filesystem. "
        "File changes are immediately visible to all agents; no separate worktree is created. "
        "The limit applies to all retained child agents in the group, including nested and completed agents; reuse send for follow-up work. "
        "spawn and send require user approval even under yolo; users can configure each child's model before approving spawn. "
        "spawn returns immediately; assign disjoint file boundaries and explicit verification. "
        "The creating agent must supply a short, unique, task-based name, e.g. api-review, ui-review, or test-check; main is reserved. "
        "send steers a running agent or starts another turn in its existing context. "
        "Use start=false to queue without waking an idle agent. list shows state; wait returns the latest answer. "
        "Do not overwrite or revert other agents' edits. Inspect the actual changes before accepting a report."
    )

    @classmethod
    def session_schema(cls, session: Session, strict: bool = False) -> Json:
        schema = cls.schema(strict)
        limit = session.subagents.limit if session.subagents is not None else session.settings.max_subagents
        schema["function"]["description"] += f" Maximum retained child agents: {limit} (excluding main)."
        return schema

    @cached_property
    def _model_settings(self) -> Session:
        """One unpublished approval draft per call, including two spawns in the same batch.

        Reusing the caller's live Session or a shared draft would make Config/refusal alter a
        sibling's first request. The group copies this draft only after approval succeeds.
        """
        return Session(
            cwd=self.session.cwd,
            config=deepcopy(self.session.config),
            settings=replace(self.session.settings),
            provider_overrides=deepcopy(self.session.provider_overrides),
            catalog=self.session.catalog,
        )

    def approval_config(self) -> Session | None:
        return self._model_settings if self.single_dict_arg("Subagent requires named fields").get("action") == "spawn" else None

    @classmethod
    def params_schema(cls) -> Json:
        return cls.object_schema(
            {
                "action": {"type": "string", "enum": ["spawn", "send", "list", "wait", "stop"]},
                "name": {"type": "string", "description": "Required for spawn: unique task-based name, e.g. api-review, ui-review, test-check. Never main."},
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
            entry = await group.spawn(self.session, payload.get("name", ""), payload.get("message", ""), model_settings=self.approval_config())
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
            task = group.entry(uid).task
            group.stop(uid)
            if task is not None:
                # Report after the child settles. asyncio.wait observes cancellation without
                # re-raising the child's CancelledError or cancelling its snapshot cleanup.
                await asyncio.wait({task})
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
                    "answer": entry.answer[-12000:],
                }
                for entry in entries
            ],
            ensure_ascii=False,
        )

    def needs_confirmation(self) -> bool:
        return self.single_dict_arg("Subagent requires named fields").get("action") in {"spawn", "send", "stop"}

    def short_args(self) -> list[str]:
        payload = self.single_dict_arg("Subagent requires named fields")
        return [str(value) for value in (payload.get("action"), payload.get("name") or payload.get("agent_id")) if value]

    def always_confirms(self) -> bool:
        return self.single_dict_arg("Subagent requires named fields").get("action") in {"spawn", "send"}

    def approval_view(self) -> ApprovalView | None:
        payload = self.single_dict_arg("Subagent requires named fields")
        message = payload.get("message", "")
        if payload.get("action") in {"spawn", "send"} and isinstance(message, str) and message.strip():
            target = self.approval_config()
            if target is None and self.session.subagents is not None:
                entry = self.session.subagents.entries.get(payload.get("agent_id", ""))
                target = entry.agent.session if entry else None
            target = target or self.session
            provider = target.config.provider
            return ApprovalView(
                "agent task",
                message,
                "markdown",
                [
                    ("agent", payload.get("name") or target.agent_name),
                    ("provider", target.config.active_provider),
                    ("model", provider.model),
                    ("effort", provider.reasoning),
                    ("api", provider.api),
                    ("workspace", self.session.cwd),
                    ("files", "shared with all agents"),
                ],
            )
        return None

    def blocks_agent(self) -> bool:
        return self.single_dict_arg("Subagent requires named fields").get("action") == "wait"
