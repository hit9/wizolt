"""Lifecycle boundaries through the public manager and actual plugin registrations."""

import sys
import zipfile

import pytest
from agent_harness import session_with_provider

from wizolt.plugins.session import SessionPlugins
from wizolt.sdk import PluginError


async def test_disable_pending_install_does_not_activate_at_turn_end(tmp_path):
    runtime = SessionPlugins(session_with_provider(tmp_path))
    path = tmp_path / "pending.py"
    path.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.field("value", lambda ctx: 1)\n')
    await runtime.start_turn()
    await runtime.manage("enable", str(path))
    await runtime.manage("disable", "pending")
    await runtime.finish_turn()
    assert runtime.fields() == {}
    assert (await runtime.manage("inspect", "pending"))["plugins"][0]["status"] == "disabled"
    await runtime.close()


async def test_reload_and_disable_retire_workers_without_importing_plugin_into_host(tmp_path):
    runtime = SessionPlugins(session_with_provider(tmp_path))
    path = tmp_path / "identity.py"
    path.write_text(
        'from dataclasses import dataclass\nSDK_VERSION = 1\n@dataclass\nclass State:\n    n: int = 1\ndef setup(p):\n    p.field("n", lambda ctx: State().n)\n'
    )
    before = set(sys.modules)
    await runtime.manage("enable", str(path))
    workers = [runtime.entries["identity"].active.worker]
    for _ in range(10):
        await runtime.manage("reload", "identity")
        workers.append(runtime.entries["identity"].active.worker)
    assert not any(name.startswith("_wizolt_plugin_identity_") for name in set(sys.modules) - before)
    assert runtime.fields()["plugins.identity.n"] == 1
    await runtime.manage("disable", "identity")
    assert not any(name.startswith("_wizolt_plugin_identity_") for name in set(sys.modules) - before)
    await runtime.close()
    assert all(worker.process.returncode is not None for worker in workers)
    with pytest.raises(PluginError, match="closed"):
        await runtime.manage("enable", str(path))


async def test_tool_arguments_are_validated_before_plugin_code(tmp_path):
    runtime = SessionPlugins(session_with_provider(tmp_path))
    path = tmp_path / "echo.py"
    path.write_text("""SDK_VERSION = 1
def setup(p):
    async def echo(ctx, args):
        return str(args["n"] * 2)
    p.tool("double", "Double", {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}, echo)
""")
    await runtime.manage("enable", str(path))
    assert await runtime.invoke("echo", "tool", "double", {"n": 3}) == "6"
    with pytest.raises(PluginError, match="Invalid arguments"):
        await runtime.invoke("echo", "tool", "double", {"n": "3"})
    await runtime.close()


async def test_dependency_install_uses_new_environment_and_preserves_host(tmp_path, monkeypatch):
    """A real offline wheel exercises uv, validation and hot reload without a network fixture."""
    from wizolt.plugins.installation import PluginInstallations
    from wizolt.plugins.settings import PluginSettings

    monkeypatch.setenv("UV_OFFLINE", "1")
    wheel = tmp_path / "wizolt_plugin_testdep-1.0-py3-none-any.whl"
    metadata = "wizolt_plugin_testdep-1.0.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("wizolt_plugin_testdep.py", "VALUE = 42\n")
        archive.writestr(f"{metadata}/METADATA", "Metadata-Version: 2.1\nName: wizolt-plugin-testdep\nVersion: 1.0\n")
        archive.writestr(f"{metadata}/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{metadata}/RECORD", "")
    path = tmp_path / "dependent.py"
    path.write_text(f"""SDK_VERSION = 1
DEPENDENCIES = [{("wizolt-plugin-testdep @ " + wheel.as_uri())!r}]
import wizolt_plugin_testdep
def setup(p):
    p.configure({{"type": "object", "required": ["offset"]}})
    p.field("value", lambda ctx: wizolt_plugin_testdep.VALUE + p.config["offset"])
""")
    runtime = SessionPlugins(session_with_provider(tmp_path))
    runtime.session.config.plugins["dependent"] = {"offset": 1}
    old_path = list(sys.path)
    result = await PluginInstallations(runtime.catalog, runtime.session.cwd, PluginSettings(runtime.session.config.plugins)).manage("install", str(path))
    assert result["status"] == "saved" and result["python"]
    assert runtime.fields() == {} and sys.path == old_path
    assert "wizolt_plugin_testdep" not in sys.modules
    await runtime.hot_reload("dependent")
    assert runtime.fields() == {"plugins.dependent.value": 43}
    await runtime.close()


async def test_builtin_pet_is_disabled_persists_choice_and_uses_semantic_colors(tmp_path):
    from wizolt.ui.cli.plugins import PluginView
    from wizolt.ui.render import Theme

    runtime = SessionPlugins(session_with_provider(tmp_path))
    pet = (await runtime.manage("inspect", "pet"))["plugins"][0]
    assert pet["builtin"] and pet["installed"] and not pet["enabled"]
    assert runtime.panels("above_input") == []
    await runtime.manage("enable", "pet")
    runtime.session.state.last_turn_status = "running"
    await runtime.refresh()
    panels = runtime.panels("above_input")
    assert panels and all(row.role == "accent" for row in panels[0].rows)
    rendered = PluginView.render(panels, 80, 6)
    assert any(style == Theme.fg("accent") for style, text in rendered if text.strip())
    runtime.session.state.awaiting_input = True
    await runtime.refresh()
    assert all(row.role == "warning" for row in runtime.panels("above_input")[0].rows)
    await runtime.manage("disable", "pet")
    await runtime.close()
    fresh = SessionPlugins(session_with_provider(tmp_path))
    assert not (await fresh.manage("inspect", "pet"))["plugins"][0]["enabled"]
    assert fresh.panels("above_input") == []
    await fresh.close()


async def test_hot_reload_reconciles_saved_choices_only_in_calling_agent(tmp_path):
    from wizolt.plugins.installation import PluginInstallations

    runtime = SessionPlugins(session_with_provider(tmp_path))
    sibling = SessionPlugins(runtime.session)
    choices = PluginInstallations(runtime.catalog, runtime.session.cwd)
    path = tmp_path / "counter.py"
    path.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.field("count", lambda ctx: 1)\n')
    await choices.manage("enable", str(path))
    assert not runtime.fields(), "saving preferences is not live activation"
    await runtime.load()
    await sibling.load()
    try:
        await runtime.start_turn()
        path.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.field("count", lambda ctx: 2)\n')
        result = await runtime.hot_reload("counter")
        assert result["plugins"][0]["status"] == "pending"
        assert runtime.fields()["plugins.counter.count"] == 1
        await runtime.finish_turn()
        assert runtime.fields()["plugins.counter.count"] == 2
        assert sibling.fields()["plugins.counter.count"] == 1
        path.write_text('SDK_VERSION = 1\ndef setup(p):\n    raise RuntimeError("broken")\n')
        result = await runtime.hot_reload("counter")
        assert result["plugins"][0]["previous_retained"]
        assert runtime.fields()["plugins.counter.count"] == 2
        await choices.manage("disable", "counter")
        await runtime.hot_reload()
        assert not runtime.fields() and sibling.fields()["plugins.counter.count"] == 1
    finally:
        await runtime.close()
        await sibling.close()


async def test_native_plugin_commands_share_completion_dispatch_and_conflict_checks(tmp_path):
    from prompt_toolkit.document import Document

    from wizolt.agent.engine import Agent
    from wizolt.ui.cli.loop import CommandLoop

    session = session_with_provider(tmp_path)
    session.plugins = SessionPlugins(session)
    output = []
    loop = CommandLoop(Agent(session), input_fn=lambda *args: "", output_fn=output.append)
    path = tmp_path / "command.py"
    path.write_text("""SDK_VERSION = 1
def setup(p):
    async def echo(ctx, args):
        return "received: " + args["input"]
    p.command("my-command", "Custom echo", echo)
""")
    await session.plugins.manage("enable", str(path))
    try:
        suggestions = list(loop.input_completer.get_completions(Document("/my-"), None))
        assert suggestions[0].text == "/my-command"
        assert suggestions[0].display_meta_text == "Custom echo"
        assert await loop.command("/my-command hello") == (True, False)
        assert output[-1].endswith("received: hello")
        path.write_text(path.read_text().replace('"my-command"', '"exit"'))
        with pytest.raises(PluginError, match="already registered"):
            await session.plugins.manage("reload", "command")
        assert loop.commands.get("/my-command") is not None
        await session.plugins.manage("disable", "command")
        assert loop.commands.get("/my-command") is None
    finally:
        await session.plugins.close()
