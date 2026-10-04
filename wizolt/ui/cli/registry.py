"""One command catalog for dispatch, completion and admission, including live extensions.

Plugins contribute descriptors, never replace core dispatch. A plugin command holds the same
runtime invocation lease as its tool actions. Rebuilding this small projection reads only active
registrations, so pending reloads cannot leak into completion before they become executable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from wizolt.sdk import PluginError
from wizolt.ui.cli.commands import COMMAND_LOOKUP, COMMAND_NAMES, Command

if TYPE_CHECKING:
    from wizolt.plugins.runtime import PluginRuntime
    from wizolt.ui.cli.loop import CommandLoop


@dataclass(frozen=True)
class PluginCommand:
    runtime: PluginRuntime
    plugin: str
    name: str

    async def __call__(self, _loop: CommandLoop, arguments: str) -> str:
        try:
            return await self.runtime.invoke(self.plugin, "command", self.name, {"input": arguments})
        except Exception as error:  # noqa: BLE001 - user plugin failures belong in command output.
            return f"Plugin command error: {error}"


class CommandCatalog:
    """Project an agent's active commands; a different agent gets a different catalog."""

    def __init__(self, runtime: PluginRuntime | None = None):
        self.runtime = runtime
        if runtime is not None:
            runtime.reserved_commands = frozenset(name.removeprefix("/") for name in COMMAND_NAMES)

    def entries(self) -> dict[str, Command]:
        result = dict(COMMAND_LOOKUP)
        if self.runtime is None:
            return result
        for owner, entry in self.runtime.entries.items():
            if entry.disabling:
                continue  # A stopping plugin admits no new commands.
            for name, action in entry.active.plugin.commands.items():
                spelling = "/" + name
                if spelling in COMMAND_NAMES:
                    # Embeddings may have loaded plugins before assembling their CLI. Keep the
                    # built-in reachable and report the conflict, as normal activation would.
                    entry.active.error = str(PluginError(f"Reserved command: {spelling}"))
                    continue
                result[spelling] = Command(
                    spelling,
                    PluginCommand(self.runtime, owner, name),
                    queue_safe=action.during_turn,
                    description=action.description,
                )
        return result

    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*COMMAND_NAMES, *self.entries())))

    def get(self, name: str) -> Command | None:
        return self.entries().get(name)
