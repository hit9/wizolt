"""wizolt command loop and interactive session runtime.

The implementation lives in `cli.loop`; this package root re-exports the public names
so `from wizolt.ui.cli import CommandLoop` and the command registry keep working, and
keeps the sibling modules importable from the package root.
"""

from wizolt.ui.cli import commands, hints, modals, worker
from wizolt.ui.cli.commands import (
    COMMAND_LOOKUP,
    COMMANDS,
    QUEUE_SAFE_COMMANDS,
    Command,
)
from wizolt.ui.cli.loop import CommandLoop
from wizolt.ui.cli.runtime import TuiRuntime
from wizolt.ui.cli.view import CommandCompleter, View

__all__ = [
    "COMMANDS",
    "COMMAND_LOOKUP",
    "QUEUE_SAFE_COMMANDS",
    "Command",
    "CommandCompleter",
    "CommandLoop",
    "TuiRuntime",
    "View",
    "commands",
    "hints",
    "modals",
    "worker",
]
