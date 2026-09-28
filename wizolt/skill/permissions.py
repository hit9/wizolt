"""`allowed-tools`: the tool calls an active skill approves in advance."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import ClassVar

from wizolt.base import ToolCall


@dataclass(frozen=True)
class ToolRule:
    """One of Claude Code's `Tool` or `Tool(pattern)` rules.

    The bare name approves every call to that tool; a pattern is matched against the call's
    command (Bash) or path (file tools), as a glob, or as a word prefix when it ends in `:*`
    (`Bash(npm run:*)`). A rule only ever skips the approval prompt: it never makes a call run
    that would otherwise be refused, and a call no rule covers asks as usual."""

    tool: str
    pattern: str | None = None

    SYNTAX: ClassVar[re.Pattern] = re.compile(r"([A-Za-z_][\w.-]*)(?:\(([^()]*)\))?")
    # Tokens of a one-line rule list, split on spaces and commas outside the parentheses.
    TOKEN: ClassVar[re.Pattern] = re.compile(r"[^\s,(]+(?:\([^)]*\))?")
    # A pattern speaks for one simple command. `git status:*` must not approve
    # `git status; rm -rf ~`, so a command that chains, pipes, substitutes or redirects is left to
    # the prompt.
    SHELL_CONTROL: ClassVar[re.Pattern] = re.compile(r"[;&|`<>\n]|\$\(")

    @classmethod
    def parse(cls, text: str) -> ToolRule | None:
        match = cls.SYNTAX.fullmatch(text.strip())
        return cls(match.group(1), match.group(2)) if match else None

    @classmethod
    def tokens(cls, text: str) -> list[str]:
        return [match.group(0) for match in cls.TOKEN.finditer(text)]

    def __str__(self) -> str:
        return self.tool if self.pattern is None else f"{self.tool}({self.pattern})"

    def permits(self, call: ToolCall) -> bool:
        if self.tool != call.name:
            return False
        if self.pattern is None:
            return True
        subject = next((value for key in ("command", "path") if isinstance(value := call.hook_input().get(key), str)), None)
        if subject is None or (call.name == "Bash" and self.SHELL_CONTROL.search(subject)):
            return False
        if self.pattern.endswith(":*"):
            prefix = self.pattern[:-2]
            return subject == prefix or subject.startswith(prefix + " ")
        return fnmatch.fnmatchcase(subject, self.pattern)
