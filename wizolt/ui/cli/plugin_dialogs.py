"""Bind request-owned plugin interactions to one agent's TUI.

The serial queue includes approval contention and agent switching. A cancelled queued request
never appears later. Only host-owned presentation code runs in paint/key callbacks; plugin
actions receive a result after the view has released focus.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

from wizolt.formats import clean
from wizolt.sdk import PluginError
from wizolt.sdk.operations import MAX_TEXT
from wizolt.sdk.views import View
from wizolt.ui.tui.app import TuiApp
from wizolt.ui.tui.keys import normalized_key
from wizolt.ui.tui.plugin_views import DialogState

if TYPE_CHECKING:
    from wizolt.ui.cli.plugin_keys import PluginKeys
    from wizolt.ui.cli.presentation import Presentation


@dataclass
class Dialog:
    state: DialogState
    task: asyncio.Task | None = None
    dismissed: bool = False


class PluginDialogs:
    def __init__(self, tui: TuiApp, presentation: Presentation, keys: PluginKeys | None = None):
        self.tui = tui
        self.presentation = presentation
        self.dialogs: dict[tuple[str, str], Dialog] = {}
        self.presenting: Dialog | None = None
        self.lock = asyncio.Lock()
        self.keys = keys

    def size(self, fullscreen: bool) -> tuple[int, int]:
        if self.tui.app is None:
            return 80, 16
        size = self.tui.app.output.get_size()
        return size.columns, max(1, (size.rows - 2 if fullscreen else TuiApp.modal_rows(size.rows)) - 1)

    async def present(self, dialog: Dialog):
        async with self.lock:
            self.presenting = dialog
            state = dialog.state
            self.bind_keys(state)
            try:
                return await self.tui.show_modal(state.fragments, state.key, exclusive=state.view.fullscreen, wait_for_input=True)
            finally:
                self.presenting = None
                self.tui.view_bindings = KeyBindings()

    def bind_keys(self, state: DialogState) -> None:
        """Only this modal's actions are active; text entry keeps ordinary characters."""
        bindings = KeyBindings()
        for action in state.view.actions:
            if not action.key:
                continue
            key = normalized_key(action.key)
            active = Condition(lambda key=key: bool(self.tui.modal and self.tui.modal.key_fn == state.key and not (state.body.editing and len(key) == 1)))
            bindings.add(key, filter=active, eager=True)(lambda event, key=key: self.tui.dispatch_modal_key(key, event.data))
        self.tui.view_bindings = bindings

    async def edit(self, owner: str, arguments: dict) -> str | None:
        """``ui.edit``: the user's editor on the plugin's text, as Ctrl-G edits the input. A plugin
        owning an open view cannot also take the terminal from under it."""
        text = arguments.get("text")
        if set(arguments) != {"text"} or not isinstance(text, str) or len(text) > MAX_TEXT:
            raise PluginError(f"The editor takes text of at most {MAX_TEXT // 1024} KiB")
        if any(name == owner for name, _ in self.dialogs):
            raise PluginError("Close the plugin's open view before opening the editor")
        if not self.tui.managed and (self.tui.app is None or not self.tui.app.is_running):
            raise PluginError("The editor requires a running TUI session")
        async with self.lock:  # Waits behind another plugin's view, like a second view would.
            return await self.tui.edit_text(text)

    async def call(self, owner: str, service: str, arguments: dict) -> dict:
        if service.startswith("ui.shortcuts."):
            if self.keys is None:
                raise PluginError("Shortcut management is unavailable")
            return await self.keys.call(service, arguments)
        if service == "ui.notify":
            message, level = arguments.get("message"), arguments.get("level")
            if (
                set(arguments) != {"message", "level"}
                or not isinstance(message, str)
                or len(message) > 4000
                or level not in {"info", "success", "warning", "error"}
            ):
                raise PluginError("Notice requires bounded text and an info/success/warning/error level")
            # Semantic transcript styling belongs to the normal output writer, including
            # background-agent routing. A plugin never writes directly to the terminal.
            from wizolt.base import LogBlock, LogLine, LogRole

            roles = {"info": LogRole.MUTED, "success": LogRole.SUCCESS, "warning": LogRole.WARNING, "error": LogRole.ERROR}
            self.presentation.emit(LogBlock([LogLine(f"plugin [{owner}]", clean(message), roles[level])]))
            return {}
        if service == "ui.edit":
            return {"text": await self.edit(owner, arguments)}
        if service not in {"ui.views.show", "ui.views.update", "ui.views.close"}:
            raise PluginError(f"Unknown UI operation: {service}")
        identity = arguments.get("id")
        if not isinstance(identity, str) or not identity or len(identity) > 100:
            raise PluginError("View requires a bounded identity")
        key = owner, identity
        if set(arguments) != ({"id"} if service == "ui.views.close" else {"id", "view"}):
            raise PluginError("Invalid view operation arguments")
        if service == "ui.views.close":
            if dialog := self.dialogs.get(key):
                dialog.dismissed = True
                if dialog.task is not None:
                    dialog.task.cancel()
            return {}
        view = View.decode(arguments["view"])
        if service == "ui.views.update":
            if key not in self.dialogs:
                raise PluginError("View is no longer open")
            self.dialogs[key].state.update(view)
            # The presentation may still be waiting behind an approval or agent switch.
            # Its declaration and keys must advance together before the modal opens.
            if self.presenting is self.dialogs[key]:
                self.bind_keys(self.dialogs[key].state)
            self.tui.invalidate()
            return {}
        if key in self.dialogs or any(name == owner for name, _ in self.dialogs):
            raise PluginError("A plugin may own only one open view per agent")
        if not self.tui.managed and (self.tui.app is None or not self.tui.app.is_running):
            raise PluginError("Interactive UI requires a running TUI session")
        state = DialogState(view, lambda: self.size(view.fullscreen), self.presentation.ui)
        dialog = self.dialogs[key] = Dialog(state)
        dialog.task = asyncio.create_task(self.present(dialog))
        try:
            result = await dialog.task
            return {"result": asdict(result) if result is not None else None}
        except asyncio.CancelledError:
            if not dialog.dismissed:
                raise
            return {"result": None}
        finally:
            self.dialogs.pop(key, None)
