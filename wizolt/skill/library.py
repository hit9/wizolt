"""The session's skill library: lookup, and what the model is told about the skills in it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from wizolt.base import ToolCall
from wizolt.skill.discovery import SkillDiscovery
from wizolt.skill.skillfile import Skill
from wizolt.skill.trust import ProjectTrust
from wizolt.utils.workspace import Workspace

if TYPE_CHECKING:
    from wizolt.session import Session


class SkillLibrary:
    """Skills discovered from user and project skill directories (see `SkillDiscovery`).

    The index (name + description) rides the cache-stable prefix so the model knows what exists,
    and the full body is pulled into the conversation only when the model calls Skill(name) or the
    user references it with `$name` or `@skill:name`."""

    # The SKILLS index rides every request. 16K characters is about 4K tokens: room for roughly a
    # hundred skills at the description cut below, and a hard stop for the thousand-skill home dir.
    INDEX_BUDGET_CHARS = 16_000
    INDEX_DESCRIPTION_CHARS = 250

    def __init__(self, skills: dict[str, Skill], problems: tuple[str, ...] = ()):
        self.skills = skills
        # Files that looked like skills but could not be loaded, as "path: reason" lines for /skills.
        self.problems = problems
        # Project skills that run commands, held back until the repository is trusted (see trust).
        # Kept out of `skills`, so nothing that looks a skill up can start one by accident.
        self.untrusted: dict[str, Skill] = {}
        # Both absent for a library built from a fixed set of skills rather than from disk.
        self.discovery: SkillDiscovery | None = None
        self.trust: ProjectTrust | None = None

    @classmethod
    def attach(cls, session: Session) -> SkillLibrary:
        """A library wired to the session's skill sources with its index still empty.

        The symmetric other half of `load`: `load` attaches and scans, for callers that need
        skills immediately (tests, headless runs). Interactive startup attaches here and runs
        the first scan (`reload`) during the "starting" settle, the way MCP connects its servers
        in the background rather than before the first frame.
        """
        library = cls({})
        workspace = Workspace(session.cwd)
        library.discovery = SkillDiscovery(workspace, session.data_path("skills"))
        library.trust = ProjectTrust(session.data_path(ProjectTrust.FILE_NAME), workspace.root)
        return library

    @classmethod
    def load(cls, session: Session) -> SkillLibrary:
        library = cls.attach(session)
        library.reload()
        return library

    def reload(self) -> None:
        """Refresh the shared workspace catalog and trust decision in place.

        Activation and what each model has been told live on its Session, not in this library.
        """
        if self.discovery is None or self.trust is None:
            return
        skills, self.problems = self.discovery.scan()
        trusted = self.trust.granted()
        held = {name for name, skill in skills.items() if skill.source == "project" and skill.executable and not trusted}
        self.untrusted = {name: skills[name] for name in held}
        self.skills = {name: skill for name, skill in skills.items() if name not in held}

    def all(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda skill: skill.name)

    def get(self, name: str) -> Skill | None:
        if name in self.skills:
            return self.skills[name]
        resolved = {key.lower(): key for key in self.skills}.get(name.lower())
        return self.skills.get(resolved) if resolved else None

    def active(self, names: list[str]) -> list[Skill]:
        """The loaded skills among `names` (a session's active_skills). A name whose skill has
        since left the disk, or lost trust, has nothing left to enforce."""
        return [skill for name in names if (skill := self.get(name)) is not None]

    def approves(self, names: list[str], call: ToolCall) -> bool:
        """Whether an active skill's `allowed-tools` covers the call, so it needs no prompt."""
        return any(rule.permits(call) for skill in self.active(names) for rule in skill.allowed_tools)

    def model_visible(self) -> list[Skill]:
        """The skills the model may load, i.e. everything but `disable-model-invocation` ones."""
        return [skill for skill in self.all() if skill.model_invocable]

    def index(self) -> str:
        """The SKILLS block of the request prefix, bounded by INDEX_BUDGET_CHARS.

        It rides every request, so its size is paid again on each one: a long description is cut
        to one screen line's worth, and past the budget project skills are kept before user ones
        -- the nearer the work, the likelier the fit. A skill left out is still loadable by name."""
        visible = self.model_visible()
        if not visible:
            return ""
        rows = {skill.name: self.row(skill) for skill in visible}
        kept: set[str] = set()
        used = 0
        for skill in sorted(visible, key=lambda skill: (skill.source != "project", skill.name)):
            size = len(rows[skill.name]) + 1
            if used + size <= self.INDEX_BUDGET_CHARS:
                kept.add(skill.name)
                used += size
        lines = [rows[skill.name] for skill in visible if skill.name in kept]
        if hidden := len(visible) - len(kept):
            lines.append(f"({hidden} more skills are installed but not listed here; Skill(name) loads one by its exact name.)")
        header = "Use Skill(name) to load a skill's full instructions when its description fits the task; pass arguments when it lists args."
        return "\n".join(["--- SKILLS ---", header, "", *lines])

    def row(self, skill: Skill) -> str:
        description = skill.description or "(no description)"
        if len(description) > self.INDEX_DESCRIPTION_CHARS:
            description = description[: self.INDEX_DESCRIPTION_CHARS - 1].rstrip() + "…"
        hint = f" (args: {skill.argument_hint})" if skill.argument_hint else ""
        return f"- {skill.name} [{skill.source}]{hint}: {description}"
