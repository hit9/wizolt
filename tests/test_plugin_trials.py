"""Offline trials run real plugin processes and export the actual host projection."""

import asyncio
import json
import os
import signal
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path

import pytest
from PIL import Image

from wizolt.plugins.loading import PluginSource
from wizolt.plugins.process import PluginProcess
from wizolt.plugins.testing import PluginTrial, Stimulus
from wizolt.sdk import Context
from wizolt.ui.cli.plugin_preview import PreviewExporter
from wizolt.ui.cli.plugins import PluginView
from wizolt.ui.render import Theme


@pytest.fixture
def trial():
    return PluginTrial(Context("preview", "main", "/tmp", "running", 21, 0, "test", 0, 30))


def plugin(tmp_path, body):
    path = tmp_path / "sample.py"
    path.write_text("SDK_VERSION = 1\n" + body)
    return str(path)


async def test_failed_setup_reports_its_own_traceback(trial, tmp_path):
    report = await trial.run(plugin(tmp_path, 'def setup(p):\n    raise RuntimeError("bad setup")\n'))
    assert report.status == "failed" and report.stage == "load"
    assert "bad setup" in report.traceback and "not loaded" not in report.traceback


async def test_trial_observes_explicit_actions_events_frames_and_logs(trial, tmp_path):
    path = plugin(
        tmp_path,
        """from wizolt.sdk import Panel, Text
def setup(p):
    state = {"count": 0}
    async def done(event):
        state["count"] += 1
    async def add(ctx, args):
        state["count"] += args["amount"]
        print("action finished", flush=True)
        return "added"
    p.on("turn.finished", done)
    p.command("add", "Add", add)
    p.field("count", lambda ctx: state["count"])
    p.component("above_input", lambda ctx: Panel((Text(str(ctx.now)),)))
""",
    )
    validated = await trial.run(path, validate=True)
    assert validated.status == "passed" and not validated.frames
    report = await trial.run(path, times=(0, 0.5), stimuli=(Stimulus("event", "turn.finished"), Stimulus("command", "add", {"amount": 2})))
    assert report.status == "passed" and report.results == [None, "added"]
    assert report.frames[0]["fields"] == {"count": 3}
    assert report.frames[1]["panels"]["above_input"]["rows"][0]["text"] == "0.5"
    assert "action finished" in report.log
    # Independent trials do not inherit earlier plugin state or touch installation records.
    fresh = await trial.run(path)
    assert fresh.frames[0]["fields"] == {"count": 0}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sample.py"]


@pytest.mark.parametrize(
    "body,stage,needle",
    [
        ("def setup(p):\n    raise RuntimeError('setup failed')\n", "load", "setup failed"),
        ("def setup(p):\n    p.field('bad', lambda ctx: 1/0)\n", "snapshot", "ZeroDivisionError"),
        ("def setup(p):\n    p.component('above_input', lambda ctx: 'bad')\n", "snapshot", "Panel"),
        ("def setup(p):\n    p.field('bad', lambda ctx: float('nan'))\n", "snapshot", "finite"),
        ("import os\ndef setup(p):\n    os._exit(7)\n", "load", "exited"),
    ],
)
async def test_trial_returns_actionable_failures(trial, tmp_path, body, stage, needle):
    report = await trial.run(plugin(tmp_path, body))
    assert report.status == "failed" and report.stage == stage and needle in report.error
    if needle != "exited":
        assert "sample.py" in report.traceback or "worker.py" in report.traceback


@pytest.mark.parametrize("phase", ["setup", "component", "command"])
async def test_trial_kills_noncooperative_plugin_without_blocking_host(trial, tmp_path, phase):
    pid = tmp_path / "pid"
    source = f"""import os
from pathlib import Path
Path({str(pid)!r}).write_text(str(os.getpid()))
def block(ctx=None):
    while True:
        pass
async def command(ctx, args):
    block()
def setup(p):
"""
    source += {
        "setup": "    block()\n",
        "component": "    p.component('above_input', block)\n",
        "command": "    p.command('block', 'Block', command)\n",
    }[phase]
    trial.timeout = 0.5
    stimuli = (Stimulus("command", "block"),) if phase == "command" else ()
    async with asyncio.timeout(5):
        task = asyncio.create_task(trial.run(plugin(tmp_path, source), stimuli=stimuli))
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.02)
            ticks += 1
        report = await task
    assert ticks > 1 and report.status == "failed" and "timed out" in report.error
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


async def test_cancelled_worker_request_is_reaped(trial, tmp_path):
    path = plugin(
        tmp_path,
        """def setup(p):
    async def block(ctx, args):
        while True:
            pass
    p.command("block", "Block", block)
""",
    )
    worker, _ = await PluginProcess.start(PluginSource.read(path))
    try:
        call = asyncio.create_task(worker.request("invoke", kind="command", name="block", context=asdict(trial.context), arguments={}))
        await asyncio.sleep(0.1)
        call.cancel()
        async with asyncio.timeout(5):
            with pytest.raises(asyncio.CancelledError):
                await call
        assert worker.process.returncode == -signal.SIGKILL
    finally:
        await worker.close()


async def test_preview_matches_live_clipping_and_exports_images(trial, tmp_path):
    from wizolt.plugins.protocol import Snapshot

    original = Theme.name()
    Theme.set_mode("forest")
    try:
        path = plugin(
            tmp_path,
            """from wizolt.sdk import Panel, Text
def setup(p):
    p.component("above_input", lambda ctx: Panel((Text("猫" * 40 + "<tag>", "accent"), Text("safe\\x1b[31m text"))))
""",
        )
        report = await trial.run(path)
        frame = report.frames[0]
        exported = PreviewExporter(tmp_path).export(frame, 0)[0]
        panel = Snapshot.decode(frame).panels["above_input"]
        assert exported["styles"] == PluginView.render([panel], 30, PluginView.input_rows(24))
        assert exported["clipped"] and "\x1b" not in exported["text"]
        svg = ET.parse(exported["svg_path"])
        assert svg.getroot().tag.endswith("svg")
        with Image.open(exported["png_path"]) as image:
            assert image.width == 30 * PreviewExporter.CELL_WIDTH + 2 * PreviewExporter.PADDING
            assert len(image.getcolors(image.width * image.height)) > 1
    finally:
        Theme.set_mode(original)


async def test_cli_returns_json_and_preview_without_starting_agent(tmp_path):
    path = Path(__file__).parents[1] / "wizolt/plugins/builtin/pet.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "wizolt",
        "plugin",
        "test",
        str(path),
        "--theme",
        "forest",
        "--output",
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    report = json.loads(stdout)
    assert report["status"] == "passed" and Path(report["previews"][0]["png_path"]).is_file()
    assert "starting" not in stdout.decode()


async def test_cli_honors_custom_theme_and_project_preferences(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(f'''[paths]
data_dir = "{tmp_path}/data"
[runtime]
theme = "personal"
[ui.themes.personal]
base = "forest"
[ui.themes.personal.colors]
accent = "#aabbcc"
''')
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "wizolt",
        "plugin",
        "test",
        "pet",
        "--config",
        str(config),
        "--project",
        str(tmp_path),
        "--status",
        "running",
        "--output",
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode() + stdout.decode()
    report = json.loads(stdout)
    assert report["theme"] == "personal"
    assert any("#aabbcc" in style for style, _ in report["previews"][0]["styles"])
    assert report["frames"][0]["context"]["cwd"] == str(tmp_path)


def test_source_rejects_fifo_and_bounds_in_memory_revisions(tmp_path):
    from wizolt.plugins.loading import MAX_SOURCE_BYTES
    from wizolt.sdk import PluginError

    fifo = tmp_path / "pipe.py"
    os.mkfifo(fifo)
    with pytest.raises(PluginError, match="regular file"):
        PluginSource.read(str(fifo))
    with pytest.raises(PluginError, match="256 KiB"):
        PluginSource.read(str(tmp_path / "huge.py"), "x" * (MAX_SOURCE_BYTES + 1))


def test_source_admits_only_public_sdk_imports(tmp_path):
    from wizolt.sdk import SDK_VERSION, PluginError

    body = f'''"""A doc."""
SDK_VERSION = {SDK_VERSION}
import json
import wizolt
from wizolt import sdk
from wizolt.sdk import Plugin


def setup(plugin):
    plugin.command("sample", "does nothing", lambda context, arguments: "ok")
'''
    path = tmp_path / "boundary.py"
    path.write_text(body)
    assert PluginSource.read(str(path)).name == "boundary"  # The allowed set passes.
    for bad in ("import wizolt.plugins.session", "from wizolt.config import Config", "from wizolt import plugins"):
        path.write_text(f"{body}\n{bad}\n")
        with pytest.raises(PluginError) as error:
            PluginSource.read(str(path))
        assert "plugins may import only the public SDK (wizolt.sdk)" in str(error.value)


async def test_protocol_damage_and_oversized_component_do_not_hang(trial, tmp_path):
    # Descriptor 1 is no longer the protocol; damage the worker's private stream itself.
    damaged = plugin(
        tmp_path,
        "import gc\ndef setup(p):\n"
        "    worker = next(o for o in gc.get_objects() if type(o).__name__ == 'Worker')\n"
        "    worker.output.write('not json\\n')\n    worker.output.flush()\n",
    )
    async with asyncio.timeout(5):
        report = await trial.run(damaged)
    assert report.status == "failed" and "protocol" in report.error
    oversized = plugin(
        tmp_path,
        """from wizolt.sdk import Panel, Text
def setup(p):
    p.component("above_input", lambda ctx: Panel((Text("x" * 5000),)))
""",
    )
    report = await trial.run(oversized)
    assert report.status == "failed" and "4096" in report.error


async def test_long_field_text_is_rejected_before_any_paint(trial, tmp_path):
    from wizolt.plugins.protocol import Snapshot
    from wizolt.sdk import PluginError

    report = await trial.run(plugin(tmp_path, 'def setup(p):\n    p.field("long", lambda ctx: "x" * 5000)\n'), times=(0,))
    assert report.status == "failed" and "at most 4096 characters" in report.error
    with pytest.raises(PluginError, match="4096"):  # The host does not trust the worker's check.
        Snapshot.decode({"fields": {"long": "x" * 5000}, "panels": {}})


async def test_stderr_flood_costs_the_host_little_cpu(tmp_path):
    import time

    source = plugin(
        tmp_path,
        """import sys, time
def setup(p):
    async def flood(ctx, args):
        end = time.monotonic() + 1
        while time.monotonic() < end:
            sys.stderr.write(("x" * 200 + "\\n") * 50)
        return "done"
    p.command("flood", "Flood", flood)
""",
    )
    worker, _ = await PluginProcess.start(PluginSource.read(source))
    try:
        cpu = time.process_time()
        context = asdict(Context("a", "main", "/tmp", "idle", 0, 0, "m", 0))
        assert await worker.request("invoke", kind="command", name="flood", arguments={}, context=context, timeout=10) == "done"
        assert time.process_time() - cpu < 0.5  # Draining at full speed took about a core.
        assert "xxxx" in worker.stderr  # Its log tail is still kept.
    finally:
        await worker.close()


async def test_plugin_subprocesses_cannot_read_or_write_the_protocol(trial, tmp_path):
    source = plugin(
        tmp_path,
        """import os
def setup(p):
    async def shell(ctx, args):
        os.write(1, b"raw descriptor\\n")
        os.system("echo child stdout; head -c 1")  # head would otherwise eat a host request.
        return "ok"
    p.command("shell", "Shell", shell)
""",
    )
    async with asyncio.timeout(10):
        report = await trial.run(source, stimuli=(Stimulus("command", "shell"), Stimulus("command", "shell")))
    assert report.status == "passed" and report.results == ["ok", "ok"]
    assert "raw descriptor" in report.log and "child stdout" in report.log


async def test_explicit_bad_config_fails_without_falling_back(tmp_path):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "wizolt",
        "plugin",
        "list",
        "--config",
        str(tmp_path / "missing.toml"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert process.returncode == 1
    assert json.loads(stdout)["status"] == "failed"
    assert not list(tmp_path.iterdir())
