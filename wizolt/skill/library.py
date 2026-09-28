"""The session's skill library: lookup, and what the model is told about the skills in it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from wizolt.mentions import scan_mentions
from wizolt.skill import discovery
from wizolt.skill.invocation import Invocation, parse_command
from wizolt.skill.skillfile import Skill
from wizolt.skill.trust import ProjectTrust
from wizolt.utils.project import project_root

if TYPE_CHECKING:
    from wizolt.session import Session


class SkillLibrary:
    """Skills discovered from user and project skill directories (see `discovery`).

    The index (name + description) rides the cache-stable prefix so the model knows what exists,
    and the full body is pulled into the conversation only when the model calls Skill(name) or the
    user references it with `$name` or `@skill:name`."""

    MAX_MENTION_BLOCKS = 50
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
        self._cwd = ""
        self._user_skills = ""
        self.trust: ProjectTrust | None = None

    @classmethod
    def load(cls, session: Session) -> SkillLibrary:
        library = cls({})
        library._cwd, library._user_skills = session.cwd, session.data_path("skills")
        library.trust = ProjectTrust(session.data_path(ProjectTrust.FILE_NAME), project_root(session.cwd))
        library.reload()
        return library

    def reload(self) -> None:
        """Scan the disk again and apply the trust decision, in place: a worker shares this object."""
        if self.trust is None:
            return  # built from a fixed set of skills, not from disk
        skills, self.problems = discovery.scan(discovery.roots(self._cwd, self._user_skills))
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

    def command(self, text: str) -> Invocation | None:
        """The skill a `/name args` message starts, or None when no user-invocable skill answers
        to that name. The one place the command loop, the runtime and the engine all ask."""
        parsed = parse_command(text)
        skill = self.get(parsed[0]) if parsed else None
        if parsed is None or skill is None or not skill.user_invocable:
            return None
        return Invocation(skill, parsed[1])

    def commands(self) -> list[Skill]:
        """The skills `/name` can start, for completion."""
        return [skill for skill in self.all() if skill.user_invocable]

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

    def resolve_mentions(self, text: str) -> str:
        seen: set[str] = set()
        blocks: list[str] = []
        for span in scan_mentions(text):
            if span.kind != "skill" or not span.complete or not span.payload:
                continue
            raw = span.payload
            skill = self.get(raw)
            if skill is None or skill.name in seen:
                continue
            seen.add(skill.name)
            row = self.row(skill)
            # The user asked for it, but the author reserved it for `/name`: say so rather than let
            # the model call Skill and be refused.
            blocks.append(row if skill.model_invocable else f"{row} (only the user can start this one, with /{skill.name})")
            if len(blocks) >= self.MAX_MENTION_BLOCKS:
                break
        if not blocks:
            return ""
        header = [
            "--- SKILL MENTIONS ---",
            "The user referenced these skills. Load the one the request needs with Skill(name); the instructions are not inlined.",
            "",
        ]
        return "\n".join(header + blocks).strip()
