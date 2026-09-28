"""wizolt skills: Markdown instruction packs loaded on demand."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wizolt.agentsmd import display_path
from wizolt.base import Json, WizoltError
from wizolt.mentions import scan_mentions

if TYPE_CHECKING:
    from wizolt.session import Session


class SkillFormatError(WizoltError):
    """A SKILL.md that cannot be loaded as a skill; the message says why."""


@dataclass(frozen=True)
class SkillRoot:
    """One directory whose subfolders are skills."""

    path: str
    source: str  # "user" or "project"
    label: str  # how /skills names it: "~/.claude/skills", ".wizolt/skills", "web/.agents/skills"


@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: str
    source: str  # "user" or "project"
    # Spec deviations worth fixing but not worth refusing the skill over (see SkillLibrary.parse).
    warnings: tuple[str, ...] = ()
    location: str = ""  # the folder as /skills shows it, e.g. ".claude/skills/deploy"
    overrides: tuple[str, ...] = ()  # locations of same-named skills this one hides, lowest first


class SkillLibrary:
    """Skills discovered from user and project skill directories.

    Each skill is a `SKILL.md` file with YAML frontmatter in the Agent Skills format
    (https://agentskills.io/specification); the index (name + description) rides the cache-stable
    prefix so the model knows what exists, and the full body is pulled into the conversation only
    when the model calls Skill(name) or the user references it with `$name` or `@skill:name`.

    Skills are read where other agents keep them, so one install serves all of them: `.claude`,
    `.agents` and `.wizolt` skill folders at every level from the repository top down to the working
    directory, and the user-level folders. A later root overrides a same-named skill from an earlier
    one (see `roots`)."""

    # The closing fence may end the file: `---\nname: x\n---` has no body and is still a skill.
    FRONTMATTER = re.compile(r"^---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)(.*)$", re.DOTALL | re.MULTILINE)
    # Agent Skills spec: lowercase letters, digits and single hyphens, neither leading nor trailing.
    SPEC_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
    MAX_NAME_CHARS = 64
    MAX_DESCRIPTION_CHARS = 1024
    MAX_MENTION_BLOCKS = 50
    # One level's skill folders, lowest precedence first. wizolt's own folder wins at its level; the
    # legacy names are read in its place only when it does not exist.
    PROJECT_DIRS = (".claude", ".agents", ".wizolt")
    LEGACY_PROJECT_DIRS = (".minacode", ".nanocode")
    # Below `<data_dir>/skills`, which is wizolt's own and therefore the strongest user root.
    SHARED_USER_ROOTS = ("~/.claude/skills", "~/.agents/skills")

    def __init__(self, skills: dict[str, Skill], problems: tuple[str, ...] = ()):
        self.skills = skills
        # Files that looked like skills but could not be loaded, as "path: reason" lines for /skills.
        self.problems = problems

    @classmethod
    def load(cls, session: Session) -> SkillLibrary:
        return cls(*cls.scan(cls.roots(session.cwd, session.data_path("skills"))))

    @staticmethod
    def project_root(cwd: str) -> str:
        """The top of the git work tree holding cwd, or cwd itself outside one.

        The walk stops at a repository on purpose: outside one there is no project boundary, and
        climbing to `/` would read whatever skill folders a home directory happens to hold as the
        project's own."""
        start = current = os.path.abspath(cwd)
        while True:
            if os.path.exists(os.path.join(current, ".git")):  # a directory, or a worktree's file
                return current
            parent = os.path.dirname(current)
            if parent == current:
                return start
            current = parent

    @classmethod
    def roots(cls, cwd: str, user_skills: str) -> list[SkillRoot]:
        """Every skill root, lowest precedence first: user roots, then each project level from the
        repository top down to cwd. Deeper beats shallower, as the directory nearer the work is
        the more specific one."""
        roots = [SkillRoot(os.path.expanduser(path), "user", path) for path in cls.SHARED_USER_ROOTS]
        roots.append(SkillRoot(user_skills, "user", display_path(user_skills)))
        top, cwd = cls.project_root(cwd), os.path.abspath(cwd)
        levels = [top]
        for part in [] if cwd == top else os.path.relpath(cwd, top).split(os.sep):
            levels.append(os.path.join(levels[-1], part))
        for level in levels:
            prefix = os.path.relpath(level, top)
            for directory in cls.PROJECT_DIRS:
                path = os.path.join(level, directory, "skills")
                if directory == ".wizolt" and not os.path.isdir(path):
                    legacy = (os.path.join(level, name, "skills") for name in cls.LEGACY_PROJECT_DIRS)
                    path = next((candidate for candidate in legacy if os.path.isdir(candidate)), path)
                roots.append(SkillRoot(path, "project", os.path.normpath(os.path.join(prefix, os.path.relpath(path, level)))))
        return roots

    @classmethod
    def scan(cls, roots: list[SkillRoot]) -> tuple[dict[str, Skill], tuple[str, ...]]:
        """Read every skill under `roots`, later roots overriding earlier ones by name.

        A directory reached twice (the working directory is the home directory, a symlinked
        folder) counts once, at its first and weaker position."""
        skills: dict[str, Skill] = {}
        problems: list[str] = []
        seen: set[str] = set()
        for root in roots:
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
                    skill = cls.parse(path, entry, root.source)
                except SkillFormatError as error:
                    problems.append(f"{root.label}/{entry}/SKILL.md: {error}")
                    continue
                skill.location = f"{root.label}/{entry}"
                if (previous := skills.get(skill.name)) is not None:
                    skill.overrides = (*previous.overrides, previous.location)
                skills[skill.name] = skill
        return skills, tuple(problems)

    @classmethod
    def parse(cls, path: str, folder: str, source: str) -> Skill:
        """Read one SKILL.md. A file that cannot be read or whose frontmatter is not a YAML map is
        refused with the reason; a readable skill that bends the spec loads with warnings, so a
        skill written for another agent still works here."""
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as error:
            raise SkillFormatError(f"cannot read: {error}") from error
        # Normalize BOM and CRLF/CR so the frontmatter fences (which key on "\n") match files
        # authored on any platform.
        text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
        match = cls.FRONTMATTER.match(text)
        meta, body = (cls.frontmatter(match.group(1)), match.group(2)) if match else ({}, text)
        warnings: list[str] = []
        name = cls.text_field(meta, "name") or folder.strip()
        if not name:
            raise SkillFormatError("no name")
        if not cls.SPEC_NAME.fullmatch(name) or len(name) > cls.MAX_NAME_CHARS:
            warnings.append(f"name {name!r} is not 1-{cls.MAX_NAME_CHARS} lowercase letters, digits and single hyphens")
        if name != folder:
            warnings.append(f"name {name!r} does not match its folder {folder!r}")
        # Folded and literal YAML blocks keep their line breaks; the index shows one line.
        description = " ".join(cls.text_field(meta, "description").split())
        if not description:
            warnings.append("no description")
        elif len(description) > cls.MAX_DESCRIPTION_CHARS:
            warnings.append(f"description is longer than {cls.MAX_DESCRIPTION_CHARS} characters")
        return Skill(name, description, body.strip(), os.path.dirname(path), source, tuple(warnings))

    @staticmethod
    def frontmatter(text: str) -> Json:
        import yaml  # local import: only sessions with skills pay for the parser (see DEPENDENCY_REVIEW.md)

        try:
            meta = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise SkillFormatError(f"invalid YAML frontmatter: {' '.join(str(error).split())}") from error
        if meta is None:
            return {}
        if not isinstance(meta, dict):
            raise SkillFormatError("frontmatter is not a key/value map")
        return meta

    @staticmethod
    def text_field(meta: Json, key: str) -> str:
        """A scalar field as text. YAML types bare values (`name: 2024`); a skill author meant the
        characters, so scalars are stringified and structures are ignored."""
        value = meta.get(key)
        if value is None or isinstance(value, (dict, list)):
            return ""
        return str(value).strip()

    def all(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda skill: skill.name)

    def get(self, name: str) -> Skill | None:
        if name in self.skills:
            return self.skills[name]
        resolved = {key.lower(): key for key in self.skills}.get(name.lower())
        return self.skills.get(resolved) if resolved else None

    def expand(self, skill: Skill) -> str:
        body = skill.body
        # `${CLAUDE_SKILL_DIR}` is Claude Code's spelling; skills written for it run here unchanged.
        for placeholder in ("{skill_dir}", "${SKILL_DIR}", "${CLAUDE_SKILL_DIR}"):
            body = body.replace(placeholder, skill.dir)
        return body

    def index(self) -> str:
        if not self.skills:
            return ""
        rows = [f"- {skill.name}: {skill.description or '(no description)'}" for skill in self.all()]
        return "\n".join(["--- SKILLS ---", "Use Skill(name) to load a skill's full instructions when its description fits the task.", "", *rows])

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
            blocks.append(f"[{skill.name}] {skill.description or '(no description)'}")
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
