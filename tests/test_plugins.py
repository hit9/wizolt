"""Public plugin contracts and the host boundaries that keep DIY recoverable."""

import asyncio
import re
from dataclasses import replace
from pathlib import Path

import pytest

from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, Line, Panel, Plugin, PluginError, Text
from wizolt.ui.bars import Template
from wizolt.ui.cli.plugins import PluginView
from wizolt.ui.render import Theme


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


async def test_pending_enable_reload_and_disable(runtime, tmp_path):
    path = tmp_path / "counter.py"
    await runtime.start_turn()
    assert (await runtime.manage("enable", source(path, 1)))["status"] == "starting"
    assert not runtime.fields()
    await runtime.finish_turn()
    assert runtime.fields()["plugins.counter.value"] == 1
    await runtime.start_turn()
    source(path, 2)
    assert (await runtime.manage("reload", "counter"))["status"] == "reloading"
    assert runtime.fields()["plugins.counter.value"] == 1
    await runtime.finish_turn()
    assert runtime.fields()["plugins.counter.value"] == 2
    with pytest.raises(PluginError, match="Unknown plugin action"):  # Source history is git's job.
        await runtime.manage("rollback", "counter")
    await runtime.start_turn()
    assert (await runtime.manage("disable", "counter"))["status"] == "stopping"
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
    await runtime.refresh()  # Turns only request presentation; take that pass here.
    await child.refresh()
    assert runtime.fields()["plugins.counter.value"] == 1
    assert child.fields()["plugins.counter.value"] == 0
    await child.close()


async def test_slow_fields_do_not_delay_turns_or_commands(runtime, tmp_path):
    import time

    path = tmp_path / "slow_field.py"
    path.write_text('''import time
SDK_VERSION = 1
def setup(plugin):
    def slow(ctx):
        time.sleep(0.6)
        return 1
    async def ping(ctx, args):
        return "pong"
    plugin.field("slow", slow)
    plugin.command("ping", "Ping", ping)
''')
    await runtime.manage("enable", str(path))
    started = time.monotonic()
    await runtime.start_turn()
    await runtime.finish_turn()
    assert await runtime.invoke("slow_field", "command", "ping", {}) == "pong"
    # Each used to wait for a layout pass, and for the one in flight: about 1.2 s apiece.
    assert time.monotonic() - started < 0.9


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
    assert (await runtime.manage("disable", "slow"))["status"] == "stopping"
    # A stopping plugin drains; new work would renew its lease and keep it alive.
    with pytest.raises(PluginError, match="being disabled"):
        await runtime.invoke("slow", "command", "wait", {"started": str(started)})
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runtime.entries


@pytest.mark.parametrize("during_turn", [False, True])
async def test_busy_plugin_does_not_pin_unrelated_generations(runtime, tmp_path, during_turn):
    slow = tmp_path / "slow.py"
    slow.write_text('''import asyncio
from pathlib import Path
SDK_VERSION = 1
def setup(plugin):
    async def wait(ctx, args):
        Path(args["started"]).touch()
        await asyncio.Event().wait()
    plugin.command("wait", "Wait", wait)
''')
    other = tmp_path / "other.py"
    await runtime.manage("enable", str(slow))
    await runtime.manage("enable", source(other, 1))
    marker = tmp_path / "started"
    task = asyncio.create_task(runtime.invoke("slow", "command", "wait", {"started": str(marker)}))
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(.01)
        if during_turn:
            await runtime.start_turn()
        source(other, 2)
        result = await runtime.manage("reload", "other")
        assert result["status"] == ("reloading" if during_turn else "running")
        if during_turn:
            assert runtime.fields()["plugins.other.value"] == 1
            await runtime.finish_turn()
        assert runtime.fields()["plugins.other.value"] == 2
        assert not task.done()
        assert (await runtime.manage("disable", "other"))["status"] == "off"
        assert (await runtime.manage("reload", "slow"))["status"] == "reloading"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


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


def test_repeated_panel_projection_tracks_width_content_theme_and_caller_mutation():
    original_theme = Theme.name()
    panel = Panel((Line((Text("猫e\u0301", "accent"), Text("\x1b!", "muted"))), Text("second")))
    try:
        Theme.set_mode("dark")
        first = PluginView.render([panel], 80, 2)
        assert "".join(text for _, text in first) == "猫e\u0301 !\nsecond"
        damaged = PluginView.render([panel], 80, 2)
        damaged.clear()
        assert PluginView.render([panel], 80, 2) == first
        narrow = PluginView.render([panel], 3, 1)
        assert "".join(text for _, text in narrow) == "猫e\u0301"
        Theme.set_mode("forest")
        recolored = PluginView.render([panel], 80, 2)
        assert recolored != first
        assert [text for _, text in recolored] == [text for _, text in first]
        assert recolored[0][0] == Theme.fg("accent")
        changed = PluginView.render([Panel((Text("updated", "accent"),))], 80, 2)
        assert changed == [(Theme.fg("accent"), "updated")]
    finally:
        Theme.set_mode(original_theme)


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
    with pytest.raises(PluginError, match="disabled for this launch"):
        await recovery.hot_reload()
    with pytest.raises(PluginError, match="disabled for this launch"):
        await recovery.manage("enable", "local")
    assert recovery.catalog.read()[0]["local"].enabled
    await recovery.manage("disable", "local")
    assert not recovery.catalog.read()[0]["local"].enabled
    await plugins.close()
    await second.close()
    await recovery.close()


async def test_management_tool_uses_existing_runner_approval_contract(tmp_path):
    from agent_harness import call, session_with_provider

    from wizolt.agent.context import ContextManager
    from wizolt.agent.lifecycle import bootstrap_features
    from wizolt.agent.runner import ToolRunner
    from wizolt.plugins.installation import PluginInstallations
    from wizolt.tools.plugin import PluginTool

    session = session_with_provider(tmp_path)
    bootstrap_features(session)
    assert PluginTool(session, [{"action": "reload"}]).needs_confirmation()
    await PluginInstallations(session.plugins.catalog, session.cwd).manage("enable", source(tmp_path / "local.py", 5))
    session.settings.yolo = True
    # Through the runner, not tool.call(): a mutating tool's async call must be awaited there.
    runner = ToolRunner(session, ContextManager(session), output_fn=lambda _: None)
    [message] = await runner.run([call("Plugin", [{"action": "reload", "name": "local"}])])
    assert '"status": "running"' in str(message["content"])
    assert session.plugins.fields() == {"plugins.local.value": 5}
    await session.plugins.close()


async def test_reload_approval_shows_what_each_plugin_would_change(tmp_path):
    from agent_harness import session_with_provider

    from wizolt.agent.lifecycle import bootstrap_features
    from wizolt.plugins.installation import PluginInstallations
    from wizolt.tools.plugin import PluginTool

    session = session_with_provider(tmp_path)
    bootstrap_features(session)
    plugins = session.plugins
    choices = PluginInstallations(plugins.catalog, session.cwd)
    marker = tmp_path / "setup-ran"

    def write(name, value, *, touch=False):
        effect = f"    open({str(marker)!r}, 'w').close()\n" if touch else ""
        (tmp_path / f"{name}.py").write_text(f'SDK_VERSION = 1\ndef setup(p):\n{effect}    p.field("v", lambda ctx: {value})\n')
        return str(tmp_path / f"{name}.py")

    try:
        for name in ("same", "edited", "tuned", "dropped"):
            await choices.manage("enable", write(name, 1))
        await plugins.load()
        await choices.manage("enable", write("fresh", 1, touch=True))
        marker.unlink()
        write("edited", 2)
        plugins.settings.values["tuned"] = {"speed": 2}
        await choices.manage("disable", "dropped")
        view = PluginTool(session, [{"action": "reload"}]).approval_view()
        assert view is not None and not marker.exists(), "approval must not run plugin code"
        lines = dict(line[2:].split(": ", 1) for line in view.text.splitlines() if line.startswith("- "))
        assert lines == {"edited": "code changed", "tuned": "settings changed in [plugins.tuned]", "dropped": "disable", "fresh": "enable"}
        # Unchanged plugins are one closing line, not noise among the changes; restarting resets state.
        assert view.text.splitlines()[-1] == "Restart without changes, resetting what they keep in memory: same"
        assert ("changes", "4 of 5") in view.rows
        assert PluginTool(session, [{"action": "reload", "name": "edited"}]).approval_view().text.count("\n- ") == 1
        (tmp_path / "edited.py").write_text("SDK_VERSION = 1\nnot python (")
        assert "will fail" in plugins.reload_plan("edited")[0][1]
        assert PluginTool(session, [{"action": "list"}]).approval_view() is None
    finally:
        await plugins.close()


async def test_model_uses_plugin_tools_by_progressive_disclosure(tmp_path):
    from agent_harness import call, session_with_provider

    from wizolt.agent.context import ContextManager
    from wizolt.agent.lifecycle import bootstrap_features
    from wizolt.agent.runner import ToolRunner
    from wizolt.tools import Tool
    from wizolt.tools.plugin import PluginTool

    session = session_with_provider(tmp_path)
    bootstrap_features(session)
    runner = ToolRunner(session, ContextManager(session), output_fn=lambda _: None)

    async def run(**payload):
        [message] = await runner.run([call("Plugin", [payload])])
        return str(message["content"])

    def schemas():
        return [schema for schema in Tool.resolved_schemas(session) if "Plugin" in schema["function"]["name"]]

    path = tmp_path / "notes.py"
    path.write_text("""SDK_VERSION = 1
def setup(p):
    async def remember(ctx, args):
        return "saved " + args["body"]
    p.tool("remember", "Save a note", {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]}, remember)
""")
    try:
        before = schemas()
        await session.plugins.manage("enable", str(path))
        # One fixed schema: enabling a plugin with tools must not reshape the cached tool block.
        assert len(before) == 1 and schemas() == before
        listed = await run(action="list")
        assert '"tool": "remember"' in listed and "parameters" not in listed  # Details wait for describe.
        assert '"required": ["body"]' in await run(action="describe", name="notes", tool="remember")
        assert PluginTool(session, [{"action": "call", "name": "notes", "tool": "remember"}]).needs_confirmation()
        assert not PluginTool(session, [{"action": "list"}]).needs_confirmation()
        session.settings.yolo = True
        assert "saved milk" in await run(action="call", name="notes", tool="remember", arguments={"body": "milk"})
        assert "Invalid arguments" in await run(action="call", name="notes", tool="remember", arguments={"bdy": 1})
        assert "use action=list" in await run(action="call", name="notes", tool="forget")
    finally:
        await session.plugins.close()


def test_builtin_skill_is_discoverable(tmp_path):
    from agent_harness import session_with_provider

    from wizolt.skill.library import SkillLibrary

    library = SkillLibrary.load(session_with_provider(tmp_path))
    skill = library.get("plugin-workshop")
    assert skill and skill.source == "builtin"
    assert "plugin-workshop" in library.index()
    assert (Path(skill.dir) / "SDK.md").exists()
