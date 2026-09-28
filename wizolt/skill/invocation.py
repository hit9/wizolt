"""Turning a skill into the text the model reads: `/name` parsing, arguments, `!`command`` context.

One path for both starters. The model's Skill tool and the user's `/name` build the same
`<Skill ...>` block; only who started it differs, and the block says which.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass

from wizolt.skill.skillfile import Skill
from wizolt.utils.shellrun import run_shell

# Claude Code's argument placeholders: `$ARGUMENTS`, `$ARGUMENTS[N]` and its shorthand `$N`
# (0-based). The indexed forms are only substituted when arguments were given, so an existing
# skill's `awk '{print $1}'` survives a plain load.
ALL_ARGUMENTS = re.compile(r"\$ARGUMENTS(?!\[)")
INDEXED_ARGUMENT = re.compile(r"\$ARGUMENTS\[(\d+)\]|\$(\d+)\b")
# Claude Code's dynamic context: `!`git status`` is replaced by the command's output at load time.
DYNAMIC_COMMAND = re.compile(r"!`([^`\n]+)`")
# `/name` then, after any whitespace including a newline, the argument text to the end.
SLASH_COMMAND = re.compile(r"/([^\s/]+)(?:\s+(.*))?\Z", re.DOTALL)
SKILL_DIR_PLACEHOLDERS = ("{skill_dir}", "${SKILL_DIR}", "${CLAUDE_SKILL_DIR}")


@dataclass(frozen=True)
class Invocation:
    skill: Skill
    args: str = ""

    def prepared_body(self) -> str:
        """The body with the folder and arguments filled in, before any command runs. What an
        approval shows: the commands exactly as they will be executed."""
        body = self.skill.body
        # `${CLAUDE_SKILL_DIR}` is Claude Code's spelling; skills written for it run here unchanged.
        for placeholder in SKILL_DIR_PLACEHOLDERS:
            body = body.replace(placeholder, self.skill.dir)
        return substitute_arguments(body, self.args)

    def commands(self) -> list[str]:
        return [match.group(1).strip() for match in DYNAMIC_COMMAND.finditer(self.prepared_body())]


def parse_command(text: str) -> tuple[str, str] | None:
    """`/name args...` as (name, args), or None when the text is not a slash command. The name is
    only a candidate: whether a skill answers to it is the library's question."""
    match = SLASH_COMMAND.match(text)
    return (match.group(1), (match.group(2) or "").strip()) if match else None


def split_arguments(args: str) -> list[str]:
    try:
        return shlex.split(args)
    except ValueError:  # an unbalanced quote: fall back to plain words rather than refuse
        return args.split()


def substitute_arguments(body: str, args: str) -> str:
    uses_placeholder = bool(ALL_ARGUMENTS.search(body) or (args and INDEXED_ARGUMENT.search(body)))
    body = ALL_ARGUMENTS.sub(lambda _match: args, body)
    if args:
        values = split_arguments(args)

        def indexed(match: re.Match) -> str:
            index = int(match.group(1) or match.group(2))
            return values[index] if index < len(values) else ""

        body = INDEXED_ARGUMENT.sub(indexed, body)
    if args and not uses_placeholder:
        # A skill that never says where arguments go still receives them, as Claude Code does.
        body += f"\n\nARGUMENTS: {args}"
    return body


async def render(invocation: Invocation, *, cwd: str, timeout: float, invoked_by: str = "model") -> str:
    """The `<Skill ...>` block for one load, with every `!`command`` replaced by its output."""
    body = invocation.prepared_body()
    outputs: dict[str, str] = {}
    for command in dict.fromkeys(invocation.commands()):
        result = await run_shell(command, cwd=cwd, timeout=timeout)
        if result.exit_code == 0:
            outputs[command] = result.stdout.rstrip()
        else:
            detail = (result.stderr or result.stdout).strip()
            outputs[command] = f"[`{command}` failed with exit code {result.exit_code}" + (f": {detail}" if detail else "") + "]"
    body = DYNAMIC_COMMAND.sub(lambda match: outputs[match.group(1).strip()], body)
    attributes = {"name": invocation.skill.name, "source": invocation.skill.source}
    if invocation.args:
        attributes["args"] = invocation.args
    if invoked_by == "user":
        attributes["invoked-by"] = "user"
    opening = "<Skill " + " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in attributes.items()) + ">"
    return f"{opening}\n{body}\n</Skill>"
