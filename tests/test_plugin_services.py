"""Real workers own lazy connections through cancellation and generation replacement."""

import asyncio
from dataclasses import asdict

import pytest

from wizolt.plugins.loading import PluginSource
from wizolt.plugins.process import PluginProcess
from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, PluginError


def source(tmp_path, *, teardown="trace('closed')"):
    path = tmp_path / "service_plugin.py"
    log = tmp_path / "events"
    path.write_text(f"""SDK_VERSION = 1
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
def trace(text):
    with Path({str(log)!r}).open("a") as stream:
        stream.write(text + "\\n")
@asynccontextmanager
async def connection():
    trace("opened")
    try:
        yield "ready"
    finally:
        {teardown}
def setup(p):
    service = p.service("connection", connection)
    async def use(ctx, args):
        value = await service.get()
        if args.get("input") == "wait":
            trace("waiting")
            try:
                await asyncio.Event().wait()
            finally:
                trace("cancelled")
        return value
    p.command("connect", "Use connection", use)
""")
    return PluginSource.read(str(path)), log


async def invoke(worker, text=""):
    context = Context("test", "main", "/tmp", "idle", 0, 0, "test", 0)
    return await worker.request("invoke", kind="command", name="connect", arguments={"input": text}, context=asdict(context))


async def test_service_is_lazy_reused_and_closed_once(tmp_path):
    revision, log = source(tmp_path)
    worker, _ = await PluginProcess.start(revision)
    assert not log.exists()  # Installation/validation must not start services.
    try:
        assert await invoke(worker) == "ready"
        assert await invoke(worker) == "ready"
        assert log.read_text().splitlines() == ["opened"]
    finally:
        await worker.close()
    await worker.close()
    assert log.read_text().splitlines() == ["opened", "closed"]


async def test_shutdown_joins_resource_users_before_teardown(tmp_path):
    revision, log = source(tmp_path)
    worker, _ = await PluginProcess.start(revision)
    task = asyncio.create_task(invoke(worker, "wait"))
    try:
        async with asyncio.timeout(5):
            while not log.exists() or "waiting" not in log.read_text():
                await asyncio.sleep(0.01)
        await worker.close()
        with pytest.raises(PluginError, match="CancelledError"):
            await task
        assert log.read_text().splitlines() == ["opened", "waiting", "cancelled", "closed"]
    finally:
        await worker.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_hung_teardown_cannot_strand_worker(tmp_path):
    revision, _ = source(tmp_path, teardown="await asyncio.Event().wait()")
    worker, _ = await PluginProcess.start(revision)
    try:
        await invoke(worker)
        async with asyncio.timeout(4):
            await worker.close()
        assert worker.process.returncode is not None
    finally:
        await worker.close()


async def test_reload_uses_new_service_scope_and_retires_old_connection(tmp_path):
    revision, log = source(tmp_path)
    runtime = PluginRuntime(lambda: Context("test", "main", str(tmp_path), "idle", 0, 0, "test", 0))
    try:
        await runtime.manage("enable", revision.path)
        await runtime.invoke(revision.name, "command", "connect", {})
        await runtime.manage("reload", revision.name)
        await runtime.invoke(revision.name, "command", "connect", {})
    finally:
        await runtime.close()
    events = log.read_text().splitlines()
    assert events.count("opened") == events.count("closed") == 2


async def test_samples_cannot_start_service(tmp_path):
    revision, log = source(tmp_path)
    text = revision.text.replace(
        'p.command("connect", "Use connection", use)', 'async def sample(event):\n        await service.get()\n    p.on("sample", sample)'
    )
    worker, _ = await PluginProcess.start(PluginSource.read(revision.path, text))
    try:
        with pytest.raises(PluginError, match="explicit action"):
            await worker.request("snapshot", context=asdict(Context("test", "main", str(tmp_path), "idle", 0, 0, "test", 0)))
        assert not log.exists()
    finally:
        await worker.close()
