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
            return {
                "fields": list(plugin.fields),
                "slots": list(plugin.components),
                "events": list(plugin.observers),
                "themes": plugin.themes,
                "presets": plugin.presets,
                "commands": {
                    name: {"description": action.description, "parameters": dict(action.parameters), "during_turn": action.during_turn}
                    for name, action in plugin.commands.items()
                },
                "tools": {name: {"description": action.description, "parameters": dict(action.parameters)} for name, action in plugin.tools.items()},
            }
        if self.loaded is None:
            raise PluginError("Plugin is not loaded")
        plugin = self.loaded.plugin
        context = Context.decode(request["context"])
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
            result = await registry[request["name"]].handler(context, request["arguments"])
            if not isinstance(result, str):
                raise PluginError("Plugin actions must return text")
            return result
        raise PluginError(f"Unknown worker operation: {operation}")

    async def respond(self, request: dict) -> None:
        identity = request["id"]
        try:
            value = await self.dispatch(request)
            self.write({"id": identity, "value": value})
        except BaseException as error:  # noqa: BLE001 - all plugin exits must become worker responses.
            # SystemExit must be a failed call, while a native crash is detected by parent EOF.
            self.write({"id": identity, "error": f"{type(error).__name__}: {error}"[:8192], "traceback": traceback.format_exc()[-16384:]})
        finally:
            self.tasks.pop(identity, None)

    async def run(self) -> None:
        try:
            while line := await asyncio.to_thread(sys.stdin.readline, MAX_FRAME + 1):
                if len(line) > MAX_FRAME:
                    raise PluginError("Plugin request exceeds protocol frame limit")
                request = json.loads(line)
                if request.get("operation") == "cancel":
                    if task := self.tasks.get(request["target"]):
                        task.cancel()
                else:
                    self.tasks[request["id"]] = asyncio.create_task(self.respond(request))
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
            if self.loaded is not None:
                self.loaded.close()


if __name__ == "__main__":
    asyncio.run(Worker().run())
