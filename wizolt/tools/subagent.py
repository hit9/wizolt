"""Model-facing operations on the shared agent group."""

import asyncio
from dataclasses import replace
from functools import cached_property

from wizolt.base import ApprovalView, Json, ToolError
from wizolt.session import Session
from wizolt.tools.base import Tool

# How much of an answer a settled result row carries: the head, where a child's summary is. The
# whole text is `report`'s job, and a `list` of many children would otherwise spend context the
# model did not ask to read.
ANSWER_HEAD = 4_000


class SubagentTool(Tool):
    NAME = "Subagent"
    MUTATES = True
    DESCRIPTION = (
        "Run parallel agents with isolated conversations in the SAME working directory and filesystem. "
        "File changes are immediately visible to all agents; no separate worktree is created. "
        "The limit applies to all non-archived child agents in the group, including nested and completed agents; reuse send for follow-up work. "
        "Only main can request archive: it stops a child and its descendants, frees their slots and preserves read-only history. "
        "archive always requires human approval, even under yolo; the approval lists the affected agents. "
        "spawn and send require user approval even under yolo; users can configure each child's model before approving spawn. "
        "spawn returns immediately; assign disjoint file boundaries and explicit verification. "
        "The creating agent must supply a short, unique, task-based name, e.g. api-review, ui-review, or test-check; main is reserved. "
        "send steers a running agent or starts another turn in its existing context. "
        "Archived agents cannot receive input; spawn a new agent for fresh work. "
        "Use start=false to queue without waking an idle agent. list shows each agent's state only. "
        "report returns one child's latest answer whole, as plain text, live or archived; "
        "wait rows carry each settled target's answer head (4000 characters) and a truncated flag. "
        "inspect reads a bounded snapshot of an active or archived agent: task, plan, recent messages, tool activity, settings and result; a clipped result names report for the full text. No approval is needed. "
        "wait defaults to 180 seconds (3 minutes); choose timeout up to 600 seconds (10 minutes) for longer tasks. "
        "Omit agent_ids to wait for any of your currently running direct children; if none are running, return [] immediately. "
        "Or pass a non-empty agent_ids list to select targets, including already settled agents. "
        "wait returns all currently settled targets when any completes, fails or is interrupted, "
        "after any immediately resumed queued work also settles, "
        "or [] on timeout. Other agents keep running. Remove returned IDs before waiting again; already settled targets return immediately. "
        'mode="all" instead returns once every target has settled; on timeout it returns every target, running ones marked running. '
        "A wait timeout does not stop the child; wait again or continue other work. "
        "Your direct children's latest settled results are reported automatically before your next model request; "
        "this does not start a new turn. "
        "Do not overwrite or revert other agents' edits; verify their claims against the files."
    )

    @classmethod
    def session_schema(cls, session: Session, strict: bool = False) -> Json:
        schema = cls.schema(strict)
        limit = session.subagents.limit if session.subagents is not None else session.settings.max_subagents
        schema["function"]["description"] += f" Maximum retained child agents: {limit} (excluding main)."
        if session.agent_parent:
            schema["function"]["parameters"]["properties"]["action"]["enum"].remove("archive")
        return schema

    @cached_property
    def _archive_scope(self) -> frozenset[str]:
        """Freeze identities, not names/status, across approval and execution.

        A descendant spawned while the approval is open is not implicitly approved.
        The coordinator checks this scope again while holding admission control.
        """
        group = self.session.subagents
        if group is None or self.session is not group.root.session or self.session.agent_parent:
            raise ToolError("Only main can request archive")
        uid = self.single_dict_arg("Subagent requires named fields").get("agent_id", "")
        if uid == group.root.session.uid:
            raise ToolError("Cannot archive main")
        return frozenset(group.branch(uid))

    @cached_property
    def _model_settings(self) -> Session:
        """One unpublished approval draft per call, including two spawns in the same batch.

        Reusing the caller's live Session or a shared draft would make Config/refusal alter a
        sibling's first request. The group copies this draft only after approval succeeds.
        Live config already includes the parent's switches; replaying its old override map
        after applying creation defaults would switch the draft back to the parent's provider.
        """
        return Session(
            cwd=self.session.cwd,
            config=self.session.config.for_subagent(policy=self.session.policy),
            settings=replace(self.session.settings),
            catalog=self.session.catalog,
        )

    def approval_config(self) -> Session | None:
        return self._model_settings if self.single_dict_arg("Subagent requires named fields").get("action") == "spawn" else None

    @classmethod
    def params_schema(cls) -> Json:
        from wizolt.agent.subagents import Subagents

        return cls.object_schema(
            {
                "action": {"type": "string", "enum": ["spawn", "send", "list", "inspect", "report", "wait", "stop", "archive"]},
                "name": {"type": "string", "description": "Required for spawn: unique task-based name, e.g. api-review, ui-review, test-check. Never main."},
                "message": {"type": "string", "description": "Standalone task or additional steering input"},
                "agent_id": {"type": "string", "description": "Target for send, inspect, report, stop or archive"},
                "agent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Optional for wait: select targets explicitly; omit to wait for currently running direct children",
                },
                "start": {"type": "boolean", "description": "Wake an idle agent on send (default true)"},
                "mode": {
                    "type": "string",
                    "enum": ["any", "all"],
                    "description": "For wait: any (default) returns when one target settles; all returns when every target has settled",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": Subagents.MAX_WAIT_TIMEOUT,
                    "description": f"Wait timeout in seconds (default {Subagents.DEFAULT_WAIT_TIMEOUT}); 0 polls without waiting. Timeout does not stop the child.",
                },
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
        entries = []
        uid = payload.get("agent_id", "")
        if action == "inspect":
            return json.dumps(await group.inspect(uid), ensure_ascii=False)
        if action == "report":
            return await group.report(uid)
        if action == "archive":
            scope = self._archive_scope
            targets = [{"agent_id": key, "name": entry.agent.session.agent_name, "status": "archived"} for key, entry in group.entries.items() if key in scope]
            await group.archive(uid, expected=scope)
            return json.dumps({"archived": targets, "released_slots": len(targets)}, ensure_ascii=False)
        if action == "spawn":
            entry = await group.spawn(self.session, payload.get("name", ""), payload.get("message", ""), model_settings=self.approval_config())
            uid = entry.agent.session.uid
        elif action == "send":
            message = payload.get("message", "")
            if not isinstance(message, str) or not message.strip():
                raise ToolError("send requires message")
            await group.send(uid, message, start=payload.get("start", True))
        elif action == "wait":
            timeout = payload.get("timeout")
            if timeout is None:
                timeout = group.DEFAULT_WAIT_TIMEOUT
            if isinstance(timeout, bool) or not isinstance(timeout, int):
                raise ToolError("wait timeout must be an integer number of seconds")
            if payload.get("agent_id"):
                raise ToolError("wait uses agent_ids, not agent_id; omit agent_ids to wait for running direct children")
            uids = payload.get("agent_ids")
            if uids is None:
                # Freeze this call's targets; completed history must not repeatedly wake a
                # default wait. Future spawns belong to a later call, not this observation.
                uids = [key for key, entry in group.entries.items() if entry.parent == self.session.uid and entry.task is not None and not entry.task.done()]
                if not uids:
                    return "[]"
            if isinstance(uids, list) and self.session.uid in uids:
                raise ToolError("Cannot wait for the calling agent")
            entries = await group.wait(uids, timeout, mode=payload.get("mode", "any"))
        elif action == "stop":
            if uid == self.session.uid:
                raise ToolError("Cannot stop the calling agent")
            if uid == group.root.session.uid:
                # The user may stop main through the frontend, but another model cannot.
                raise ToolError("Cannot stop the main agent from a subagent")
            task = group.entry(uid).task
            group.stop(uid)
            if task is not None:
                # Report after the child settles. asyncio.wait observes cancellation without
                # re-raising the child's CancelledError or cancelling its snapshot cleanup.
                await asyncio.wait({task})
        elif action != "list":
            raise ToolError(f"Unknown Subagent action: {action}")
        if action != "wait":
            entries = list(group.entries.values()) if action == "list" else [group.entry(uid)]
        rows = []
        for entry in entries:
            row: Json = {
                "agent_id": entry.agent.session.uid,
                "name": entry.agent.session.agent_name,
                "parent": entry.parent,
                "status": entry.status,
                "context_percent": entry.agent.session.usage.context_percent(entry.agent.session.state.context_percent),
                "error": entry.error,
            }
            if action != "list":
                # A row carries the answer -- and proof of delivery -- only when the model asked
                # to read it; a status overview must not acknowledge a result it never showed.
                row["result_id"] = entry.result.get("result_id", "") if entry.status in {"completed", "failed", "interrupted"} else ""
                answer = entry.answer
                row["answer"] = answer[:ANSWER_HEAD]
                row["truncated"] = len(answer) > ANSWER_HEAD
            rows.append(row)
        if action == "list":
            rows.extend(
                {"agent_id": item["uid"], "name": item.get("name", ""), "parent": item.get("parent", ""), "status": "archived"}
                for item in group.root.session.subagent_entries
                if item.get("archived")
            )
        return json.dumps(rows, ensure_ascii=False)

    def needs_confirmation(self) -> bool:
        return self.single_dict_arg("Subagent requires named fields").get("action") in {"spawn", "send", "stop", "archive"}

    def short_args(self) -> list[str]:
        payload = self.single_dict_arg("Subagent requires named fields")
        action = payload.get("action")
        target = payload.get("agent_ids") if action == "wait" else payload.get("name") or payload.get("agent_id")
        if isinstance(target, list):
            target = ", ".join(str(uid) for uid in target)
        if action == "wait":
            action = f"wait {payload.get('mode', 'any')}"  # The transcript says which wait it was.
        return [str(value) for value in (action, target) if value]

    def always_confirms(self) -> bool:
        return self.single_dict_arg("Subagent requires named fields").get("action") in {"spawn", "send", "archive"}

    def approval_view(self) -> ApprovalView | None:
        payload = self.single_dict_arg("Subagent requires named fields")
        if payload.get("action") == "archive":
            scope = self._archive_scope
            group = self.session.subagents
            assert group is not None
            targets = [entry for key, entry in group.entries.items() if key in scope]
            text = "Stop these agents, discard queued inputs and free their slots. Keep conversation history and file changes.\n\n"
            text += "\n".join(f"- {entry.agent.session.agent_name}: {entry.status}" for entry in targets)
            return ApprovalView(
                "archive agents",
                text,
                rows=[
                    ("target", group.entry(payload["agent_id"]).agent.session.agent_name),
                    ("slots released", str(len(scope))),
                    ("history", "kept, read-only in /agents"),
                ],
            )
        message = payload.get("message", "")
        if payload.get("action") in {"spawn", "send"} and isinstance(message, str) and message.strip():
            target = self.approval_config()
            if target is None and self.session.subagents is not None:
                entry = self.session.subagents.entry(payload.get("agent_id", ""))
                if entry.agent is self.session.subagents.root:
                    raise ToolError("Send steering directly through the main agent's input")
                target = entry.agent.session
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
