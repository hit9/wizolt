"""Private plugin worker protocol. User Python never executes in the terminal process.

The protocol owns stdout; ordinary plugin prints go to stderr. The parent enforces deadlines
and can kill this process even when a callback blocks its event loop. This is fault isolation,
not a filesystem or network sandbox. Only JSON values cross the boundary.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import traceback
from contextvars import ContextVar
from dataclasses import asdict, replace
from typing import Any

from wizolt.plugins.layout import SLOTS, LayoutBudget
from wizolt.plugins.loading import LoadedPlugin, PluginSource
from wizolt.plugins.protocol import MAX_FRAME, MAX_REQUEST, MAX_ROW_CHARACTERS, Snapshot
from wizolt.sdk import Context, Event, PluginError, ToolActivity


class Worker:
    def __init__(self):
        # Subprocesses inherit descriptors 0 and 1, not sys.stdin/sys.stdout. Move the protocol
        # to private, non-inheritable duplicates: a plugin's `git status` must neither corrupt
        # replies on the protocol stdout nor consume host requests from the protocol stdin.
        self.input = os.fdopen(os.dup(0), "r", encoding="utf-8")
        self.output = os.fdopen(os.dup(1), "w", encoding="utf-8")
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        self.loaded: LoadedPlugin | None = None
        self.tasks: dict[int, asyncio.Task] = {}
        self.current_request: ContextVar[int | None] = ContextVar("plugin_request", default=None)
        self.service_sequence = 0
        self.services: dict[int, asyncio.Future] = {}

    async def call_host(self, service: str, arguments: dict) -> dict:
        parent = self.current_request.get()
        if parent is None or self.loaded is None or not self.loaded.plugin.services.invoking.get():
            raise PluginError("Host services require an explicit action")
        self.service_sequence += 1
        identity = self.service_sequence
        future = asyncio.get_running_loop().create_future()
        self.services[identity] = future
        try:
            self.write({"service_id": identity, "parent": parent, "service": service, "arguments": arguments})
            return await future
        finally:
            self.services.pop(identity, None)

    def write(self, value: dict) -> None:
        frame = json.dumps(value, ensure_ascii=True, allow_nan=False)
        if len(frame) > MAX_FRAME:
            raise PluginError("Plugin response exceeds protocol frame limit")
        self.output.write(frame + "\n")
        self.output.flush()

    async def dispatch(self, request: dict) -> Any:
        operation = request["operation"]
        if operation == "load":
            source = PluginSource(**request["revision"])
            self.loaded = LoadedPlugin.load(source, request.get("config"), request.get("directory", ""))
            plugin = self.loaded.plugin
            plugin.models.call = self.call_host
            plugin.settings.call = self.call_host
            plugin.ui.components.call = self.call_host
            plugin.ui.call = self.call_host
            plugin.ui.shortcuts.call = self.call_host
            return {
                "fields": list(plugin.fields),
                "slots": {slot: item.gap_before for slot, item in plugin.components.items()},
                "events": list(plugin.observers),
                "themes": plugin.themes,
                "presets": plugin.presets,
                "summarizer": plugin.summary_handler is not None,
                "commands": {
                    name: {"description": action.description, "parameters": dict(action.parameters), "during_turn": action.during_turn}
                    for name, action in plugin.commands.items()
                },
                "tools": {name: {"description": action.description, "parameters": dict(action.parameters)} for name, action in plugin.tools.items()},
            }
        if operation == "shutdown":
            # Join callbacks before closing their resources. Never cancel this shutdown call.
            tasks = [task for task in self.tasks.values() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # A failed load has nothing to close; answering with an error here would replace
            # the load's traceback, which is the one the author needs.
            if self.loaded is not None:
                await self.loaded.plugin.services.close()
            return None
        if self.loaded is None:
            raise PluginError("Plugin is not loaded")
        plugin = self.loaded.plugin
        context = Context.decode(request["context"])
        if operation == "compact":
            if plugin.summary_handler is None:
                raise PluginError("Plugin has no summarizer")
            with plugin.services.invocation():
                summary = await plugin.summary_handler(context, request["text"])
            if not isinstance(summary, str) or not summary.strip() or len(summary) > 16000:
                raise PluginError("Summarizer must return non-empty text of at most 16000 characters")
            return summary.strip()
        if operation in ("snapshot", "sample", "sample_render"):
            sampled = replace(context, layout=None)
            # The public name says what it is: the refresh tick, not a lifecycle transition.
            for callback in plugin.observers.get("tick", ()):
                await callback(Event("tick", sampled))
            fields = {}
            for name, callback in plugin.fields.items():
                value = callback(sampled)
                if type(value) not in (str, int, float, bool):
                    raise PluginError(f"Field {name} must return a scalar")
                if isinstance(value, float) and not math.isfinite(value):
                    raise PluginError(f"Field {name} must return a finite number")
                if isinstance(value, str) and len(value) > MAX_ROW_CHARACTERS:
                    raise PluginError(f"Field {name} must return at most {MAX_ROW_CHARACTERS} characters")
                fields[name] = value
            panels = {}
            if operation == "snapshot":
                budget = LayoutBudget(context.viewport)
                for slot in SLOTS:
                    if slot in plugin.components:
                        item = plugin.components[slot]
                        allocated = budget.context(context, slot, item.gap_before)
                        assert allocated.layout is not None
                        panel = item.callback(allocated)
                        Snapshot.check_panel(panel)
                        panels[slot] = asdict(budget.consume(allocated.layout, panel))
            elif operation == "sample_render":
                if context.layout is None:
                    raise PluginError("Component rendering requires a layout allocation")
                panel = plugin.components[context.layout.slot].callback(context)
                Snapshot.check_panel(panel)
                panels[context.layout.slot] = asdict(panel)
            return {"fields": fields, "panels": panels}
        if operation == "render":
            if context.layout is None:
                raise PluginError("Component rendering requires a layout allocation")
            panel = plugin.components[context.layout.slot].callback(context)
            Snapshot.check_panel(panel)
            return asdict(panel)
        if operation == "event":
            tool = ToolActivity(**request["tool"]) if request.get("tool") else None
            for callback in plugin.observers.get(request["name"], ()):
                await callback(Event(request["name"], context, tool, request.get("reason", "")))
            return None
        if operation == "invoke":
            if request["kind"] not in ("command", "tool"):
                raise PluginError("Invocation kind must be command or tool")
            registry = plugin.commands if request["kind"] == "command" else plugin.tools
            if request["kind"] == "tool":
                from jsonschema import Draft202012Validator
                from jsonschema.exceptions import ValidationError
                from referencing import Registry

                # An empty retrieval registry retains in-document references without
                # jsonschema's implicit network fetches. Validation is worker-owned so
                # expensive schemas cannot block the host's event loop or cancellation.
                try:
                    Draft202012Validator(dict(registry[request["name"]].parameters), registry=Registry()).validate(request["arguments"])
                except ValidationError as error:
                    raise PluginError(f"Invalid arguments for {plugin.name}.{request['name']}: {error.message}") from error
            with plugin.services.invocation():
                result = await registry[request["name"]].handler(context, request["arguments"])
            if not isinstance(result, str):
                raise PluginError("Plugin actions must return text")
            return result
        raise PluginError(f"Unknown worker operation: {operation}")

    async def respond(self, request: dict) -> None:
        identity = request["id"]
        token = self.current_request.set(identity)
        try:
            value = await self.dispatch(request)
            self.write({"id": identity, "value": value})
        except BaseException as error:  # noqa: BLE001 - all plugin exits must become worker responses.
            # SystemExit must be a failed call, while a native crash is detected by parent EOF.
            self.write({"id": identity, "error": f"{type(error).__name__}: {error}"[:8192], "traceback": traceback.format_exc()[-16384:]})
        finally:
            self.current_request.reset(token)
            self.tasks.pop(identity, None)

    async def run(self) -> None:
        try:
            while line := await asyncio.to_thread(self.input.readline, MAX_REQUEST + 1):
                if len(line) > MAX_REQUEST:
                    raise PluginError("Plugin request exceeds protocol frame limit")
                request = json.loads(line)
                if "service_result" in request:
                    future = self.services.get(request["service_result"])
                    if future is not None and not future.done():
                        if error := request.get("error"):
                            future.set_exception(PluginError(error))
                        else:
                            future.set_result(request["value"])
                elif request.get("operation") == "cancel":
                    if task := self.tasks.get(request["target"]):
                        task.cancel()
                else:
                    self.tasks[request["id"]] = asyncio.create_task(self.respond(request))
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
            if self.loaded is not None:
                await self.loaded.plugin.services.close()
                self.loaded.close()


if __name__ == "__main__":
    asyncio.run(Worker().run())
