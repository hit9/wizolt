"""wizolt skills: Markdown instruction packs loaded on demand."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wizolt.base import Json, WizoltError
from wizolt.mentions import scan_mentions

if TYPE_CHECKING:
    from wizolt.session import Session


class SkillFormatError(WizoltError):
    """A SKILL.md that cannot be loaded as a skill; the message says why."""


@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: str
    source: str  # "user" or "project"
    # Spec deviations worth fixing but not worth refusing the skill over (see SkillLibrary.parse).
    warnings: tuple[str, ...] = ()


class SkillLibrary:
    """Skills discovered from user and project skill directories.

    Each skill is a `SKILL.md` file with YAML frontmatter in the Agent Skills format
    (https://agentskills.io/specification); the index (name + description) rides the cache-stable
    prefix so the model knows what exists, and the full body is pulled into the conversation only
    when the model calls Skill(name) or the user references it with `$name` or `@skill:name`."""

    # The closing fence may end the file: `---\nname: x\n---` has no body and is still a skill.
    FRONTMATTER = re.compile(r"^---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)(.*)$", re.DOTALL | re.MULTILINE)
    # Agent Skills spec: lowercase letters, digits and single hyphens, neither leading nor trailing.
    SPEC_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
    MAX_NAME_CHARS = 64
    MAX_DESCRIPTION_CHARS = 1024
    MAX_MENTION_BLOCKS = 50

    def __init__(self, skills: dict[str, Skill], problems: tuple[str, ...] = ()):
        self.skills = skills
        # Files that looked like skills but could not be loaded, as "path: reason" lines for /skills.
        self.problems = problems

    @classmethod
    def load(cls, session: Session) -> SkillLibrary:
        skills: dict[str, Skill] = {}
        problems: list[str] = []
        # Later roots override earlier ones: projects can customize user skills.
        project_skills = next(
            (path for directory in (".wizolt", ".minacode", ".nanocode") if os.path.isdir(path := os.path.join(session.cwd, directory, "skills"))),
            os.path.join(session.cwd, ".wizolt", "skills"),
        )
        for root, source in (
            (session.data_path("skills"), "user"),
            (project_skills, "project"),
        ):
            if not os.path.isdir(root):
                continue
            for entry in sorted(os.listdir(root)):
                path = os.path.join(root, entry, "SKILL.md")
                if not os.path.isfile(path):
                    continue
                try:
                    skill = cls.parse(path, entry, source)
                except SkillFormatError as error:
                    problems.append(f"{path}: {error}")
                    continue
                skills[skill.name] = skill
        return cls(skills, tuple(problems))

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
