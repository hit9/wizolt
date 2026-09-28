"""Skill tool: loading an installed skill's full instructions on demand."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

from wizolt.base import ApprovalView, Json, ToolArgs, ToolError
from wizolt.skill.invocation import Arguments, Invocation
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
    def delegation(self) -> DelegateTool | None:
        """The Delegate send a `context: fork` skill becomes when this session has a worker; None
        to load inline, which is also what the worker itself does (it cannot delegate)."""
        resolution = self._resolution
        if isinstance(resolution, ToolError) or not resolution.skill.fork or not DelegateTool.enabled(self.session):
            return None
        return DelegateTool(self.session, [{"action": "send", "order": resolution.fork_order(), "title": f"skill {resolution.skill.name}"}])

    def needs_confirmation(self) -> bool:
        # Loading instructions is reading; loading one that runs `!`commands`` is running them, and
        # forking one is a Delegate send. A call that resolves to no skill reports that when run.
        resolution = self._resolution
        return self.delegation is not None or (isinstance(resolution, Invocation) and bool(resolution.commands()))

    def always_confirms(self) -> bool:
        return self.delegation is not None  # a send, which yolo never skips (see DelegateTool)

    def approval_view(self) -> ApprovalView | None:
        if self.delegation is not None:
            return self.delegation.approval_view()
        resolution = self._resolution
        commands = resolution.commands() if isinstance(resolution, Invocation) else []
        return ApprovalView("commands", "\n".join(commands), "bash") if commands else None

    async def call(self) -> str:
        if self.delegation is not None:
            self.delegation.runner = self.runner
            return await self.delegation.call()
        return await self.invocation().load(self.session)
