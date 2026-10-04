"""Scripted human responses using the real dialog states, with no terminal or input waits.

Fixtures match ordered declarations, not random runtime view IDs. Optional update expectations
keep a live view open until its specified revisions arrive. Every wait remains bounded.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from wizolt.sdk import PluginError
from wizolt.sdk.views import View
from wizolt.ui.render import UiPrinter
from wizolt.ui.tui.plugin_views import DialogState, FormState, SelectionState


@dataclass
class ScriptedView:
    state: DialogState
    step: dict
    future: asyncio.Future
    updates: list[dict]


class ScriptedDialogs:
    def __init__(self, steps: list[dict], width: int, height: int, timeout: float):
        if not isinstance(steps, list) or len(steps) > 64 or any(not isinstance(step, dict) for step in steps):
            raise PluginError("Interactions must be a list of at most 64 steps")
        self.steps = steps
        self.width, self.height, self.timeout = width, height, timeout
        self.position = 0
        self.open: dict[str, ScriptedView] = {}
        self.trace: list[dict] = []
        self.error = ""

    def finish(self) -> None:
        if self.error:
            raise PluginError(self.error)
        if self.position != len(self.steps):
            raise PluginError(f"Unused interaction steps: {len(self.steps) - self.position}")

    @staticmethod
    def expect(view: View, expected: dict) -> None:
        if not isinstance(expected, dict) or set(expected) != {"title", "kind"}:
            raise PluginError("Each expectation requires title and kind")
        if expected != {"title": view.title, "kind": view.body.kind}:
            raise PluginError(f"Unexpected view: {view.title!r} ({view.body.kind})")

    async def call(self, service: str, arguments: dict) -> dict:
        try:
            return await self._call(service, arguments)
        except Exception as error:
            self.error = str(error)
            raise

    async def _call(self, service: str, arguments: dict) -> dict:
        if service == "ui.notify":
            self.trace.append({"notice": arguments})
            return {}
        if service not in {"ui.views.show", "ui.views.update", "ui.views.close"}:
            raise PluginError(f"Host service unavailable in scripted trials: {service}")
        identity = arguments["id"]
        if service == "ui.views.close":
            if (item := self.open.get(identity)) and not item.future.done():
                if item.updates:
                    self.error = "View closed before expected updates arrived"
                item.future.set_result(None)
            return {}
        view = View.decode(arguments["view"])
        if service == "ui.views.update":
            item = self.open.get(identity)
            if item is None or not item.updates:
                raise PluginError("Unexpected view update")
            self.expect(view, item.updates.pop(0))
            item.state.update(view)
            self.capture(item.state)
            self.answer(item)
            return {}
        if self.open:
            raise PluginError("A plugin may own only one open view per agent")
        if self.position >= len(self.steps):
            raise PluginError(f"Missing interaction answer for {view.title!r}")
        step = self.steps[self.position]
        self.position += 1
        if not {"expect", "reply"} <= step.keys() or step.keys() - {"expect", "reply", "updates"}:
            raise PluginError("Interaction step requires expect, reply and optional updates")
        self.expect(view, step["expect"])
        updates = step.get("updates", [])
        if not isinstance(updates, list) or len(updates) > 64:
            raise PluginError("updates must be a bounded expectation list")
        state = DialogState(view, lambda: (self.width, self.height), UiPrinter())
        item = ScriptedView(state, step, asyncio.get_running_loop().create_future(), list(updates))
        self.open[identity] = item
        try:
            self.capture(state)
            self.answer(item)
            # Unlike a human view, a fixture may never exempt an action from its deadline.
            result = await asyncio.wait_for(item.future, self.timeout)
            self.trace.append({"result": result})
            return {"result": result}
        finally:
            self.open.pop(identity, None)

    def capture(self, state: DialogState) -> None:
        self.trace.append({"view": asdict(state.view), "fragments": list(state.fragments())})

    def answer(self, item: ScriptedView) -> None:
        if item.updates:
            return
        reply, state = item.step["reply"], item.state
        if reply is None:
            item.future.set_result(None)
            return
        if not isinstance(reply, dict) or reply.keys() - {"action", "selected", "values"}:
            raise PluginError("Reply must be a view result or null")
        action = reply.get("action", state.view.actions[0].id if state.view.actions else "submit")
        if action not in ({item.id for item in state.view.actions} if state.view.actions else {"submit"}):
            raise PluginError("Unknown view action")
        if isinstance(state.body, SelectionState):
            selected = reply.get("selected", list(state.body.body.selected))
            choices = state.body.state.choices
            if not isinstance(selected, list) or any(value not in choices for value in selected) or len(set(selected)) != len(selected):
                raise PluginError("Unknown or duplicate selection IDs")
            if not state.body.body.multiple and len(selected) != 1:
                raise PluginError("Select exactly one choice")
            if reply.get("values"):
                raise PluginError("Selection replies cannot contain form values")
            state.body.checked = set(selected)
            if selected:
                state.body.state.selected = choices.index(selected[0])
        elif isinstance(state.body, FormState):
            values = reply.get("values", {})
            if not isinstance(values, dict) or values.keys() - state.body.buffers.keys() or reply.get("selected"):
                raise PluginError("Unknown form fields")
            for key, value in values.items():
                field = next(field for field in state.body.fields if field.id == key)
                if (
                    not isinstance(value, str)
                    or len(value) > 16_000
                    or (not field.multiline and "\n" in value)
                    or any(ord(character) < 32 and character not in "\n\t" for character in value)
                ):
                    raise PluginError("Form values must be bounded text")
                state.body.buffers[key].text = value
        elif reply.get("selected") or reply.get("values"):
            raise PluginError("Document replies only support actions")
        result = state.body.result(action)
        self.capture(state)
        if result is None:
            raise PluginError("Invalid form response")
        item.future.set_result(asdict(result))
