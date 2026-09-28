"""One SKILL.md file: the Skill value it describes, and the parser that reads it.

The format is the Agent Skills one (https://agentskills.io/specification) plus the Claude Code
fields wizolt honors. A file that cannot be read, or whose frontmatter is not a YAML map, is
refused with the reason; a readable skill that bends the spec loads with warnings, so a skill
written for another agent still works here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from wizolt.base import Json, WizoltError

# The closing fence may end the file: `---\nname: x\n---` has no body and is still a skill.
FRONTMATTER = re.compile(r"^---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)(.*)$", re.DOTALL | re.MULTILINE)
# Agent Skills spec: lowercase letters, digits and single hyphens, neither leading nor trailing.
SPEC_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Claude Code's dynamic context: `!`git status`` in the body runs when the skill loads.
DYNAMIC_COMMAND = re.compile(r"!`([^`\n]+)`")
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024


class SkillFormatError(WizoltError):
    """A SKILL.md that cannot be loaded as a skill; the message says why."""


@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: str
    source: str  # "user" or "project"
    # Spec deviations worth fixing but not worth refusing the skill over.
    warnings: tuple[str, ...] = ()
    location: str = ""  # the folder as /skills shows it, e.g. ".claude/skills/deploy"
    overrides: tuple[str, ...] = ()  # locations of same-named skills this one hides, lowest first
    argument_hint: str = ""  # `argument-hint`, e.g. "<env>": what `/name` expects after it
    # `disable-model-invocation: true` clears this: the skill stays out of the index and only `/name`
    # starts it (a deploy the user wants to trigger themselves).
    model_invocable: bool = True
    # `user-invocable: false` clears this: no `/name` command, for background knowledge the model
    # loads when relevant but that is no action a user would start.
    user_invocable: bool = True

    @property
    def executable(self) -> bool:
        """Whether loading the skill runs commands, which is what a repository has to be trusted
        for. Instructions alone are no more than the repository's own AGENTS.md."""
        return bool(DYNAMIC_COMMAND.search(self.body))


def parse(path: str, folder: str, source: str) -> Skill:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        raise SkillFormatError(f"cannot read: {error}") from error
    # Normalize BOM and CRLF/CR so the frontmatter fences (which key on "\n") match files
    # authored on any platform.
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    match = FRONTMATTER.match(text)
    meta, body = (frontmatter(match.group(1)), match.group(2)) if match else ({}, text)
    warnings: list[str] = []
    name = text_field(meta, "name") or folder.strip()
    if not name:
        raise SkillFormatError("no name")
    if not SPEC_NAME.fullmatch(name) or len(name) > MAX_NAME_CHARS:
        warnings.append(f"name {name!r} is not 1-{MAX_NAME_CHARS} lowercase letters, digits and single hyphens")
    if name != folder:
        warnings.append(f"name {name!r} does not match its folder {folder!r}")
    # Folded and literal YAML blocks keep their line breaks; the index shows one line.
    description = " ".join(text_field(meta, "description").split())
    if not description:
        warnings.append("no description")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        warnings.append(f"description is longer than {MAX_DESCRIPTION_CHARS} characters")
    return Skill(
        name,
        description,
        body.strip(),
        os.path.dirname(path),
        source,
        argument_hint=" ".join(text_field(meta, "argument-hint").split()),
        model_invocable=not bool_field(meta, "disable-model-invocation", False, warnings),
        user_invocable=bool_field(meta, "user-invocable", True, warnings),
        warnings=tuple(warnings),
    )


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


def text_field(meta: Json, key: str) -> str:
    """A scalar field as text. YAML types bare values (`name: 2024`); a skill author meant the
    characters, so scalars are stringified and structures are ignored."""
    value = meta.get(key)
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def bool_field(meta: Json, key: str, default: bool, warnings: list[str]) -> bool:
    value = meta.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    warnings.append(f"{key} must be true or false; using {str(default).lower()}")
    return default
