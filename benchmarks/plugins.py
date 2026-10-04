"""Plugin activation, IPC sampling and cached UI reads; no models, network or user state.

Use the exported source for workers as well as the host. Without PYTHONPATH the worker's
``-P -m`` would silently benchmark the installed checkout instead of the selected revision.
Timing is observational; no wall-clock threshold belongs in the test suite.
"""

import argparse
import asyncio
import gc
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path


class Probe:
    def __init__(self, repeat):
        self.repeat = repeat
        self.results = {}

    async def measure(self, name, action):
        await action()  # Warm filesystem/import caches before the recorded samples.
        samples = []
        for _ in range(self.repeat):
            gc.collect()
            started = time.perf_counter()
            await action()
            samples.append(round((time.perf_counter() - started) * 1000, 6))
        self.results[name] = {"samples_ms": samples, "median_ms": statistics.median(samples)}

    async def run(self, directory):
        from wizolt.plugins.runtime import PluginRuntime
        from wizolt.sdk import Context
        from wizolt.ui.cli.plugins import PluginView

        context = Context("benchmark", "main", directory, "running", 21, 1, "local", 1, 100)
        paths = []
        for index in range(3):
            path = Path(directory) / f"meter_{index}.py"
            path.write_text('''from wizolt.sdk import Panel, Text
SDK_VERSION = 1
def setup(p):
    p.field("fill", lambda ctx: ctx.context_percent)
    p.component("above_input", lambda ctx: Panel((Text("context: " + str(ctx.context_percent), "accent"),)))
    async def echo(ctx, args):
        return "done"
    p.tool("echo", "Local echo", {"type": "object", "additionalProperties": False}, echo)
''')
            paths.append(str(path))

        async def activate():
            runtime = PluginRuntime(lambda: context)
            try:
                await runtime.manage("enable", paths[0])
            finally:
                await runtime.close()

        await self.measure("enable_close_one", activate)

        runtime = PluginRuntime(lambda: context)
        # Refresh explicitly to avoid a background sample overlapping a timed operation.
        runtime.REFRESH_INTERVAL = 3600
        try:
            for path in paths:
                await runtime.manage("enable", path)

            async def sample():
                for _ in range(20):
                    await runtime.refresh()

            async def invoke():
                for _ in range(20):
                    assert await runtime.invoke("meter_0", "tool", "echo", {}) == "done"

            async def paint():
                view = PluginView(runtime)
                for _ in range(1000):
                    assert len(runtime.fields()) == 3
                    assert view.fragments(100, 32)["above_input"]

            await self.measure("sample_three_20_times", sample)
            await self.measure("invoke_tool_20_times", invoke)
            await self.measure("cached_projection_1000_times", paint)
        finally:
            await runtime.close()

        # Dense, themed rows exercise what short scalar meters miss: repeated clipping of
        # unchanged multi-span snapshots while the input redraws faster than the sampler.
        from wizolt.sdk import Line, Panel, Text

        panels = {"above_input": [Panel(tuple(Line(tuple(Text("温度·" + str(index), "accent" if index % 2 else "muted") for index in range(64))) for _ in range(6)))]}

        async def dense_paint():
            for _ in range(1000):
                PluginView.project(panels, 100, 32)

        await self.measure("dense_projection_1000_times", dense_paint)
        rendered = json.dumps(PluginView.project(panels, 100, 32), ensure_ascii=True, sort_keys=True)
        self.results["dense_projection_1000_times"]["output_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
        from importlib.util import find_spec

        if find_spec("wizolt.plugins.presenters") is not None:  # Older revisions have no interception.
            await self.interception(directory, context)

    async def interception(self, directory, context):
        """Chain overhead the host pays per operation: the fast paths every turn takes, and the
        worker round trips a matching chain costs. Cores are trivial, so this is pure overhead."""
        from types import MappingProxyType

        from wizolt.plugins.runtime import PluginRuntime
        from wizolt.sdk.operations import ModelRequest, ModelResponse, ToolCall, ToolResult

        def plugin(name, body):
            path = Path(directory) / f"{name}.py"
            path.write_text("from wizolt.sdk import Panel, Text\nfrom wizolt.sdk.operations import *\nSDK_VERSION = 1\ndef setup(p):\n" + body)
            return str(path)

        relay = "    async def h(ctx, value, next):\n        return await next(value)\n    p.intercept('tool.call', h{})\n"
        bash = ToolCall("c1", "Bash", MappingProxyType({"command": "ls"}))
        read = ToolCall("c2", "Read", MappingProxyType({"path": "x"}))
        result = ToolResult("ok")

        async def core(_value):
            return result

        runtime = PluginRuntime(lambda: context)
        runtime.REFRESH_INTERVAL = 3600
        try:

            async def empty():
                for _ in range(1000):
                    await runtime.interception.run("tool.call", bash, core)

            await self.measure("chain_empty_1000_times", empty)
            await runtime.manage("enable", plugin("bashonly", relay.format(", match={'tool': 'Bash'}")))

            async def nonmatching():
                for _ in range(1000):
                    await runtime.interception.run("tool.call", read, core)

            await self.measure("chain_nonmatching_1000_times", nonmatching)
            for index in range(2):
                await runtime.manage("enable", plugin(f"relay_{index}", relay.format("")))

            async def noop_three():
                for _ in range(20):
                    assert await runtime.interception.run("tool.call", bash, core) == result

            await self.measure("chain_three_noop_20_times", noop_three)
        finally:
            await runtime.close()

        runtime = PluginRuntime(lambda: context)
        runtime.REFRESH_INTERVAL = 3600
        upper = "    async def h(ctx, request, next):\n        reply = await next(request)\n        return reply.replace(text=reply.text.upper())\n    p.intercept('model.request', h, response='replace')\n"
        card = "    async def card(ctx, view):\n        return Panel((Text(view.tool + ' ' + view.status, 'success'),))\n    p.presenter('tool.result', card)\n"
        request = ModelRequest("r1", "turn")
        reply = ModelResponse("x" * 4000)

        async def model_core(_value):
            return reply

        try:
            await runtime.manage("enable", plugin("upper", upper))
            await runtime.manage("enable", plugin("cards", card))

            async def transform():
                for _ in range(20):
                    assert (await runtime.interception.run("model.request", request, model_core)).text == reply.text.upper()

            async def present():
                from wizolt.sdk.presentation import ToolSummary

                for _ in range(20):
                    assert await runtime.presenters.render("tool.result", ToolSummary("c1", "Bash", {}, "ok", "a\nb"))

            await self.measure("buffered_response_transform_20_times", transform)
            await self.measure("present_tool_result_20_times", present)
            activity = "    async def line(ctx, view):\n        return Panel((Text(view.status, 'muted'),))\n    p.presenter('activity', line)\n"
            await runtime.manage("enable", plugin("activity", activity))

            async def refresh_activity():
                # The 5 Hz pass with an activity presenter: one extra round trip per refresh.
                for _ in range(20):
                    await runtime.refresh()
                assert runtime.presenters.activity is not None

            await self.measure("refresh_with_activity_presenter_20_times", refresh_activity)
        finally:
            await runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeat", type=int, default=9)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat must be positive")
    source = args.source.resolve()
    if not (source / "wizolt/plugins/runtime.py").exists():
        print(json.dumps({"results": {}}))
        return
    sys.path.insert(0, str(source))
    os.environ["PYTHONPATH"] = str(source)
    probe = Probe(args.repeat)
    with tempfile.TemporaryDirectory(prefix="wizolt-plugin-benchmark-") as directory:
        asyncio.run(probe.run(directory))
    print(json.dumps({"results": probe.results}, indent=2))


if __name__ == "__main__":
    main()
