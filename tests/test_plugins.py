"""Public plugin contracts and the host boundaries that keep DIY recoverable."""

import asyncio
import re
from dataclasses import replace
from pathlib import Path

import pytest

from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, Panel, Plugin, PluginError, Text
from wizolt.ui.bars import Template
from wizolt.ui.cli.plugins import PluginView


@pytest.fixture
def context():
    return Context("root", "main", "/tmp", "idle", 21, 0, "model", 1)


@pytest.fixture
async def runtime(context):
    instance = PluginRuntime(lambda: context)
    yield instance
    await instance.close()


def source(path, value):
    path.write_text(f'SDK_VERSION = 1\ndef setup(plugin):\n    plugin.field("value", lambda ctx: {value!r})\n')
    return str(path)


def example(tmp_path, name):
    """Run the code we actually teach, rather than keeping a second example implementation."""
    document = Path(__file__).parents[1] / "wizolt/skill/builtin/plugin-workshop/SDK.md"
    section = document.read_text().split(f"## Example: {name}.py\n", 1)[1]
    code = re.search(r"```python\n(.*?)```", section, re.DOTALL).group(1)
    path = tmp_path / f"{name}.py"
    path.write_text(code)
    return str(path)


async def test_validate_does_not_activate_and_bad_reload_preserves_version(runtime, tmp_path):
    from wizolt.plugins.testing import PluginTrial

    path = tmp_path / "counter.py"
    result = await PluginTrial(runtime.context()).run(source(path, 3), validate=True)
    assert result.status == "passed" and not runtime.fields()
    active = await runtime.manage("enable", str(path))
    path.write_text("syntax error !!!")
    with pytest.raises(SyntaxError):
        await runtime.manage("reload", "counter")
    assert runtime.fields() == {"plugins.counter.value": 3}
    assert (await runtime.manage("inspect", "counter"))["plugins"][0]["version"] == active["version"]


async def test_pending_enable_reload_disable_and_source_rollback(runtime, tmp_path):
    path = tmp_path / "counter.py"
    await runtime.start_turn()
    assert (await runtime.manage("enable", source(path, 1)))["status"] == "pending"
    assert not runtime.fields()
    await runtime.finish_turn()
    assert runtime.fields()["plugins.counter.value"] == 1
    await runtime.start_turn()
    source(path, 2)
    assert (await runtime.manage("reload", "counter"))["status"] == "pending"
    assert runtime.fields()["plugins.counter.value"] == 1
    await runtime.finish_turn()
    assert runtime.fields()["plugins.counter.value"] == 2
    await runtime.manage("rollback", "counter")
    assert runtime.fields()["plugins.counter.value"] == 1
    await runtime.start_turn()
    assert (await runtime.manage("disable", "counter"))["status"] == "pending"
    await runtime.finish_turn()
    assert not runtime.fields()


async def test_instances_isolate_callback_state(runtime, context, tmp_path):
    path = tmp_path / "counter.py"
    path.write_text('''SDK_VERSION = 1
def setup(plugin):
    state = {"count": 0}
    async def done(event):
        state["count"] += 1
    plugin.on("turn.finished", done)
    plugin.field("value", lambda ctx: state["count"])
''')
    child = PluginRuntime(lambda: replace(context, agent_id="child", agent_name="child"))
    await runtime.manage("enable", str(path))
    await child.manage("enable", str(path))
    await runtime.start_turn()
    await runtime.finish_turn()
    assert runtime.fields()["plugins.counter.value"] == 1
    assert child.fields()["plugins.counter.value"] == 0
    await child.close()


async def test_invocation_pins_generation_and_cancellation_releases_it(runtime, tmp_path):
    path = tmp_path / "slow.py"
    path.write_text('''import asyncio
from pathlib import Path
SDK_VERSION = 1
def setup(plugin):
    async def wait(ctx, args):
        Path(args["started"]).touch()
        await asyncio.Event().wait()
    plugin.command("wait", "Wait", wait)
''')
    await runtime.manage("enable", str(path))
    started = tmp_path / "started"
    task = asyncio.create_task(runtime.invoke("slow", "command", "wait", {"started": str(started)}))
    async with asyncio.timeout(5):
        while not started.exists():
            await asyncio.sleep(.01)
    assert (await runtime.manage("disable", "slow"))["status"] == "pending"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runtime.entries and not runtime.busy


async def test_cooperative_cancellation_keeps_the_generation_usable(runtime, tmp_path):
    path = tmp_path / "polite.py"
    path.write_text('''import asyncio
from pathlib import Path
SDK_VERSION = 1
def setup(plugin):
    async def wait(ctx, args):
        Path(args["started"]).touch()
        await asyncio.Event().wait()
    plugin.command("wait", "Wait", wait)
    plugin.field("value", lambda ctx: 7)
''')
    await runtime.manage("enable", str(path))
    worker = runtime.entries["polite"].active.worker
    reports = []
    asyncio.get_running_loop().set_exception_handler(lambda _, context: reports.append(context["message"]))
    started = tmp_path / "started"
    task = asyncio.create_task(runtime.invoke("polite", "command", "wait", {"started": str(started)}))
    async with asyncio.timeout(5):
        while not started.exists():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The callback unwound when asked, so the worker survives; only a blocked loop is killed.
    assert worker.process.returncode is None and not worker.error
    await runtime.refresh()
    assert runtime.fields() == {"plugins.polite.value": 7} and runtime.active_count == 1
    assert not reports


async def test_status_reads_do_not_resize_prompt_components(runtime, tmp_path):
    path = tmp_path / "wide.py"
    path.write_text('from wizolt.sdk import Panel, Text\nSDK_VERSION = 1\ndef setup(p):\n    p.field("columns", lambda ctx: ctx.columns)\n')
    await runtime.manage("enable", str(path))
    runtime.panels("above_input", 160)  # The live prompt projection reports its width.
    runtime.panels("status")  # /status reads its panel without one.
    await runtime.refresh()
    assert runtime.fields()["plugins.wide.columns"] == 160


async def test_broken_component_is_reported_without_breaking_other_plugins(runtime, tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.component("above_input", lambda ctx: 1/0)\n')
    # Activation rejects a broken initial projection. Later failures remain isolated as well.
    with pytest.raises(PluginError, match="division"):
        await runtime.manage("enable", str(bad))
    bad.write_text('''from wizolt.sdk import Panel
from pathlib import Path
SDK_VERSION = 1
def draw(ctx):
    if Path(__file__ + ".fail").exists():
        return 1 / 0
    return Panel()
def setup(p):
    p.component("above_input", draw)
''')
    await runtime.manage("enable", str(bad))
    await runtime.manage("enable", example(tmp_path, "pet"))
    Path(str(bad) + ".fail").touch()
    await runtime.refresh()
    assert len(runtime.panels("above_input")) == 1
    assert "division" in (await runtime.manage("inspect", "bad"))["plugins"][0]["error"]
    calls = runtime.entries["bad"].active.calls
    runtime.panels("above_input")
    assert runtime.entries["bad"].active.calls == calls


async def test_examples_compose_and_fields_use_existing_template(runtime, tmp_path):
    for name in ("pet", "tokenweather"):
        await runtime.manage("enable", example(tmp_path, name))
    assert len(runtime.panels("above_input")) == 2
    template = Template("{% if plugins.tokenweather.weather %}{plugins.tokenweather.weather}{% endif %}")
    assert "".join(text for _, text in template.render(runtime.fields(), 30, {})).strip() == "clear"
    await runtime.manage("disable", "tokenweather")
    assert "".join(text for _, text in template.render(runtime.fields(), 30, {})).strip() == ""


@pytest.mark.parametrize("width", [0, 1, 10, 80])
def test_panel_projection_clips_cells_rows_and_control_characters(width):
    from prompt_toolkit.utils import get_cwidth

    panels = [Panel((Text("猫" * 80, "unknown"), Text("\x1b[31mtest\nnext"), Text("hidden")))]
    fragments = PluginView.render(panels, width, 2)
    lines = "".join(part for _, part in fragments).splitlines()
    assert len(lines) <= 2
    assert all(get_cwidth(line) <= width for line in lines)
    assert "\x1b" not in "".join(lines) and "hidden" not in "".join(lines)


def test_registration_rejects_invalid_names_duplicates_and_unknown_events():
    plugin = Plugin("example")
    plugin.field("count", lambda ctx: 1)
    with pytest.raises(PluginError, match="Duplicate"):
        plugin.field("count", lambda ctx: 2)
    with pytest.raises(PluginError, match="identifier"):
        plugin.field("bad.name", lambda ctx: 2)
    with pytest.raises(PluginError, match="Unknown event"):
        plugin.on("made.up", None)


async def test_safe_mode_skips_imports_and_installation_survives_new_agent(tmp_path, monkeypatch):
    from agent_harness import session_with_provider

    from wizolt.plugins.session import SessionPlugins

    session = session_with_provider(tmp_path)
    path = tmp_path / "local.py"
    plugins = SessionPlugins(session)
    await plugins.manage("enable", source(path, 42))
    second = SessionPlugins(session)
    await second.load()
    assert second.fields()["plugins.local.value"] == 42
    monkeypatch.setenv("WIZOLT_NO_PLUGINS", "1")
    recovery = SessionPlugins(session)
    await recovery.load()
    assert not recovery.fields()
    await plugins.close()
    await second.close()
    await recovery.close()


async def test_management_tool_uses_existing_runner_approval_contract(tmp_path):
    from agent_harness import call, session_with_provider

    from wizolt.agent.context import ContextManager
    from wizolt.agent.lifecycle import bootstrap_features
    from wizolt.agent.runner import ToolRunner
    from wizolt.plugins.installation import PluginInstallations
    from wizolt.tools.plugin import PluginHotReload

    session = session_with_provider(tmp_path)
    bootstrap_features(session)
    assert PluginHotReload(session, [{}]).needs_confirmation()
    await PluginInstallations(session.plugins.catalog, session.cwd).manage("enable", source(tmp_path / "local.py", 5))
    session.settings.yolo = True
    # Through the runner, not tool.call(): a mutating tool's async call must be awaited there.
    runner = ToolRunner(session, ContextManager(session), output_fn=lambda _: None)
    [message] = await runner.run([call("PluginHotReload", [{"name": "local"}])])
    assert '"status": "active"' in str(message["content"])
    assert session.plugins.fields() == {"plugins.local.value": 5}
    await session.plugins.close()


def test_builtin_skill_is_discoverable(tmp_path):
    from agent_harness import session_with_provider

    from wizolt.skill.library import SkillLibrary

    library = SkillLibrary.load(session_with_provider(tmp_path))
    skill = library.get("plugin-workshop")
    assert skill and skill.source == "builtin"
    assert "plugin-workshop" in library.index()
    assert (Path(skill.dir) / "SDK.md").exists()
