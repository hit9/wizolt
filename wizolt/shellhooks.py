"""Shell hooks: user commands run at points in a turn, with Claude Code's contract.

Configured the way Claude Code configures them, in the config file or a skill's frontmatter:

    [[hooks.PreToolUse]]
    matcher = "Bash|Edit"          # a regex over the tool name; empty or "*" matches every tool
    hooks = [{ type = "command", command = "~/bin/guard.sh", timeout = 30 }]

A hook reads one JSON object on stdin (`session_id`, `cwd`, `hook_event_name` and the event's
fields) and answers with its exit code: 0 passes (stdout may carry a JSON decision), 2 blocks with
stderr as the reason, anything else is a hook failure the user sees and the turn ignores. So a
hook written for Claude Code runs here unchanged, as long as it matches wizolt's tool names.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wizolt.base import ConfigError, Json, WizoltError
from wizolt.utils.shellrun import run_shell

if TYPE_CHECKING:
    from wizolt.session import Session

PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
STOP = "Stop"
EVENTS = (PRE_TOOL_USE, POST_TOOL_USE, USER_PROMPT_SUBMIT, STOP)
TOOL_EVENTS = (PRE_TOOL_USE, POST_TOOL_USE)
DEFAULT_TIMEOUT = 60.0
BLOCKING_EXIT_CODE = 2


class PromptBlocked(WizoltError):
    """A UserPromptSubmit hook refused the user's input; the message is its reason."""


@dataclass(frozen=True)
class HookCommand:
    event: str
    command: str
    matcher: str = ""  # tool events only: a regex that must match the whole tool name
    timeout: float = DEFAULT_TIMEOUT
    origin: str = "config"  # "config", or "skill <name>" for a skill's own hook

    def matches(self, tool_name: str) -> bool:
        if self.event not in TOOL_EVENTS or self.matcher in ("", "*"):
            return True
        return re.fullmatch(self.matcher, tool_name) is not None


@dataclass(frozen=True)
class HookOutcome:
    """What the hooks of one event decided, combined."""

    blocked: bool = False
    reason: str = ""  # why it was blocked, for whoever the event reports to
    context: tuple[str, ...] = ()  # text the hooks add for the model
    permission: str = ""  # PreToolUse: "allow" skips the approval prompt, "ask" forces it
    failures: tuple[str, ...] = ()  # hooks that broke: shown to the user, never to the model


class ShellHooks:
    """The hooks in force for one session: the config file's, run in the session's project."""

    def __init__(self, configured: tuple[HookCommand, ...], project_dir: str):
        self.configured = configured
        self.project_dir = project_dir

    def detached(self) -> ShellHooks:
        """The configured tool hooks for a worker session, which starts with no skill of its own.

        A worker's turns are the parent's work, not the user's prompts or the user's stopping
        point, so UserPromptSubmit and Stop stay with the parent (Claude Code likewise keeps
        them off its subagents)."""
        return ShellHooks(tuple(hook for hook in self.configured if hook.event in TOOL_EVENTS), self.project_dir)

    def active(self) -> tuple[HookCommand, ...]:
        return self.configured

    def matching(self, event: str, tool_name: str = "") -> list[HookCommand]:
        return [hook for hook in self.active() if hook.event == event and hook.matches(tool_name)]

    def watches_tool(self, tool_name: str) -> bool:
        """Whether a tool call has hooks around it, which keeps it off the parallel read path: that
        path runs no approval step, so it has no place for a hook to decide in."""
        return any(self.matching(event, tool_name) for event in TOOL_EVENTS)

    async def fire(self, event: str, session: Session, fields: Json, *, tool_name: str = "") -> HookOutcome:
        """Run the hooks for `event`; `fields` are the event's own payload fields."""
        hooks = self.matching(event, tool_name)
        if not hooks:
            return HookOutcome()
        payload: Json = {"session_id": session.uid, "cwd": session.cwd, "hook_event_name": event, **({"tool_name": tool_name} if tool_name else {}), **fields}
        return await run_hooks(hooks, payload, cwd=session.cwd, project_dir=self.project_dir)


def parse_hooks(raw: object, origin: str = "config") -> tuple[HookCommand, ...]:
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
            matcher = str(group.get("matcher") or "")
            try:
                re.compile(matcher)
            except re.error as error:
                raise ConfigError(f"{where}.{event}: invalid matcher {matcher!r}: {error}") from error
            for hook in group["hooks"]:
                commands.append(_hook_command(hook, event, matcher, origin, where))
    return tuple(commands)


def _hook_command(hook: object, event: str, matcher: str, origin: str, where: str) -> HookCommand:
    if not isinstance(hook, dict) or hook.get("type", "command") != "command":
        raise ConfigError(f'{where}.{event}: only `type = "command"` hooks are supported')
    command = hook.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ConfigError(f"{where}.{event}: a hook needs a non-empty `command`")
    timeout = hook.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"{where}.{event}: `timeout` must be a positive number of seconds")
    return HookCommand(event, command.strip(), matcher, float(timeout), origin)


async def run_hooks(hooks: Iterable[HookCommand], payload: Json, *, cwd: str, project_dir: str) -> HookOutcome:
    """Run each hook in order and combine their answers: any block blocks, "ask" outranks
    "allow", and every hook's context and failure is kept."""
    blocked_reasons: list[str] = []
    context: list[str] = []
    failures: list[str] = []
    permissions: list[str] = []
    stdin = json.dumps(payload, ensure_ascii=False)
    # Claude Code's variable, so its hook scripts find the project; wizolt's own spelling beside it.
    env = {"CLAUDE_PROJECT_DIR": project_dir, "WIZOLT_PROJECT_DIR": project_dir}
    for hook in hooks:
        result = await run_shell(hook.command, cwd=cwd, timeout=hook.timeout, stdin=stdin, env=env)
        label = f"{hook.event} hook `{hook.command}` ({hook.origin})"
        if result.exit_code == BLOCKING_EXIT_CODE:
            blocked_reasons.append(result.stderr.strip() or f"blocked by {label}")
        elif result.exit_code != 0:
            detail = "timed out" if result.timed_out else f"exited {result.exit_code}"
            failures.append(f"{label} {detail}" + (f": {result.stderr.strip()}" if result.stderr.strip() else ""))
        else:
            decision = _decision(hook.event, result.stdout)
            if decision.blocked:
                blocked_reasons.append(decision.reason or f"blocked by {label}")
            context.extend(decision.context)
            if decision.permission:
                permissions.append(decision.permission)
    permission = "ask" if "ask" in permissions else "allow" if permissions else ""
    return HookOutcome(bool(blocked_reasons), "\n".join(blocked_reasons), tuple(context), permission, tuple(failures))


def _decision(event: str, stdout: str) -> HookOutcome:
    """Read a passing hook's stdout. JSON carries a decision; plain text is context only for
    UserPromptSubmit, as in Claude Code, and otherwise just the hook talking to itself."""
    text = stdout.strip()
    try:
        data = json.loads(text) if text.startswith("{") else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return HookOutcome(context=(text,) if text and event == USER_PROMPT_SUBMIT else ())
    specific = data.get("hookSpecificOutput")
    if not isinstance(specific, dict):
        specific = {}
    extra = specific.get("additionalContext")
    context = (extra,) if isinstance(extra, str) and extra.strip() else ()
    reason = str(data.get("reason") or specific.get("permissionDecisionReason") or "")
    permission = specific.get("permissionDecision") or {"approve": "allow", "block": "deny"}.get(str(data.get("decision")), "")
    if event == PRE_TOOL_USE:
        if permission == "deny":
            return HookOutcome(True, reason, context)
        return HookOutcome(context=context, permission=permission if permission in ("allow", "ask") else "")
    return HookOutcome(data.get("decision") == "block", reason, context)
