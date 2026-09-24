"""Memory tools: durable notes and context introspection."""

from __future__ import annotations

import json
from typing import ClassVar, cast

from wizolt.base import Json, ToolError
from wizolt.session import AgentState, PlanItem
from wizolt.tools.base import Tool


class NoteTool(Tool):
    NAME = "Note"
    # Simple work should not pay for a plan-only tool call.
    DESCRIPTION = (
        "Session-only across context compaction; not global AGENTS.md. "
        "Keep for non-trivial work; skip easiest quarter. Never a single-step plan. "
        "View includes read-only activity history."
    )
    STORES_RESULT = False
    MUTATES = True
    # The call line is the note's own prose, which the argument lexer would tint word by word.
    LOG_LEXER = ""

    def needs_confirmation(self) -> bool:
        # MUTATES serializes Note with other state edits; working-note changes do not need user
        # confirmation because they are reversible session metadata, not workspace writes.
        return False

    @classmethod
    def params_schema(cls) -> Json:
        # fmt: off
        plan_item = cls.object_schema({
            "status": {"type": "string", "enum": list(PlanItem.STATUSES)},
            "text": {"type": "string", "description": "Plan step description"},
        }, ["status", "text"])
        return cls.object_schema({
            "action": {"type": "string", "enum": ["view", "update"]},
            "fields": {"type": "array", "items": {"type": "string", "enum": ["goal", "plan", "known", "check", "recent_activity"]}, "minItems": 1, "description": "Fields to view; default all available"},
            "set_goal": {"type": "string", "description": "Replace or clear goal"},
            "replace_plan": {"type": "array", "items": plan_item, "description": "Replace plan"},
            "append_known": {"type": "array", "items": {"type": "string"}, "description": "Append facts"},
            "replace_known": {"type": "array", "items": {"type": "string"}, "description": "Replace all facts"},
            "set_check": {"type": "string", "description": "Replace or clear verification criteria"},
        })
        # fmt: on

    def call(self) -> str:
        data = self.data()
        mutation_fields = {"set_goal", "replace_plan", "append_known", "replace_known", "set_check"}
        if unexpected := sorted(set(data) - {"action", "fields", *mutation_fields}):
            raise ToolError("Note unexpected field: " + ", ".join(unexpected))
        action = data.get("action")
        if action is None:
            action = "update" if mutation_fields.intersection(data) else "view"
        if action not in {"view", "update"}:
            raise ToolError("Note action must be view or update")
        if action == "view":
            if mutation_fields.intersection(data):
                raise ToolError("Note view does not accept update fields")
            return self.view(data)
        if not mutation_fields.intersection(data):
            raise ToolError("Note update requires set_goal, replace_plan, append_known, replace_known, or set_check")

        goal = self.session.state.goal
        plan = list(self.session.state.plan)
        known = list(self.session.state.known)
        check = self.session.state.check
        if "set_goal" in data:
            goal = str(data["set_goal"]).strip()
        if "set_check" in data:
            check = str(data["set_check"]).strip()
        if "replace_plan" in data:
            if not isinstance(data["replace_plan"], list):
                raise ToolError('Note replace_plan must be an array of plan items, e.g. {"replace_plan":[{"status":"doing","text":"inspect"}]}')
            for item in data["replace_plan"]:
                if isinstance(item, dict):
                    if str(item.get("status") or "").strip().lower() not in PlanItem.STATUSES:
                        raise ToolError("Note replace_plan status must be one of: " + ", ".join(PlanItem.STATUSES))
                    if not str(item.get("text") or "").strip():
                        raise ToolError("Note replace_plan text is required")
            plan = cast(list[PlanItem | Json | str], AgentState.plan_items(data["replace_plan"]))
        if "append_known" in data:
            if not isinstance(data["append_known"], list):
                raise ToolError('Note append_known must be an array of strings, e.g. {"append_known":["tests use pytest"]}')
            known = list(dict.fromkeys([*known, *(str(item).strip() for item in data["append_known"] if str(item).strip())]))
        if "replace_known" in data:
            if not isinstance(data["replace_known"], list):
                raise ToolError('Note replace_known must be an array of strings, e.g. {"replace_known":["fact"]}')
            known = [str(item).strip() for item in data["replace_known"] if str(item).strip()]

        before = (self.session.state.goal, list(self.session.state.plan), list(self.session.state.known), self.session.state.check)
        self.session.state.goal = goal
        self.session.state.plan = plan
        self.session.state.known = known
        self.session.state.check = check
        after = (goal, plan, known, check)
        names = ("goal", "plan", "known", "check")
        changed = [name for name, old, new in zip(names, before, after, strict=True) if old != new]
        known_added = len(set(known) - set(before[2]))
        return json.dumps({"ok": True, "changed": changed, "known_added": known_added}, ensure_ascii=False)

    def view(self, data: Json) -> str:
        raw_fields = data.get("fields", ["goal", "plan", "known", "check"])
        if not isinstance(raw_fields, list) or not raw_fields:
            raise ToolError("Note fields must be a non-empty array")
        fields = list(dict.fromkeys(str(field) for field in raw_fields))
        if invalid := [field for field in fields if field not in {"goal", "plan", "known", "check", "recent_activity"}]:
            raise ToolError("Note fields must contain only goal, plan, known, check, recent_activity: " + ", ".join(invalid))
        state = self.session.state
        values: Json = {
            "goal": state.goal,
            "plan": [{"status": item.status, "text": item.text} for item in AgentState.plan_items(state.plan)],
            "known": list(state.known),
            "check": state.check,
        }
        values["recent_activity"] = self.session.recent_activity()
        if "fields" not in data and values["recent_activity"]:
            fields.append("recent_activity")
        return json.dumps({field: values[field] for field in fields}, ensure_ascii=False)

    def data(self) -> Json:
        data = {key: value for key, value in self.single_dict_arg("Note requires named fields").items() if value is not None}
        if data.get("fields") == []:
            data.pop("fields")
        return data

    def short_args(self) -> list[str]:
        data = self.data()
        if data.get("action") == "view" or not any(key in data for key in ("set_goal", "replace_plan", "append_known", "replace_known", "set_check")):
            fields = data.get("fields")
            return ["view " + (", ".join(str(field) for field in fields) if isinstance(fields, list) else "all")]
        lines = []
        if "set_goal" in data:
            goal = str(data["set_goal"] or "").strip()
            lines.append("goal: " + (Tool.compact(goal, 120) if goal else "(cleared)"))
        if "set_check" in data:
            check = str(data["set_check"] or "").strip()
            lines.append("check: " + (Tool.compact(check, 120) if check else "(cleared)"))
        if isinstance(data.get("replace_plan"), list):
            lines.extend(["plan:", *(f"  {row}" for row in AgentState.plan_rows_for(data["replace_plan"], status=True, style="symbol") if row != "- (empty)")])
        if isinstance(data.get("append_known"), list):
            known = [Tool.compact(item, 120) for item in data["append_known"] if str(item).strip() and str(item).strip() not in self.session.state.known]
            if known:
                lines.extend(["known:", *(f"  + {item}" for item in known)])
        if isinstance(data.get("replace_known"), list):
            known = [Tool.compact(item, 120) for item in data["replace_known"] if str(item).strip()]
            if known:
                lines.extend(["known:", *(f"  {item}" for item in known)])
        return ["\n".join(lines) or "{}"]


class ContextTool(Tool):
    """Inspect context usage or request a reset at turn settlement."""

    NAME = "Context"
    DESCRIPTION = (
        "Report tokens left in the context window, or start a new one. A reset keeps Note state, "
        "compacted history files, results, jobs and transcript; it drops model conversation after this turn."
    )
    STORES_RESULT = False
    MUTATES = True

    def needs_confirmation(self) -> bool:
        # Only model context changes; user-visible history and workspace remain.
        return False

    @classmethod
    def params_schema(cls) -> Json:
        return cls.object_schema({"action": {"type": "string", "enum": ["remaining", "reset"]}}, ["action"])

    def call(self) -> str:
        action = self.action()
        if action == "remaining":
            return json.dumps(self.session.context_fill(), ensure_ascii=False)
        if not self.session.request_context_reset():
            return "Reset is already scheduled for the end of this turn."
        # The reset lands at turn settlement, not here: the conversation still holds the assistant
        # message this call is answering, and dropping it mid-batch would leave that message's other
        # calls without results. Say so, because it decides whether more work in this turn is worth
        # doing -- everything after this point is dropped with the conversation.
        return (
            "Reset scheduled: the conversation is dropped when this turn ends. Note state, compacted history "
            "files, stored results, jobs, transcript and workspace remain. Finish the turn now; later "
            "conversation in this turn is also dropped. Note and recent activity seed the new window."
        )

    def action(self) -> str:
        payload = self.single_dict_arg("Context requires an action")
        if unexpected := sorted(set(payload) - {"action"}):
            raise ToolError("Context unexpected field: " + ", ".join(unexpected))
        action = str(payload.get("action") or "").strip()
        if action not in {"remaining", "reset"}:
            raise ToolError("Context action must be remaining or reset")
        return action

    def short_args(self) -> list[str]:
        payload = self.args[0] if len(self.args) == 1 and isinstance(self.args[0], dict) else {}
        return [str(payload.get("action") or "?")]


class NextHintsTool(Tool):
    NAME = "NextHints"
    DESCRIPTION = (
        "Offer 2-3 useful one-line follow-up inputs after completed work. Use sparingly and only when natural next steps exist. "
        "Call once in the same response as the final answer; that response ends the turn."
    )
    STORES_RESULT = False
    SILENT = True
    MAX_HINTS: ClassVar[int] = 4

    @classmethod
    def params_schema(cls) -> Json:
        # fmt: off
        return cls.object_schema({
            "inputs": {"type": "array", "items": {"type": "string"}, "description": 'Short one-line next-step prompts to offer, e.g. ["run the tests", "show the diff"]'},
        }, ["inputs"])
        # fmt: on

    def call(self) -> str:
        data = self.single_dict_arg("NextHints requires named fields")
        if unexpected := sorted(set(data) - {"inputs"}):
            raise ToolError("NextHints unexpected field: " + ", ".join(unexpected))
        raw = data.get("inputs")
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ToolError('NextHints inputs must be an array of strings, e.g. {"inputs":["run the tests"]}')
        # One line each, and the whole line: the input row shortens a chip that does not fit, but
        # `Enter` must put the suggestion the user is choosing into the input, not its display form.
        hints = list(dict.fromkeys(Tool.compact(item, None) for item in raw if item.strip()))[: self.MAX_HINTS]
        if not hints:
            raise ToolError("NextHints inputs must contain at least one non-empty string")
        # Merge rather than replace: a batch of several NextHints calls offers every suggestion,
        # not just the last call's.
        self.session.add_quick_hints(hints, limit=self.MAX_HINTS)
        return f"Offered {len(hints)} quick input(s)"

    def short_args(self) -> list[str]:
        data = self.args[0] if self.args and isinstance(self.args[0], dict) else {}
        inputs = data.get("inputs")
        if isinstance(inputs, list):
            items = [Tool.compact(item, 120) for item in inputs if str(item).strip()]
            if items:
                return ["inputs: " + ", ".join(f'"{item}"' for item in items)]
        return ["{}"]
