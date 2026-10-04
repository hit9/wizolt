"""User-owned command shortcuts, independent of plugin source and settings.

Only modified/function keys are global. Protected editing, cancellation and approval keys
cannot be replaced. Other existing bindings require an explicit replacement request. Saved
targets stay dormant while their plugin is disabled and reactivate on publication.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

from wizolt.base import ConfigError
from wizolt.sdk import PluginError
from wizolt.ui.tui.app import InputMode
from wizolt.ui.tui.keys import normalized_key

if TYPE_CHECKING:
    from wizolt.plugins.session import SessionPlugins
    from wizolt.ui.tui import TuiApp


class PluginKeys:
    PROTECTED = frozenset({"escape", "enter", "c-m", "c-j", "c-c", "c-d", "c-\\", "c-@", "c-h", "backspace", "delete", "tab", "c-i", "s-tab", "c-s"})

    def __init__(self, runtime: SessionPlugins, tui: TuiApp, invoke: Callable[[str], None]):
        self.runtime, self.tui, self.invoke = runtime, tui, invoke
        self.preferences = runtime.catalog.preferences
        self.saved: dict[str, str] = {}
        self.error = ""
        self.load()

    def target(self, command: str):
        owner, separator, name = command.partition(".")
        entry = self.runtime.entries.get(owner)
        return entry.active.plugin.commands.get(name) if separator and entry and not entry.disabling and not entry.active.error else None

    def enabled(self, command: str) -> bool:
        action = self.target(command)
        return bool(
            action and self.tui.modal is None and (self.tui.input_mode == InputMode.CHAT or (self.tui.input_mode == InputMode.RUNNING and action.during_turn))
        )

    def load(self) -> None:
        try:
            saved = self.preferences.read("shortcuts").get("bindings", {})
            if not isinstance(saved, dict) or len(saved) > 64:
                raise PluginError("Invalid shortcut preferences")
            for key, command in saved.items():
                self.check(key)
                if not isinstance(command, str) or len(command) > 200 or "." not in command:
                    raise PluginError("Invalid shortcut command")
            saved = {self.check(key): command for key, command in saved.items()}
            self.saved, self.error = saved, ""
        except (OSError, ValueError, ConfigError) as error:
            self.saved, self.error = {}, str(error)
        self.publish()

    @classmethod
    def check(cls, key: str) -> str:
        key = normalized_key(key)
        if key in cls.PROTECTED or not (key.startswith("c-") or (key.startswith("f") and key[1:].isdigit())):
            raise PluginError(f"Protected or unmodified global key: {key}; choose a function key or an available Ctrl key")
        return key

    def publish(self) -> None:
        bindings = KeyBindings()
        for key, command in self.saved.items():
            bindings.add(key, filter=Condition(lambda command=command: self.enabled(command)), eager=True)(
                lambda event, command=command: self.invoke("/" + command.partition(".")[2])
            )
        self.tui.command_bindings = bindings
        self.tui.invalidate()

    def listing(self) -> dict:
        return {
            "bindings": [{"key": key, "command": command, "active": self.target(command) is not None} for key, command in sorted(self.saved.items())],
            "protected": sorted(self.PROTECTED),
            "error": self.error,
        }

    async def call(self, service: str, arguments: dict) -> dict:
        action = service.removeprefix("ui.shortcuts.")
        expected = {"list": set(), "bind": {"key", "command", "replace"}, "unbind": {"key"}}
        if action not in expected or set(arguments) != expected[action]:
            raise PluginError("Invalid shortcut operation")
        if action == "list":
            return self.listing()
        key = self.check(arguments["key"])
        # Re-read before changing so another live session's preferences are not discarded.
        self.load()
        if self.error:
            raise PluginError(self.error)
        saved = dict(self.saved)
        if action == "bind":
            command = arguments["command"]
            if not isinstance(command, str) or self.target(command) is None:
                raise PluginError("Target must be an active plugin command, named plugin.command")
            if type(arguments["replace"]) is not bool:
                raise PluginError("replace must be boolean")
            builtin = any(tuple(normalized_key(k) for k in binding.keys) == (key,) for binding in self.tui.make_bindings().bindings)
            # Treat Ctrl keys conservatively: readline owns them too. Function keys have
            # inert fallback bindings in prompt-toolkit, not host actions to replace.
            occupied = key in saved or bool(builtin) or key.startswith("c-")
            if occupied and saved.get(key) != command and not arguments["replace"]:
                raise PluginError(f"{key} is already bound ({saved.get(key, 'host/readline')}); choose another key or explicitly replace it")
            saved[key] = command
        else:
            saved.pop(key, None)
        if len(saved) > 64:
            raise PluginError("At most 64 global plugin shortcuts are supported")
        self.save(saved)
        self.saved = saved
        self.publish()
        return self.listing()

    def save(self, saved: dict[str, str]) -> None:
        """Publish only after the comment-preserving config write succeeds."""
        self.preferences.save("shortcuts", "bindings", saved)
