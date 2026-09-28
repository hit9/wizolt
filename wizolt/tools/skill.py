"""Skill tool: loading an installed skill's full instructions on demand."""

from __future__ import annotations

from wizolt.base import ApprovalView, Json, ToolArgs, ToolError
from wizolt.skill import invocation
from wizolt.skill.invocation import Invocation
from wizolt.tools.base import Tool


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

    def needs_confirmation(self) -> bool:
        # Loading instructions is reading; loading one that runs `!`commands`` is running them.
        try:
            return bool(self.invocation().commands())
        except ToolError:
            return False  # the call itself reports the error

    def approval_view(self) -> ApprovalView | None:
        commands = self.invocation().commands()
        return ApprovalView("commands", "\n".join(commands), "bash") if commands else None

    async def call(self) -> str:
        return await invocation.render(self.invocation(), cwd=self.session.cwd, timeout=self.session.settings.shell_timeout)
