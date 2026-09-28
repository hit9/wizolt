"""Skill tool: loading an installed skill's full instructions on demand."""

from __future__ import annotations

from typing import TYPE_CHECKING

from wizolt.base import ApprovalView, Json, ToolArgs, ToolError
from wizolt.skill import invocation
from wizolt.skill.invocation import Invocation
from wizolt.tools.base import Tool
from wizolt.tools.delegate import DelegateTool

if TYPE_CHECKING:
    from wizolt.agent.runner import ToolRunner


class SkillTool(Tool):
    NAME = "Skill"
    DESCRIPTION = (
        "Load a skill's full instructions by name (skills are listed in the SKILLS section). "
        "Follow the returned steps, running any bundled scripts it references via Bash."
    )
    runner: ToolRunner | None = None  # injected by ToolRunner.call_tool; a forked skill sends through it

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

    def invocation(self) -> Invocation:
        name, *rest = self.strings(min_count=1, max_count=2)
        library = self.session.skills
        skill = library.get(name) if library else None
        if skill is None:
            available = ", ".join(item.name for item in library.all()) if library else ""
            raise ToolError(f"unknown skill {name!r}" + (f"; available: {available}" if available else "; no skills are installed"))
        if not skill.model_invocable:
            raise ToolError(f"skill {skill.name!r} can only be started by the user, with /{skill.name}")
        return Invocation(skill, rest[0].strip() if rest else "")

    def forks(self) -> bool:
        """A `context: fork` skill runs in the worker when this session has one; without one (or
        inside the worker itself) it loads inline like any other."""
        try:
            return self.invocation().skill.fork and DelegateTool.enabled(self.session)
        except ToolError:
            return False

    def delegation(self) -> DelegateTool:
        order = self.invocation().fork_order()
        return DelegateTool(self.session, [{"action": "send", "order": order, "title": f"skill {self.invocation().skill.name}"}])

    def needs_confirmation(self) -> bool:
        # Loading instructions is reading; loading one that runs `!`commands`` is running them, and
        # forking one is a Delegate send.
        try:
            return self.forks() or bool(self.invocation().commands())
        except ToolError:
            return False  # the call itself reports the error

    def always_confirms(self) -> bool:
        return self.forks() and self.delegation().always_confirms()

    def approval_view(self) -> ApprovalView | None:
        if self.forks():
            return self.delegation().approval_view()
        commands = self.invocation().commands()
        return ApprovalView("commands", "\n".join(commands), "bash") if commands else None

    async def call(self) -> str:
        if self.forks():
            delegation = self.delegation()
            delegation.runner = self.runner
            return await delegation.call()
        return await invocation.load(self.session, self.invocation())
