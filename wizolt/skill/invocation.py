"""Turning a skill into the text the model reads: `/name` parsing, arguments, `!`command`` context.

One path for both starters. The model's Skill tool and the user's `/name` build the same
`<Skill ...>` block; only who started it differs, and the block says which.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wizolt.skill.skillfile import DYNAMIC_COMMAND, Skill
from wizolt.utils.shellrun import run_shell

if TYPE_CHECKING:
    from wizolt.session import Session

# Claude Code's argument placeholders: `$ARGUMENTS`, `$ARGUMENTS[N]` and its shorthand `$N`
# (0-based). The indexed forms are only substituted when arguments were given, so an existing
# skill's `awk '{print $1}'` survives a plain load.
ALL_ARGUMENTS = re.compile(r"\$ARGUMENTS(?!\[)")
INDEXED_ARGUMENT = re.compile(r"\$ARGUMENTS\[(\d+)\]|\$(\d+)\b")
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

    def call_text(self) -> str:
        """The Skill call that starts this invocation, as the model would write it."""
        arguments = f", arguments={json.dumps(self.args, ensure_ascii=False)}" if self.args else ""
        return f"Skill(name={json.dumps(self.skill.name)}{arguments})"

    def fork_order(self) -> str:
        """The Delegate order for a `context: fork` skill. The worker loads the skill itself, so its
        `!`commands``, hooks and allowed-tools belong to the worker's session, not the parent's."""
        return (
            f"Run the `{self.skill.name}` skill: call {self.call_text()} and follow its instructions to the end.\n"
            "Report what you did and what you found; that report is all the requester will see."
        )


def fork_notice(invocation: Invocation) -> str:
    """What `/name` gives the model for a forked skill: a request to start it with the Skill tool,
    which is where the worker is sent from, instead of the instructions themselves."""
    return (
        f'<Skill name={json.dumps(invocation.skill.name)} context="fork" invoked-by="user">\n'
        f"The user started this skill. It runs in the worker: call {invocation.call_text()} now, then relay its report.\n"
        "</Skill>"
    )


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


async def load(session: Session, invocation: Invocation, *, invoked_by: str = "model") -> str:
    """Load one skill into the session: run its `!`commands``, put its hooks and allowed-tools in
    force for the rest of the session, and return the `<Skill ...>` block the model reads.

    Activation follows the commands, so a load cancelled halfway leaves nothing in force."""
    body = await run_commands(invocation, cwd=session.cwd, timeout=session.settings.shell_timeout)
    skill = invocation.skill
    if skill.name not in session.active_skills:
        session.active_skills.append(skill.name)
    attributes = {"name": skill.name, "source": skill.source}
    if invocation.args:
        attributes["args"] = invocation.args
    if invoked_by == "user":
        attributes["invoked-by"] = "user"
    opening = "<Skill " + " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in attributes.items()) + ">"
    return f"{opening}\n{body}\n</Skill>" + in_force_note(skill)


def in_force_note(skill: Skill) -> str:
    """Tells the model what the skill now enforces, so a blocked or unprompted call reads as the
    skill's policy rather than an error to work around."""
    lines = []
    if skill.hooks:
        lines.append("- hooks: " + ", ".join(dict.fromkeys(f"{hook.event}({hook.matcher})" if hook.matcher else hook.event for hook in skill.hooks)))
    if skill.allowed_tools:
        lines.append("- pre-approved tools: " + " ".join(skill.allowed_tools))
    return "\nIn force for the rest of this session:\n" + "\n".join(lines) if lines else ""


async def run_commands(invocation: Invocation, *, cwd: str, timeout: float) -> str:
    """The prepared body with every `!`command`` replaced by its output, or by why it failed."""
    body = invocation.prepared_body()
    outputs: dict[str, str] = {}
    for command in dict.fromkeys(invocation.commands()):
        result = await run_shell(command, cwd=cwd, timeout=timeout)
        if result.exit_code == 0:
            outputs[command] = result.stdout.rstrip()
        else:
            detail = (result.stderr or result.stdout).strip()
            outputs[command] = f"[`{command}` failed with exit code {result.exit_code}" + (f": {detail}" if detail else "") + "]"
    return DYNAMIC_COMMAND.sub(lambda match: outputs[match.group(1).strip()], body)
