"""Plugin activation, IPC sampling and cached UI reads; no models, network or user state.

Use the exported source for workers as well as the host. Without PYTHONPATH the worker's
``-P -m`` would silently benchmark the installed checkout instead of the selected revision.
Timing is observational; no wall-clock threshold belongs in the test suite.
"""

import argparse
import asyncio
import gc
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
