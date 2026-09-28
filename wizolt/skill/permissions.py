"""`allowed-tools`: the tool calls an active skill approves in advance.

A rule is Claude Code's `Tool` or `Tool(pattern)`: the bare name approves every call to that tool;
a pattern is matched against the call's command (Bash) or path (file tools), as a glob, or as a
prefix when it ends in `:*` (`Bash(npm run:*)`). A rule only ever skips the approval prompt: it
never makes a call run that would otherwise be refused, and a call no rule covers asks as usual.
"""

from __future__ import annotations

import fnmatch
import re
from typing import TYPE_CHECKING

from wizolt.base import ToolCall

if TYPE_CHECKING:
    from wizolt.session import Session

# A pattern speaks for one simple command. `git status:*` must not approve `git status; rm -rf ~`,
# so a command that chains, pipes, substitutes or redirects is left to the prompt.
SHELL_CONTROL = re.compile(r"[;&|`<>\n]|\$\(")


def pre_approved(session: Session, call: ToolCall) -> bool:
    """Whether an active skill's `allowed-tools` covers this call."""
    if session.skills is None:
        return False
    return any(permits(rule, call) for skill in session.skills.active(session.active_skills) for rule in skill.allowed_tools)


def permits(rule: str, call: ToolCall) -> bool:
    name, _, rest = rule.partition("(")
    if name != call.name:
        return False
    if not rest:
        return True
    pattern = rest.removesuffix(")")
    subject = next((value for key in ("command", "path") if isinstance(value := call.hook_input().get(key), str)), None)
    if subject is None or (call.name == "Bash" and SHELL_CONTROL.search(subject)):
        return False
    if pattern.endswith(":*"):
        prefix = pattern[:-2]
        return subject == prefix or subject.startswith(prefix + " ")
    return fnmatch.fnmatchcase(subject, pattern)
