"""Lifecycle boundaries through the public manager and actual plugin registrations."""

import sys
import zipfile

import pytest
from agent_harness import session_with_provider

from wizolt.plugins.session import SessionPlugins
from wizolt.sdk import PluginError


@pytest.mark.parametrize("action", ["enable", "disable"])
@pytest.mark.parametrize("busy", [False, True])
async def test_failed_preference_write_preserves_active_and_pending_generations(tmp_path, monkeypatch, action, busy):
    runtime = SessionPlugins(session_with_provider(tmp_path))
    source = tmp_path / "stable.py"
    source.write_text('SDK_VERSION = 1\ndef setup(p): p.field("value", lambda ctx: 1)\n')
    try:
        await runtime.manage("enable", str(source))
        worker = runtime.entries["stable"].active.worker
        if busy:
            await runtime.start_turn()
        source.write_text('SDK_VERSION = 1\ndef setup(p): p.field("value", lambda ctx: 2)\n')

        def fail(_item):
            raise OSError("read-only config")

        monkeypatch.setattr(runtime.catalog, "save", fail)
        with pytest.raises(OSError, match="read-only config"):
            await runtime.manage(action, "stable")
        if busy:
            await runtime.finish_turn()
        assert runtime.entries["stable"].active.worker is worker
        assert runtime.entries["stable"].pending is None
        assert not runtime.entries["stable"].disabling
        assert runtime.fields()["plugins.stable.value"] == 1
        assert worker.process.returncode is None
    finally:
        await runtime.close()


@pytest.mark.parametrize("action", ["enable", "disable"])
async def test_failed_write_preserves_admission_of_new_plugins(tmp_path, monkeypatch, action):
    runtime = SessionPlugins(session_with_provider(tmp_path))
    source = tmp_path / "candidate.py"
    source.write_text('SDK_VERSION = 1\ndef setup(p): p.field("value", lambda ctx: 1)\n')
    try:
        await runtime.start_turn()
        if action == "disable":
            await runtime.manage("enable", str(source))

        def fail(_item):
            raise OSError("read-only config")

        monkeypatch.setattr(runtime.catalog, "save", fail)
        with pytest.raises(OSError, match="read-only config"):
            await runtime.manage(action, str(source) if action == "enable" else "candidate")
        await runtime.finish_turn()
        assert ("candidate" in runtime.entries) == (action == "disable")
    finally:
        await runtime.close()


@pytest.mark.parametrize("invalid", ["record", "table", "syntax"])
async def test_invalid_builtin_preference_does_not_disable_live_generation(tmp_path, invalid):
    runtime = SessionPlugins(session_with_provider(tmp_path))
    try:
        await runtime.manage("enable", "pet")
        worker = runtime.entries["pet"].active.worker
        preferences = runtime.catalog.preferences
        if invalid == "record":
            preferences.save("installations", "pet", {"enabled": "invalid"})
        else:
            preferences.path.write_text('[plugin_manager]\ninstallations = "invalid"\n' if invalid == "table" else "[broken")
        report = await runtime.hot_reload()
        assert report["problems"]
        assert runtime.entries["pet"].active.worker is worker
        assert worker.process.returncode is None
        preferences.path.write_text("")
        preferences.save("installations", "pet", {"enabled": True})
        assert not (await runtime.hot_reload())["problems"]
        assert runtime.entries["pet"].active.worker is not worker
    finally:
        await runtime.close()


@pytest.mark.parametrize("requirement", ["--python=/tmp/another-environment", "--target=/tmp/site-packages", "--requirements=/tmp/list.txt"])
async def test_dependency_metadata_cannot_be_interpreted_as_installer_options(tmp_path, monkeypatch, requirement):
    from wizolt.plugins.dependencies import DependencyEnvironment
    from wizolt.plugins.loading import PluginSource

    source = tmp_path / "dependency.py"
    source.write_text(f"SDK_VERSION = 1\nDEPENDENCIES = [{requirement!r}]\ndef setup(p): pass\n")
    environment = DependencyEnvironment(tmp_path / "environments", [PluginSource.read(str(source))], str(tmp_path))

    async def command(_arguments):
        pytest.fail("Invalid metadata reached the package installer")

    monkeypatch.setattr(environment, "command", command)
    with pytest.raises(PluginError, match="requirement"):
        await environment.prepare()
    assert not environment.path.exists()


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
    p.tool("double", "Double", {"type": "object", "$defs": {"number": {"type": "integer"}}, "properties": {"n": {"$ref": "#/$defs/number"}}, "required": ["n"]}, echo)
""")
    await runtime.manage("enable", str(path))
    assert await runtime.invoke("echo", "tool", "double", {"n": 3}) == "6"
    with pytest.raises(PluginError, match="Invalid arguments"):
        await runtime.invoke("echo", "tool", "double", {"n": "3"})
    await runtime.close()


@pytest.mark.parametrize("reference", ["$ref", "$dynamicRef"])
async def test_tool_schema_validation_never_fetches_remote_references(tmp_path, monkeypatch, reference):
    import io
    import urllib.request

    fetched = []

    def remote(request):
        fetched.append(request.full_url)
        return io.BytesIO(b'{"type": "integer"}')

    monkeypatch.setattr(urllib.request, "urlopen", remote)
    marker = tmp_path / "worker-fetched"
    path = tmp_path / "external.py"
    path.write_text(f'''import io
import urllib.request
from pathlib import Path
SDK_VERSION = 1
def remote(request):
    Path({str(marker)!r}).touch()
    return io.BytesIO(b'{{"type": "integer"}}')
urllib.request.urlopen = remote
def setup(p):
    async def act(ctx, args):
        return "should not execute"
    p.tool("act", "Act", {{"type": "object", "properties": {{"n": {{{reference!r}: "https://example.invalid/schema"}}}}}}, act)
''')
    runtime = SessionPlugins(session_with_provider(tmp_path))
    try:
        await runtime.manage("enable", str(path))
        with pytest.raises(PluginError, match="Unresolvable"):
            await runtime.invoke("external", "tool", "act", {"n": 3})
        assert fetched == []
        assert not marker.exists()
    finally:
        await runtime.close()


def test_expensive_tool_schema_is_bounded_by_worker_deadline(tmp_path):
    """Use an outer process deadline: the regression blocks the host event loop itself."""
    import subprocess
    import textwrap

    path = tmp_path / "slow_schema.py"
    path.write_text('''SDK_VERSION = 1
def setup(p):
    async def act(ctx, args):
        return "unreachable"
    p.tool("act", "Act", {"type": "object", "properties": {"text": {"type": "string", "pattern": "^(a+)+$"}}}, act)
''')
    script = textwrap.dedent('''
        import asyncio
        import sys
        from wizolt.plugins.runtime import PluginRuntime
        from wizolt.sdk import Context, PluginError

        async def main():
            runtime = PluginRuntime(lambda: Context("test", "main", sys.argv[2], "idle", 0, 0, "test", 0))
            runtime.ACTION_TIMEOUT = 0.1
            try:
                await runtime.manage("enable", sys.argv[1])
                try:
                    await runtime.invoke("slow_schema", "tool", "act", {"text": "a" * 40 + "!"})
                except PluginError as error:
                    assert "timed out" in str(error), str(error)
                else:
                    raise AssertionError("expected worker deadline")
            finally:
                await runtime.close()
        asyncio.run(main())
    ''')
    result = subprocess.run([sys.executable, "-P", "-c", script, str(path), str(tmp_path)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


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
    # A later installation can change interpreters. Rollback must retain the original
    # dependency environment along with source/settings, not use the latest preference.
    from wizolt.plugins.catalog import Installation

    path.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.field("value", lambda ctx: 99)\n')
    runtime.catalog.save(Installation("dependent", str(path)))
    await runtime.hot_reload("dependent")
    assert runtime.fields() == {"plugins.dependent.value": 99}
    await runtime.manage("rollback", "dependent")
    assert runtime.fields() == {"plugins.dependent.value": 43}
    await runtime.manage("rollback", "dependent")
    assert runtime.fields() == {"plugins.dependent.value": 99}
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

    def outline(panel):  # The mood colors the ears and the body's first span.
        ears, body = panel.rows
        return {ears.role, body.spans[0].role}

    assert panels and outline(panels[0]) == {"accent"}
    rendered = PluginView.render(panels, 80, 6)
    assert any(style == Theme.fg("accent") for style, text in rendered if text.strip())
    runtime.session.state.awaiting_input = True
    await runtime.refresh()
    assert outline(runtime.panels("above_input")[0]) == {"warning"}
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


async def test_failed_live_reload_reports_where_the_plugin_failed(tmp_path):
    from wizolt.plugins.installation import PluginInstallations

    runtime = SessionPlugins(session_with_provider(tmp_path))
    path = tmp_path / "typo.py"
    path.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.field("width", lambda ctx: ctx.colums)\n')
    await PluginInstallations(runtime.catalog, runtime.session.cwd).manage("enable", str(path))
    try:
        # A sample failure: the model needs the file and line, not only the exception text.
        [result] = (await runtime.hot_reload("typo"))["plugins"]
        assert result["status"] == "failed" and "colums" in result["error"]
        assert f'File "{path}", line 3' in result["traceback"]
        # A setup failure, with what the plugin printed before it.
        path.write_text('SDK_VERSION = 1\ndef setup(p):\n    print("configuring")\n    raise RuntimeError("bad setup")\n')
        [result] = (await runtime.hot_reload("typo"))["plugins"]
        assert "bad setup" in result["traceback"] and "configuring" in result["log"]
    finally:
        await runtime.close()


def test_plugin_cli_defaults_to_the_calling_agents_config_and_project(tmp_path, monkeypatch, capsys):
    import json

    from wizolt.plugins.catalog import PluginCatalog
    from wizolt.ui.cli.plugin_commands import main

    config = tmp_path / "agent.toml"
    config.write_text(f'[paths]\ndata_dir = "{tmp_path / "data"}"\n')
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # The agent has cd'ed away from its project.
    monkeypatch.setenv("WIZOLT_CONFIG", str(config))
    monkeypatch.setenv("WIZOLT_SESSION_CWD", str(project))
    assert main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["runtime_directory"] == str(PluginCatalog.for_user(str(tmp_path / "data")).directory)


async def test_errored_generation_inspection_includes_its_traceback(tmp_path):
    runtime = SessionPlugins(session_with_provider(tmp_path))
    path = tmp_path / "later.py"
    flag = tmp_path / "fail"
    path.write_text(f'from pathlib import Path\nSDK_VERSION = 1\ndef value(ctx):\n    if Path({str(flag)!r}).exists():\n        raise KeyError("missing")\n    return 1\ndef setup(p):\n    p.field("value", value)\n')
    try:
        await runtime.manage("enable", str(path))
        assert "traceback" not in (await runtime.manage("inspect", "later"))["plugins"][0]
        flag.touch()
        await runtime.refresh()
        item = (await runtime.manage("inspect", "later"))["plugins"][0]
        assert "missing" in item["error"] and "line 5" in item["traceback"]
    finally:
        await runtime.close()


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


async def test_user_plugins_and_layout_follow_cwd_but_live_state_stays_isolated(tmp_path):
    from wizolt.plugins.installation import PluginInstallations
    from wizolt.plugins.workspace import PluginWorkspace

    first = session_with_provider(tmp_path)
    second = session_with_provider(tmp_path)
    (tmp_path / "another-project").mkdir()
    second.cwd = str(tmp_path / "another-project")
    other = SessionPlugins(second)
    source = tmp_path / "shared.py"
    source.write_text('''from wizolt.sdk import Panel, Text
SDK_VERSION = 1
def setup(p):
    p.field("cwd", lambda ctx: ctx.cwd)
    p.component("above_input", lambda ctx: Panel((Text(ctx.cwd),)))
''')
    config = first.plugins.catalog.preferences.path
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[paths]\ndata_dir = "{first.config.data_dir}"\n')
    workspace = PluginWorkspace.open(str(config), second.cwd)
    try:
        await first.plugins.manage("enable", str(source))
        await first.plugins.call_host("ui.components.set_gap", {"component": "shared.above_input", "gap_before": 2})
        listed = await PluginInstallations(workspace.catalog, workspace.cwd).manage("list")
        assert next(item for item in listed["plugins"] if item["name"] == "shared")["enabled"]
        assert workspace.catalog.directory == first.plugins.catalog.directory == other.catalog.directory
        await other.load()
        assert first.plugins.fields()["plugins.shared.cwd"] == first.cwd
        assert other.fields()["plugins.shared.cwd"] == second.cwd
        assert other.components()[0].gap_before == 2
        assert first.plugins.entries["shared"].active.worker is not other.entries["shared"].active.worker
        await other.manage("disable", "shared")
        assert "shared" in first.plugins.entries  # Preferences never silently mutate another live agent.
        await first.plugins.hot_reload()
        assert "shared" not in first.plugins.entries
    finally:
        await first.plugins.close()
        await other.close()
        first.close()
        second.close()


def test_user_catalog_resolves_data_directory_symlinks_without_project_storage(tmp_path):
    from wizolt.plugins.catalog import Installation, PluginCatalog

    data = tmp_path / "profile"
    data.mkdir()
    link = tmp_path / "profile-link"
    link.symlink_to(data, target_is_directory=True)
    catalog = PluginCatalog.for_user(str(link))
    catalog.save(Installation("example", str(tmp_path / "example.py")))
    assert catalog.directory == data / "plugins" / ".state"
    assert "example" in PluginCatalog.for_user(str(data)).read()[0]
    assert not (data / "projects").exists()
