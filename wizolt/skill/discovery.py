"""Where skills live: the roots searched, their precedence, and one scan over them.

Skills are read where other agents keep them, so one install serves all of them: `.claude`,
`.agents` and `.wizolt` skill folders at every level from the repository top down to the working
directory, and the user-level folders. A later root overrides a same-named skill from an earlier
one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from wizolt.agentsmd import display_path
from wizolt.skill.skillfile import Skill, SkillFormatError, parse
from wizolt.utils.project import project_root

# One level's skill folders, lowest precedence first. wizolt's own folder wins at its level; the
# legacy names are read in its place only when it does not exist.
PROJECT_DIRS = (".claude", ".agents", ".wizolt")
LEGACY_PROJECT_DIRS = (".minacode", ".nanocode")
# Below `<data_dir>/skills`, which is wizolt's own and therefore the strongest user root.
SHARED_USER_ROOTS = ("~/.claude/skills", "~/.agents/skills")


@dataclass(frozen=True)
class SkillRoot:
    """One directory whose subfolders are skills."""

    path: str
    source: str  # "user" or "project"
    label: str  # how /skills names it: "~/.claude/skills", ".wizolt/skills", "web/.agents/skills"


def roots(cwd: str, user_skills: str) -> list[SkillRoot]:
    """Every skill root, lowest precedence first: user roots, then each project level from the
    repository top down to cwd. Deeper beats shallower, as the directory nearer the work is the
    more specific one."""
    found = [SkillRoot(os.path.expanduser(path), "user", path) for path in SHARED_USER_ROOTS]
    found.append(SkillRoot(user_skills, "user", display_path(user_skills)))
    top, cwd = project_root(cwd), os.path.abspath(cwd)
    levels = [top]
    for part in [] if cwd == top else os.path.relpath(cwd, top).split(os.sep):
        levels.append(os.path.join(levels[-1], part))
    for level in levels:
        prefix = os.path.relpath(level, top)
        for directory in PROJECT_DIRS:
            path = os.path.join(level, directory, "skills")
            if directory == ".wizolt" and not os.path.isdir(path):
                legacy = (os.path.join(level, name, "skills") for name in LEGACY_PROJECT_DIRS)
                path = next((candidate for candidate in legacy if os.path.isdir(candidate)), path)
            found.append(SkillRoot(path, "project", os.path.normpath(os.path.join(prefix, os.path.relpath(path, level)))))
    return found


def scan(skill_roots: list[SkillRoot]) -> tuple[dict[str, Skill], tuple[str, ...]]:
    """Read every skill under `skill_roots`, later roots overriding earlier ones by name. Returns
    the skills and "location: reason" lines for files that looked like skills but did not load.

    A directory reached twice (the working directory is the home directory, a symlinked folder)
    counts once, at its first and weaker position."""
    skills: dict[str, Skill] = {}
    problems: list[str] = []
    seen: set[str] = set()
    for root in skill_roots:
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
                skill = parse(path, entry, root.source)
            except SkillFormatError as error:
                problems.append(f"{root.label}/{entry}/SKILL.md: {error}")
                continue
            skill.location = f"{root.label}/{entry}"
            if (previous := skills.get(skill.name)) is not None:
                skill.overrides = (*previous.overrides, previous.location)
            skills[skill.name] = skill
    return skills, tuple(problems)
