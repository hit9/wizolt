"""Asks before destructive shell commands, such as rm -rf or a force push, while yolo is on.

Uses only the public SDK. Without yolo wizolt's own approval already asks before every such command, so the guard stays
quiet rather than asking twice. With yolo on, a command matching a pattern pauses for a
confirmation; declining, or having no one to ask (headless runs), refuses it with a reason the
model can read. No model calls; other commands pass through unchanged.
"""

import re

from wizolt.sdk import Plugin, PluginError
from wizolt.sdk.operations import Refusal

SDK_VERSION = 1

PATTERNS = (
    r"\brm\s+(-\S*\s+)*(-\w*[rRf]\w*|--recursive|--force)",  # Any flag position: rm -v -fr.
    r"\bgit\s+push\b.*\s(--force\b|--force-with-lease\b|-f\b|\+\S)",
    r"\bgit\s+reset\s+.*--hard\b",
    r"\bgit\s+clean\s+-\w*f",
    r"\bgit\s+branch\s+-D\b",
    r"\bdd\s+.*\bof=",
    r"\bmkfs(\.\w+)?\b",
    r">\s*/dev/(sd|nvme|disk)",
)


def setup(plugin: Plugin) -> None:
    """Default enablement belongs to the installation catalog, never to plugin source code."""
    plugin.configure(
        {"type": "object", "properties": {"patterns": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False},
        defaults={"patterns": list(PATTERNS)},
    )
    try:
        patterns = [re.compile(pattern) for pattern in plugin.config["patterns"]]
    except re.error as error:
        raise PluginError(f"guard: invalid pattern: {error}") from None

    async def guard(context, call, next):
        command = call.arguments.get("command")
        if not context.yolo or not isinstance(command, str) or not any(pattern.search(command) for pattern in patterns):
            return await next(call)
        # One printable line: a view title refuses control characters, and that refusal must not
        # read as "no one can confirm".
        shown = "".join(char for char in " ".join(command.split()) if char.isprintable())
        try:
            confirmed = await plugin.ui.confirm("Run this destructive command? " + (shown if len(shown) <= 150 else shown[:149] + "…"))
        except PluginError:
            return Refusal("guard: a destructive command needs confirmation, and no one can confirm in this session")
        if not confirmed:
            return Refusal("guard: the user declined this destructive command; ask before trying another way")
        return await next(call)

    plugin.intercept("tool.call", guard, match={"tool": "Bash"})
