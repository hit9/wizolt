"""Starting a skill: the arguments it was given, and the text the model reads when it loads.

One path for both starters. The model's Skill tool and the user's `/name` build the same
`<Skill ...>` block; only who started it differs, and the block says which.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from wizolt.skill.skillfile import DYNAMIC_COMMAND, Skill
from wizolt.utils.process import ShellCommand

if TYPE_CHECKING:
    from wizolt.session import Session


@dataclass(frozen=True)
class Arguments:
    """The text after `/name`, or the Skill tool's `arguments`, and how a body receives it."""

    text: str = ""

    # Claude Code's placeholders: `$ARGUMENTS`, `$ARGUMENTS[N]` and its shorthand `$N` (0-based).
    # The indexed forms are only filled when arguments were given, so an existing skill's
    # `awk '{print $1}'` survives a plain load.
    ALL: ClassVar[re.Pattern] = re.compile(r"\$ARGUMENTS(?!\[)")
    INDEXED: ClassVar[re.Pattern] = re.compile(r"\$ARGUMENTS\[(\d+)\]|\$(\d+)\b")

    def values(self) -> list[str]:
        try:
            return shlex.split(self.text)
        except ValueError:  # an unbalanced quote: fall back to plain words rather than refuse
            return self.text.split()

    def fill(self, body: str) -> str:
        uses_placeholder = bool(self.ALL.search(body) or (self.text and self.INDEXED.search(body)))
        body = self.ALL.sub(lambda _match: self.text, body)
        if self.text:
            values = self.values()

            def indexed(match: re.Match) -> str:
                index = int(match.group(1) or match.group(2))
                return values[index] if index < len(values) else ""

            body = self.INDEXED.sub(indexed, body)
        if self.text and not uses_placeholder:
            # A skill that never says where arguments go still receives them, as Claude Code does.
            body += f"\n\nARGUMENTS: {self.text}"
        return body


@dataclass(frozen=True)
class Invocation:
    """One start of one skill: which skill, with what arguments."""

    skill: Skill
    arguments: Arguments = field(default_factory=Arguments)

    # `/name` then, after any whitespace including a newline, the argument text to the end.
    SLASH_COMMAND: ClassVar[re.Pattern] = re.compile(r"/([^\s/]+)(?:\s+(.*))?\Z", re.DOTALL)
    SKILL_DIR_PLACEHOLDERS: ClassVar[tuple[str, ...]] = ("{skill_dir}", "${SKILL_DIR}", "${CLAUDE_SKILL_DIR}")
    # A `!`command`` result stays in the conversation for its whole life; a `cat` of a large file
    # must not take the context with it. About two thousand tokens a command.
    MAX_COMMAND_OUTPUT: ClassVar[int] = 8_000

    @classmethod
    def split_command(cls, text: str) -> tuple[str, Arguments] | None:
        """`/name args...` as (name, arguments), or None when the text is not a slash command. The
        name is only a candidate: whether a skill answers to it is the library's question."""
        match = cls.SLASH_COMMAND.match(text)
        return (match.group(1), Arguments((match.group(2) or "").strip())) if match else None

    def prepared_body(self) -> str:
        """The body with the folder and arguments filled in, before any command runs. What an
        approval shows: the commands exactly as they will be executed."""
        body = self.skill.body
        # `${CLAUDE_SKILL_DIR}` is Claude Code's spelling; skills written for it run here unchanged.
        for placeholder in self.SKILL_DIR_PLACEHOLDERS:
            body = body.replace(placeholder, self.skill.dir)
        return self.arguments.fill(body)

    def commands(self) -> list[str]:
        return [match.group(1).strip() for match in DYNAMIC_COMMAND.finditer(self.prepared_body())]

    def call_text(self) -> str:
        """The Skill call that starts this invocation, as the model would write it."""
        arguments = f", arguments={json.dumps(self.arguments.text, ensure_ascii=False)}" if self.arguments.text else ""
        return f"Skill(name={json.dumps(self.skill.name)}{arguments})"

    async def load(self, session: Session, *, invoked_by: str = "model") -> str:
        """Load the skill into the session: run its `!`commands``, put its hooks and allowed-tools
        in force for the rest of the session, and return the `<Skill ...>` block the model reads.

        Activation follows the commands, so a load cancelled halfway leaves nothing in force."""
        body = await self.run_commands(session.cwd, session.settings.shell_timeout)
        if self.skill.name not in session.active_skills:
            session.active_skills.append(self.skill.name)
        attributes = {"name": self.skill.name, "source": self.skill.source}
        if self.arguments.text:
            attributes["args"] = self.arguments.text
        if invoked_by == "user":
            attributes["invoked-by"] = "user"
        return f"{self.opening(attributes)}\n{body}\n</Skill>" + self.in_force_note()

    async def run_commands(self, cwd: str, timeout: float) -> str:
        """The prepared body with every `!`command`` replaced by its output, or by why it failed."""
        outputs: dict[str, str] = {}
        for command in dict.fromkeys(self.commands()):
            result = await ShellCommand(command, cwd, timeout).run(max_output=self.MAX_COMMAND_OUTPUT)
            if result.exit_code == 0:
                outputs[command] = result.stdout.rstrip()
            else:
                detail = (result.stderr or result.stdout).strip()
                outputs[command] = f"[`{command}` failed with exit code {result.exit_code}" + (f": {detail}" if detail else "") + "]"
        return DYNAMIC_COMMAND.sub(lambda match: outputs[match.group(1).strip()], self.prepared_body())

    def in_force_note(self) -> str:
        """Tells the model what the skill now enforces, so a blocked or unprompted call reads as the
        skill's policy rather than an error to work around."""
        lines = []
        if self.skill.hooks:
            lines.append("- hooks: " + ", ".join(dict.fromkeys(f"{hook.event}({hook.matcher})" if hook.matcher else hook.event for hook in self.skill.hooks)))
        if self.skill.allowed_tools:
            lines.append("- pre-approved tools: " + " ".join(str(rule) for rule in self.skill.allowed_tools))
        return "\nIn force for the rest of this session:\n" + "\n".join(lines) if lines else ""

    def fork_order(self) -> str:
        """The Delegate order for a `context: fork` skill. The worker loads the skill itself, so its
        `!`commands``, hooks and allowed-tools belong to the worker's session, not the parent's."""
        return (
            f"Run the `{self.skill.name}` skill: call {self.call_text()} and follow its instructions to the end.\n"
            "Report what you did and what you found; that report is all the requester will see."
        )

    def fork_notice(self) -> str:
        """What `/name` gives the model for a forked skill: a request to start it with the Skill
        tool, which is where the worker is sent from, instead of the instructions themselves."""
        opening = self.opening({"name": self.skill.name, "context": "fork", "invoked-by": "user"})
        return f"{opening}\nThe user started this skill. It runs in the worker: call {self.call_text()} now, then relay its report.\n</Skill>"

    @staticmethod
    def opening(attributes: dict[str, str]) -> str:
        return "<Skill " + " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in attributes.items()) + ">"
