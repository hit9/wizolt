"""Private plugin worker protocol. User Python never executes in the terminal process.

The protocol owns stdout; ordinary plugin prints go to stderr. The parent enforces deadlines
and can kill this process even when a callback blocks its event loop. This is fault isolation,
not a filesystem or network sandbox. Only JSON values cross the boundary.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import traceback
from contextvars import ContextVar
from dataclasses import asdict
from typing import Any

from wizolt.plugins.loading import LoadedPlugin, PluginSource
from wizolt.plugins.protocol import MAX_FRAME, Snapshot
from wizolt.sdk import Context, Event, PluginError


class Worker:
    def __init__(self):
        self.output = sys.stdout
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
            return {
                "fields": list(plugin.fields),
                "slots": list(plugin.components),
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
        if self.loaded is None:
            raise PluginError("Plugin is not loaded")
        plugin = self.loaded.plugin
        if operation == "shutdown":
            # Join callbacks before closing their resources. Never cancel this shutdown call.
            tasks = [task for task in self.tasks.values() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await plugin.services.close()
            return None
        context = Context.decode(request["context"])
        if operation == "compact":
            if plugin.summary_handler is None:
                raise PluginError("Plugin has no summarizer")
            with plugin.services.invocation():
                summary = await plugin.summary_handler(context, request["text"])
            if not isinstance(summary, str) or not summary.strip() or len(summary) > 16000:
                raise PluginError("Summarizer must return non-empty text of at most 16000 characters")
            return summary.strip()
        if operation == "snapshot":
            for callback in plugin.observers.get("sample", ()):
                await callback(Event("sample", context))
            fields = {}
            for name, callback in plugin.fields.items():
                value = callback(context)
                if type(value) not in (str, int, float, bool):
                    raise PluginError(f"Field {name} must return a scalar")
                if isinstance(value, float) and not math.isfinite(value):
                    raise PluginError(f"Field {name} must return a finite number")
                fields[name] = value
            panels = {}
            for slot, callback in plugin.components.items():
                panel = callback(context)
                Snapshot.check_panel(panel)
                panels[slot] = asdict(panel)
            return {"fields": fields, "panels": panels}
        if operation == "event":
            for callback in plugin.observers.get(request["name"], ()):
                await callback(Event(request["name"], context))
            return None
        if operation == "invoke":
            if request["kind"] not in ("command", "tool"):
                raise PluginError("Invocation kind must be command or tool")
            registry = plugin.commands if request["kind"] == "command" else plugin.tools
            if request["kind"] == "tool":
                from jsonschema import Draft202012Validator

                Draft202012Validator(dict(registry[request["name"]].parameters)).validate(request["arguments"])
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
            while line := await asyncio.to_thread(sys.stdin.readline, MAX_FRAME + 1):
                if len(line) > MAX_FRAME:
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
