"""Skill tool: loading an installed skill's full instructions on demand."""

from __future__ import annotations

from dataclasses import replace
from functools import cached_property

from wizolt.base import ApprovalView, Json, ToolArgs, ToolError
from wizolt.session import Session
from wizolt.skill.invocation import Arguments, Invocation
from wizolt.tools.base import Tool
from wizolt.tools.subagent import SubagentTool


class SkillTool(Tool):
    NAME = "Skill"
    DESCRIPTION = (
        "Load a skill's full instructions by name (skills are listed in the SKILLS section). "
        "Follow the returned steps, running any bundled scripts it references via Bash."
    )

    @classmethod
    def params_schema(cls) -> Json:
        return cls.object_schema(
            {
                "name": {"type": "string", "description": "Skill name from the SKILLS section"},
                "arguments": {"type": "string", "description": "Arguments for a skill that lists args, as you would type them after /name"},
            },
            ["name"],
        )

    @classmethod
    def payload_args(cls, payload: Json) -> ToolArgs:
        args = payload.get("arguments")
        return [payload.get("name", ""), *([args] if isinstance(args, str) and args.strip() else [])]

    @cached_property
    def _resolution(self) -> Invocation | ToolError:
        """The skill this call starts, resolved once: the approval prompt and the run must describe
        the same skill even if the library is rescanned in between. An error is kept for `call`
        to report, so the approval questions above it can be answered without raising."""
        name, *rest = self.strings(min_count=1, max_count=2)
        library = self.session.skills
        skill = library.get(name) if library else None
        if skill is None:
            available = ", ".join(item.name for item in library.all()) if library else ""
            return ToolError(f"unknown skill {name!r}" + (f"; available: {available}" if available else "; no skills are installed"))
        if not skill.model_invocable:
            return ToolError(f"skill {skill.name!r} can only be started by the user, with /{skill.name}")
        return Invocation(skill, Arguments(rest[0].strip() if rest else ""))

    def invocation(self) -> Invocation:
        if isinstance(self._resolution, ToolError):
            raise self._resolution
        return self._resolution

    @cached_property
    def fork_order(self) -> str:
        """Fork once; skills loaded inside a child stay in that child's context."""
        resolution = self._resolution
        if isinstance(resolution, ToolError) or not resolution.skill.fork or self.session.agent_parent:
            return ""
        return resolution.fork_order()

    def needs_confirmation(self) -> bool:
        resolution = self._resolution
        return bool(self.fork_order) or (isinstance(resolution, Invocation) and bool(resolution.commands()))

    def always_confirms(self) -> bool:
        return bool(self.fork_order)

    @cached_property
    def _fork_tool(self) -> SubagentTool | None:
        if not self.fork_order:
            return None
        return SubagentTool(self.session, [{"action": "spawn", "name": f"skill {self.invocation().skill.name}", "message": self.fork_order}])

    def approval_config(self) -> Session | None:
        return self._fork_tool.approval_config() if self._fork_tool else None

    def approval_view(self) -> ApprovalView | None:
        if self.fork_order:
            view = self._fork_tool.approval_view() if self._fork_tool else None
            return replace(view, label="skill task") if view else None
        resolution = self._resolution
        commands = resolution.commands() if isinstance(resolution, Invocation) else []
        return ApprovalView("commands", "\n".join(commands), "bash") if commands else None

    async def call(self) -> str:
        if self.fork_order:
            group = self.session.subagents
            if group is None:
                raise ToolError("Subagents are unavailable")
            entry = await group.spawn(self.session, f"skill {self.invocation().skill.name}", self.fork_order, model_settings=self.approval_config())
            while entry.task is not None:
                await group.wait(entry.agent.session.uid)
            if entry.error:
                raise ToolError(entry.error)
            return next((str(message.get("content", "")) for message in reversed(entry.agent.session.messages) if message.get("role") == "assistant"), "")
        return await self.invocation().load(self.session)
