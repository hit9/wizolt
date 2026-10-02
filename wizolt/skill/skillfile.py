"""One SKILL.md file: the Skill value it describes, and the reader that parses it.

The format is the Agent Skills one (https://agentskills.io/specification) plus the Claude Code
fields wizolt honors. A file that cannot be read, or whose frontmatter is not a YAML map, is
refused with the reason; a readable skill that bends the spec loads with warnings, so a skill
written for another agent still works here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import ClassVar

from wizolt.base import ConfigError, Json, WizoltError
from wizolt.shellhooks import HookCommand
from wizolt.skill.permissions import ToolRule

# Claude Code's dynamic context: `!`git status`` in the body runs when the skill loads.
DYNAMIC_COMMAND = re.compile(r"!`([^`\n]+)`")


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
    # `hooks:` in Claude Code's shape: in force from the skill's first load to the session's end.
    hooks: tuple[HookCommand, ...] = ()
    # `allowed-tools`: calls that need no approval while the skill is active.
    allowed_tools: tuple[ToolRule, ...] = ()
    # `context: fork`: the skill runs in a subagent, and its report returns to the caller.
    fork: bool = False

    @property
    def executable(self) -> bool:
        """Whether the skill acts on its own -- runs commands when it loads, hooks tool calls, or
        approves them -- which is what a repository has to be trusted for. Instructions alone are
        no more than the repository's own AGENTS.md."""
        return bool(DYNAMIC_COMMAND.search(self.body) or self.hooks or self.allowed_tools)


class SkillFile:
    """Reads one SKILL.md's frontmatter into a Skill, collecting the warnings it raises."""

    # The closing fence may end the file: `---\nname: x\n---` has no body and is still a skill.
    FRONTMATTER: ClassVar[re.Pattern] = re.compile(r"^---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)(.*)$", re.DOTALL | re.MULTILINE)
    # Agent Skills spec: lowercase letters, digits and single hyphens, neither leading nor trailing.
    SPEC_NAME: ClassVar[re.Pattern] = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
    MAX_NAME_CHARS: ClassVar[int] = 64
    MAX_DESCRIPTION_CHARS: ClassVar[int] = 1024
    # Real frontmatter is a few hundred characters; a hooks table a few thousand.
    MAX_FRONTMATTER_CHARS: ClassVar[int] = 64_000
    # Claude Code picks a subagent type and a model per skill. wizolt has one worker, configured
    # under [worker], and sends the model the session chose (a per-skill switch would re-price the
    # conversation's cache), so these are named rather than silently dropped.
    UNSUPPORTED: ClassVar[dict[str, str]] = {"agent": "the worker runs this skill", "model": "the session's model runs this skill"}

    def __init__(self, meta: Json):
        self.meta = meta
        self.warnings: list[str] = []

    @classmethod
    def parse(cls, path: str, folder: str, source: str) -> Skill:
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
        return cls(meta).skill(body.strip(), folder, os.path.dirname(path), source)

    @classmethod
    def frontmatter(cls, text: str) -> Json:
        """The frontmatter as a map. A cloned repository writes these files, so nothing in one may
        stop wizolt: oversized or pathologically nested YAML is refused like malformed YAML."""
        import yaml  # local import: only sessions with skills pay for the parser (see DEPENDENCY_REVIEW.md)

        if len(text) > cls.MAX_FRONTMATTER_CHARS:
            raise SkillFormatError(f"frontmatter is longer than {cls.MAX_FRONTMATTER_CHARS} characters")
        try:
            meta = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise SkillFormatError(f"invalid YAML frontmatter: {' '.join(str(error).split())}") from error
        except RecursionError as error:  # PyYAML's pure-Python parser recurses per nesting level
            raise SkillFormatError("frontmatter is nested too deeply") from error
        if meta is None:
            return {}
        if not isinstance(meta, dict):
            raise SkillFormatError("frontmatter is not a key/value map")
        return meta

    def skill(self, body: str, folder: str, directory: str, source: str) -> Skill:
        name = self.text("name") or folder.strip()
        if not name:
            raise SkillFormatError("no name")
        if not self.SPEC_NAME.fullmatch(name) or len(name) > self.MAX_NAME_CHARS:
            self.warnings.append(f"name {name!r} is not 1-{self.MAX_NAME_CHARS} lowercase letters, digits and single hyphens")
        if name != folder:
            self.warnings.append(f"name {name!r} does not match its folder {folder!r}")
        # Folded and literal YAML blocks keep their line breaks; the index shows one line.
        description = " ".join(self.text("description").split())
        if not description:
            self.warnings.append("no description")
        elif len(description) > self.MAX_DESCRIPTION_CHARS:
            self.warnings.append(f"description is longer than {self.MAX_DESCRIPTION_CHARS} characters")
        context = self.text("context")
        if context not in ("", "fork"):
            self.warnings.append(f"context {context!r} is not supported; the skill loads inline")
        self.warnings.extend(f"{key} is not supported; {effect}" for key, effect in self.UNSUPPORTED.items() if key in self.meta)
        return Skill(
            name,
            description,
            body,
            directory,
            source,
            argument_hint=" ".join(self.text("argument-hint").split()),
            model_invocable=not self.flag("disable-model-invocation", False),
            user_invocable=self.flag("user-invocable", True),
            hooks=self.hooks(name),
            allowed_tools=self.rules("allowed-tools"),
            fork=context == "fork",
            warnings=tuple(self.warnings),
        )

    def text(self, key: str) -> str:
        """A scalar field as text. YAML types bare values (`name: 2024`); a skill author meant the
        characters, so scalars are stringified and structures are ignored."""
        value = self.meta.get(key)
        if value is None or isinstance(value, (dict, list)):
            return ""
        return str(value).strip()

    def flag(self, key: str, default: bool) -> bool:
        value = self.meta.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        self.warnings.append(f"{key} must be true or false; using {str(default).lower()}")
        return default

    def hooks(self, name: str) -> tuple[HookCommand, ...]:
        try:
            return HookCommand.parse_table(self.meta.get("hooks"), origin=f"skill {name}")
        except ConfigError as error:
            # Refused rather than loaded without them: a guard that silently never runs is worse
            # than a skill that visibly does not load.
            raise SkillFormatError(str(error)) from error

    def rules(self, key: str) -> tuple[ToolRule, ...]:
        """`allowed-tools` as rules. The spec writes one space-separated string, Claude Code also a
        comma-separated one or a YAML list; a rule's parenthesized part may itself hold spaces."""
        value = self.meta.get(key)
        if value is None:
            return ()
        if isinstance(value, str):
            texts = ToolRule.tokens(value)
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            texts = [item.strip() for item in value if item.strip()]
        else:
            self.warnings.append(f"{key} must be a string or a list of strings; ignored")
            return ()
        rules = []
        for text in texts:
            if (rule := ToolRule.parse(text)) is None:
                self.warnings.append(f"{key} rule {text!r} is not `Tool` or `Tool(pattern)`; ignored")
            else:
                rules.append(rule)
        return tuple(rules)
