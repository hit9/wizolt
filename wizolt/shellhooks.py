"""Shell hooks: user commands run at points in a turn, with Claude Code's contract.

Configured the way Claude Code configures them, in the config file or a skill's frontmatter:

    [[hooks.PreToolUse]]
    matcher = "Bash|Edit"          # a regex over the tool name; empty or "*" matches every tool
    hooks = [{ type = "command", command = "~/bin/guard.sh", timeout = 30 }]

A hook reads one JSON object on stdin (`session_id`, `cwd`, `hook_event_name` and the event's
fields) and answers with its exit code: 0 passes (stdout may carry a JSON decision), 2 blocks with
stderr as the reason for blocking events; notification events cannot block. Only the documented
command-hook subset of Claude Code's contract is supported.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from wizolt.base import ConfigError, Json, WizoltError
from wizolt.utils.process import ShellCommand, ShellResult

if TYPE_CHECKING:
    from wizolt.session import Session

PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
STOP = "Stop"
SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
PERMISSION_REQUEST = "PermissionRequest"
POST_TOOL_USE_FAILURE = "PostToolUseFailure"
PRE_COMPACT = "PreCompact"
POST_COMPACT = "PostCompact"
SUBAGENT_START = "SubagentStart"
SUBAGENT_STOP = "SubagentStop"
STOP_FAILURE = "StopFailure"
TOOL_EVENTS = (PRE_TOOL_USE, POST_TOOL_USE, POST_TOOL_USE_FAILURE, PERMISSION_REQUEST)
EVENTS = (*TOOL_EVENTS, USER_PROMPT_SUBMIT, STOP, SESSION_START, SESSION_END, PRE_COMPACT, POST_COMPACT, SUBAGENT_START, SUBAGENT_STOP, STOP_FAILURE)
SUBAGENT_EVENTS = (*TOOL_EVENTS, PRE_COMPACT, POST_COMPACT, SUBAGENT_START, SUBAGENT_STOP, STOP_FAILURE)
BLOCKING_EVENTS = (PRE_TOOL_USE, POST_TOOL_USE, USER_PROMPT_SUBMIT, STOP, SUBAGENT_STOP, PRE_COMPACT)
CONTEXT_EVENTS = (PRE_TOOL_USE, POST_TOOL_USE, POST_TOOL_USE_FAILURE, USER_PROMPT_SUBMIT, SESSION_START, SUBAGENT_START, STOP, SUBAGENT_STOP)
MATCH_FIELDS = {
    **dict.fromkeys(TOOL_EVENTS, "tool_name"),
    SESSION_START: "source",
    SESSION_END: "reason",
    PRE_COMPACT: "trigger",
    POST_COMPACT: "trigger",
    SUBAGENT_START: "agent_type",
    SUBAGENT_STOP: "agent_type",
    STOP_FAILURE: "error",
}
DEFAULT_TIMEOUT = 60.0
BLOCKING_EXIT_CODE = 2


class PromptBlocked(WizoltError):
    """A UserPromptSubmit hook refused the user's input; the message is its reason."""


@dataclass(frozen=True)
class HookCommand:
    event: str
    command: str
    matcher: str = ""  # a regex over the event's match field
    timeout: float = DEFAULT_TIMEOUT
    origin: str = "config"  # "config", or "skill <name>" for a skill's own hook

    def matches(self, value: str) -> bool:
        if self.event not in MATCH_FIELDS or self.matcher in ("", "*"):
            return True
        return re.fullmatch(self.matcher, value) is not None

    def label(self) -> str:
        return f"{self.event} hook `{self.command}` ({self.origin})"

    @classmethod
    def parse_table(cls, raw: object, origin: str = "config") -> tuple[HookCommand, ...]:
        """The `hooks` table of a config file or frontmatter, in Claude Code's shape. Raises
        ConfigError naming the entry at fault: a guard that silently never runs is worse than none."""
        if raw is None:
            return ()
        where = "hooks" if origin == "config" else f"{origin} hooks"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a table of events")
        commands: list[HookCommand] = []
        for event, groups in raw.items():
            if event not in EVENTS:
                raise ConfigError(f"{where}: unsupported event {event!r}; supported: {', '.join(EVENTS)}")
            if not isinstance(groups, list):
                raise ConfigError(f"{where}.{event} must be a list of matcher groups")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise ConfigError(f"{where}.{event}: each entry needs a `hooks` list")
                matcher = group.get("matcher", "")
                if not isinstance(matcher, str):
                    raise ConfigError(f"{where}.{event}: `matcher` must be a string")
                try:
                    if matcher != "*":
                        re.compile(matcher)
                except re.error as error:
                    raise ConfigError(f"{where}.{event}: invalid matcher {matcher!r}: {error}") from error
                commands.extend(cls._parse_entry(hook, event, matcher, origin, f"{where}.{event}") for hook in group["hooks"])
        return tuple(commands)

    @classmethod
    def _parse_entry(cls, hook: object, event: str, matcher: str, origin: str, where: str) -> HookCommand:
        if not isinstance(hook, dict) or hook.get("type", "command") != "command":
            raise ConfigError(f'{where}: only `type = "command"` hooks are supported')
        unknown = set(hook) - {"type", "command", "timeout"}
        if unknown:
            raise ConfigError(f"{where}: unsupported command hook fields: {', '.join(sorted(map(str, unknown)))}")
        command = hook.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(f"{where}: a hook needs a non-empty `command`")
        timeout = hook.get("timeout", DEFAULT_TIMEOUT)
        try:
            valid_timeout = not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and timeout > 0 and math.isfinite(timeout)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ConfigError(f"{where}: `timeout` must be a positive number of seconds")
        return cls(event, command.strip(), matcher, float(timeout), origin)


@dataclass(frozen=True)
class HookOutcome:
    """What one hook, or all the hooks of one event combined, decided."""

    blocked: bool = False
    reason: str = ""  # why it was blocked, for whoever the event reports to
    context: tuple[str, ...] = ()  # text the hooks add for the model
    permission: str = ""  # PreToolUse: "allow" skips the approval prompt, "ask" forces it
    failures: tuple[str, ...] = ()  # hooks that broke: shown to the user, never to the model

    @classmethod
    def of(cls, hook: HookCommand, result: ShellResult) -> HookOutcome:
        """One hook's answer, by Claude Code's contract: exit 2 blocks with stderr as the reason,
        another failure is the hook's own problem, and exit 0 may carry a decision on stdout."""
        if result.exit_code == BLOCKING_EXIT_CODE:
            if hook.event in BLOCKING_EVENTS:
                return cls(True, result.stderr.strip() or f"blocked by {hook.label()}")
            if hook.event == PERMISSION_REQUEST:
                return cls()  # only the JSON decision can grant or deny a permission request
        if result.exit_code != 0:
            detail = "timed out" if result.timed_out else f"exited {result.exit_code}"
            return cls(failures=(f"{hook.label()} {detail}" + (f": {result.stderr.strip()}" if result.stderr.strip() else ""),))
        decision = cls.from_stdout(hook.event, result.stdout)
        return replace(decision, reason=decision.reason or f"blocked by {hook.label()}") if decision.blocked else decision

    @classmethod
    def from_stdout(cls, event: str, stdout: str) -> HookOutcome:
        """A passing hook's stdout. JSON carries a decision; plain text is context only for
        UserPromptSubmit, as in Claude Code, and otherwise just the hook talking to itself."""
        text = stdout.strip()
        try:
            data = json.loads(text) if text.startswith("{") else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            return cls(context=(text,) if text and event in (USER_PROMPT_SUBMIT, SESSION_START) else ())
        specific = data.get("hookSpecificOutput")
        if not isinstance(specific, dict):
            specific = {}
        extra = specific.get("additionalContext")
        context = (extra,) if event in CONTEXT_EVENTS and isinstance(extra, str) and extra.strip() else ()
        reason = str(data.get("reason") or specific.get("permissionDecisionReason") or "")
        if event == PERMISSION_REQUEST:
            decision = specific.get("decision")
            if not isinstance(decision, dict):
                return cls()
            behavior = decision.get("behavior")
            if behavior == "deny":
                return cls(True, str(decision.get("message") or ""))
            # Never approve the original call when a hook meant to approve a rewritten one.
            if any(key in decision for key in ("updatedInput", "updatedPermissions", "interrupt")):
                return cls(failures=("PermissionRequest hook: updatedInput, updatedPermissions and interrupt are not supported; asking for permission",))
            return cls(permission="allow" if behavior == "allow" else "")
        permission = specific.get("permissionDecision") or {"approve": "allow", "block": "deny"}.get(str(data.get("decision")), "")
        if event == PRE_TOOL_USE:
            if permission == "deny":
                return cls(True, reason, context)
            if "updatedInput" in specific:
                return cls(context=context, permission="ask", failures=("PreToolUse hook: updatedInput is not supported; asking for permission",))
            return cls(context=context, permission=permission if permission in ("allow", "ask") else "")
        return cls(event in BLOCKING_EVENTS and data.get("decision") == "block", reason, context)

    @classmethod
    def combine(cls, outcomes: list[HookOutcome]) -> HookOutcome:
        """Any block blocks, "ask" outranks "allow", and every context and failure is kept."""
        permissions = [outcome.permission for outcome in outcomes if outcome.permission]
        return cls(
            any(outcome.blocked for outcome in outcomes),
            "\n".join(outcome.reason for outcome in outcomes if outcome.blocked),
            tuple(line for outcome in outcomes for line in outcome.context),
            "ask" if "ask" in permissions else "allow" if permissions else "",
            tuple(failure for outcome in outcomes for failure in outcome.failures),
        )


class ShellHooks:
    """The hooks in force for one session: the config file's, plus those of the skills the session
    has loaded (`Session.active_skills`), run in the session's project.

    Skill hooks are read from the session each time rather than copied in, so loading a skill,
    resuming a session, and a skill losing trust all take effect without anything to keep in sync.
    """

    def __init__(self, configured: tuple[HookCommand, ...], project_dir: str, events: tuple[str, ...] = EVENTS):
        self.configured = configured
        self.project_dir = project_dir
        self.events = events

    def detached(self, session: Session | None = None) -> ShellHooks:
        """A subagent gets configured tool/compaction hooks and its own start/stop events.

        Only subagent events from the parent's skills cross the handoff. The child never reads
        or mutates the parent session; hooks are frozen at child creation."""
        skills = session.skills.active(session.active_skills) if session is not None and session.skills is not None else []
        inherited = tuple(hook for skill in skills for hook in skill.hooks if hook.event in (SUBAGENT_START, SUBAGENT_STOP))
        return ShellHooks((*self.configured, *inherited), self.project_dir, SUBAGENT_EVENTS)

    def active(self, session: Session) -> tuple[HookCommand, ...]:
        skills = session.skills.active(session.active_skills) if session.skills is not None else []
        hooks = (*self.configured, *(hook for skill in skills for hook in skill.hooks))
        return tuple(hook for hook in hooks if hook.event in self.events)

    def matching(self, event: str, session: Session, tool_name: str = "") -> list[HookCommand]:
        return [hook for hook in self.active(session) if hook.event == event and hook.matches(tool_name)]

    def watches_tool(self, session: Session, tool_name: str) -> bool:
        """Whether a tool call has hooks around it, which keeps it off the parallel read path: that
        path runs no approval step, so it has no place for a hook to decide in."""
        return any(hook.event in TOOL_EVENTS and hook.matches(tool_name) for hook in self.active(session))

    async def fire(self, event: str, session: Session, fields: Json, *, tool_name: str = "") -> HookOutcome:
        """Run the hooks for `event` in order and combine their answers; `fields` are the event's
        own payload fields."""
        fields = {**({"tool_name": tool_name} if tool_name else {}), **fields}
        hooks = self.matching(event, session, str(fields.get(MATCH_FIELDS.get(event, ""), "")))
        if not hooks:
            return HookOutcome()
        payload: Json = {
            "session_id": session.uid,
            "cwd": session.cwd,
            "hook_event_name": event,
            "permission_mode": "bypassPermissions" if session.settings.yolo else "default",
            **fields,
        }
        stdin = json.dumps(payload, ensure_ascii=False)
        # Claude Code's variable, so its hook scripts find the project; wizolt's own spelling beside it.
        env = {"CLAUDE_PROJECT_DIR": self.project_dir, "WIZOLT_PROJECT_DIR": self.project_dir}
        outcomes = []
        for hook in hooks:
            result = await ShellCommand(hook.command, session.cwd, hook.timeout, stdin, env).run()
            outcomes.append(HookOutcome.of(hook, result))
        return HookOutcome.combine(outcomes)
