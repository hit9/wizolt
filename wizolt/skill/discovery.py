"""Where skills live: the roots searched, their precedence, and one scan over them."""

from __future__ import annotations

import os
from dataclasses import dataclass

from wizolt.agentsmd import display_path
from wizolt.skill.skillfile import Skill, SkillFile, SkillFormatError
from wizolt.utils.workspace import Workspace


@dataclass(frozen=True)
class SkillRoot:
    """One directory whose subfolders are skills."""

    path: str
    source: str  # "user" or "project"
    label: str  # how /skills names it: "~/.claude/skills", ".wizolt/skills", "web/.agents/skills"


class SkillDiscovery:
    """Finds a session's skills where other agents keep them too, so one install serves all of
    them: `.claude`, `.agents` and `.wizolt` skill folders at every level from the repository top
    down to the working directory, folders in packages the agent has worked in, and the
    user-level folders. A later root overrides a same-named skill from an earlier one (`roots`)."""

    # One level's skill folders, lowest precedence first. wizolt's own folder wins at its level; the
    # legacy names are read in its place only when it does not exist.
    PROJECT_DIRS = (".claude", ".agents", ".wizolt")
    LEGACY_PROJECT_DIRS = (".minacode", ".nanocode")
    # Below `<data_dir>/skills`, which is wizolt's own and therefore the strongest user root.
    SHARED_USER_ROOTS = ("~/.claude/skills", "~/.agents/skills")

    def __init__(self, workspace: Workspace, user_skills: str):
        self.workspace = workspace
        self.user_skills = user_skills
        # Project directories off the working path the agent has opened files in (see observe).
        self.nested: set[str] = set()

    def observe(self, path: str) -> None:
        """Note a file the agent read or edited: skill folders between it and the repository top
        are scanned from the next `scan` on, the way a monorepo package brings its own skills."""
        self.nested.update(self.workspace.levels_above(path))

    def roots(self) -> list[SkillRoot]:
        """Every skill root, lowest precedence first: user roots, then nested package levels, then
        each level from the repository top down to cwd. Deeper beats shallower on the working
        path, as the directory nearer the work is the more specific one; a package level only adds
        skills, and never replaces one the working path already has."""
        found = [SkillRoot(os.path.expanduser(path), "user", path) for path in self.SHARED_USER_ROOTS]
        found.append(SkillRoot(self.user_skills, "user", display_path(self.user_skills)))
        path_levels = self.workspace.path_levels()
        for level in [*sorted(self.nested - set(path_levels)), *path_levels]:
            for directory in self.PROJECT_DIRS:
                path = self.project_folder(level, directory)
                found.append(SkillRoot(path, "project", self.workspace.relative(path)))
        return found

    def project_folder(self, level: str, directory: str) -> str:
        path = os.path.join(level, directory, "skills")
        if directory == ".wizolt" and not os.path.isdir(path):
            legacy = (os.path.join(level, name, "skills") for name in self.LEGACY_PROJECT_DIRS)
            return next((candidate for candidate in legacy if os.path.isdir(candidate)), path)
        return path

    def scan(self) -> tuple[dict[str, Skill], tuple[str, ...]]:
        """Read every skill under `roots`, later roots overriding earlier ones by name. Returns the
        skills and "location: reason" lines for files that looked like skills but did not load.

        A directory reached twice (the working directory is the home directory, a symlinked folder)
        counts once, at its first and weaker position."""
        skills: dict[str, Skill] = {}
        problems: list[str] = []
        seen: set[str] = set()
        for root in self.roots():
            real = os.path.realpath(root.path)
            if real in seen or not os.path.isdir(real):
                continue
            seen.add(real)
            try:
                entries = sorted(os.listdir(root.path))
            except OSError as error:
                problems.append(f"{root.label}: cannot list: {error}")
                continue
            for entry in entries:
                path = os.path.join(root.path, entry, "SKILL.md")
                if not os.path.isfile(path):
                    continue
                try:
                    skill = SkillFile.parse(path, entry, root.source)
                except SkillFormatError as error:
                    problems.append(f"{root.label}/{entry}/SKILL.md: {error}")
                    continue
                skill.location = f"{root.label}/{entry}"
                if (previous := skills.get(skill.name)) is not None:
                    skill.overrides = (*previous.overrides, previous.location)
                skills[skill.name] = skill
        return skills, tuple(problems)
