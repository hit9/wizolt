"""The bundled context bar and guard through real workers: what a user sees and what the agent may run."""

from pathlib import Path
from types import MappingProxyType

import pytest

from wizolt.plugins.catalog import PluginCatalog
from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, ContextWindow, Line, Text
from wizolt.sdk.operations import Refusal, ToolCall, ToolResult

BUILTIN = Path(__file__).parents[1] / "wizolt/plugins/builtin"


def facts(**changes):
    values = {"agent_id": "root", "agent_name": "main", "cwd": "/tmp", "status": "idle", "context_percent": 0, "elapsed": 0, "model": "m", "now": 0, "columns": 60}
    return Context(**{**values, **changes})


@pytest.fixture
async def runtime():
    state = {"context": facts()}
    instance = PluginRuntime(lambda: state["context"])
    instance.state = state  # type: ignore[attr-defined]
    yield instance
    await instance.close()


def text(row):
    return "".join(span.text for span in row.spans) if isinstance(row, Line) else row.text


async def test_every_builtin_introduces_itself_in_the_plugin_list(tmp_path):
    from agent_harness import session_with_provider

    from wizolt.plugins.loading import PluginSource
    from wizolt.plugins.session import SessionPlugins

    for path in BUILTIN.glob("*.py"):
        assert PluginSource.read(str(path)).description, f"{path.stem} needs a docstring whose first paragraph introduces it"
    plugins = SessionPlugins(session_with_provider(tmp_path))
    try:
        # /plugins lists disabled plugins too: their introduction is read from source, not run.
        listing = {item["name"]: item for item in (await plugins.manage("list"))["plugins"]}
        assert listing["guard"]["status"] == "off" and listing["guard"]["description"].startswith("Asks before destructive")
        await plugins.manage("enable", "pet")
        listing = {item["name"]: item for item in (await plugins.manage("list"))["plugins"]}
        assert listing["pet"]["description"].startswith("A small cat")  # Live plugins too.
        from types import SimpleNamespace

        from wizolt.ui.cli.plugins import PluginManager

        manager = PluginManager(SimpleNamespace(), plugins)  # type: ignore[arg-type]
        manager.records = listing
        # The preview opens with what the plugin is, before where it lives.
        assert manager.preview("guard").startswith("Asks before destructive shell commands")
    finally:
        await plugins.close()


def test_new_builtins_ship_disabled(tmp_path):
    records, _ = PluginCatalog.for_user(str(tmp_path)).read()
    assert not records["context_bar"].enabled and not records["guard"].enabled


async def bar_panel(runtime, window, columns=60):
    runtime.state["context"] = facts(window=window)
    runtime.resize(columns, 24)
    if "context_bar" not in runtime.entries:
        await runtime.manage("enable", str(BUILTIN / "context_bar.py"))
    await runtime.refresh()
    panels = runtime.panels("above_input")
    return panels[0] if panels else None


async def test_context_bar_stacks_categories_and_the_unestimated_rest(runtime):
    panel = await bar_panel(runtime, ContextWindow(50_000, 200_000, 180_000, (("system prompt", 10_000), ("messages", 30_000))))
    bar, legend = panel.rows
    assert text(bar).endswith(" 25%") and len(text(bar)) == 60
    # Categories in request order with their theme roles, then "other" for the fill the
    # estimates do not explain, then the free tail.
    assert [span.role for span in bar.spans] == ["status_provider", "info", "muted", "rule", "text"]
    assert sum(len(span.text) for span in bar.spans[:3]) == round(50_000 * 56 / 200_000)
    assert "system prompt 10k" in text(legend) and "other 10k" in text(legend) and text(legend).endswith("50k/200k")


async def test_context_bar_keeps_small_categories_and_fits_narrow_terminals(runtime):
    window = ContextWindow(100_000, 200_000, 0, (("system prompt", 100), ("system tools", 9_900), ("skills", 40_000), ("messages", 50_000)))
    panel = await bar_panel(runtime, window, columns=40)
    bar, legend = panel.rows
    assert len(text(bar)) == 40 and bar.spans[0] == Text("█", "status_provider")  # Tiny, still drawn.
    assert "…" in text(legend) and len(text(legend)) == 40 and text(legend).endswith("100k/200k")
    # Too narrow for the percentage beside the bar: the legend's totals carry it instead.
    bar, legend = (await bar_panel(runtime, window, columns=12)).rows
    assert "%" not in text(bar) and text(legend).strip() == "100k/200k 50%"


async def test_context_bar_warns_near_the_limit_and_hides_without_a_window(runtime):
    assert await bar_panel(runtime, ContextWindow()) is None or not (await bar_panel(runtime, ContextWindow())).rows
    panel = await bar_panel(runtime, ContextWindow(190_000, 200_000))
    [bar, _legend] = panel.rows
    assert bar.spans[-1] == Text(" 95%", "warning")


def bash(command):
    return ToolCall("c1", "Bash", MappingProxyType({"command": command}))


class Core:
    def __init__(self):
        self.ran = []

    async def __call__(self, value):
        self.ran.append(value.arguments["command"])
        return ToolResult("ran")


def answering(choice, asked):
    async def handler(owner, service, arguments):
        asked.append(arguments["view"]["title"])
        return {"result": {"action": "submit", "selected": [choice]}}

    return handler


async def test_plugins_see_the_sessions_yolo_setting(tmp_path):
    from agent_harness import session_with_provider

    session = session_with_provider(tmp_path)
    from wizolt.plugins.session import SessionPlugins

    plugins = SessionPlugins(session)
    session.settings.yolo = False
    assert plugins.context().yolo is False
    session.settings.yolo = True  # /yolo toggles mid-session; the next callback sees it.
    assert plugins.context().yolo is True
    await plugins.close()


async def test_guard_stays_quiet_without_yolo(runtime):
    await runtime.manage("enable", str(BUILTIN / "guard.py"))
    asked, core = [], Core()
    runtime.interactions.handler = answering("no", asked)
    # wizolt's own approval asks without yolo; the guard must not ask a second time.
    assert await runtime.interception.run("tool.call", bash("rm -rf build"), core) == ToolResult("ran")
    assert asked == [] and core.ran == ["rm -rf build"]


@pytest.mark.parametrize(
    "command", ["rm -rf build", "rm -v -rf build", "rm -fv build", "git push --force origin main", "git reset --hard HEAD~3", "git clean -fdx", "rm -rf \x1b[2Jbuild"]
)
async def test_guard_asks_before_destructive_commands_under_yolo(runtime, command):
    runtime.state["context"] = facts(yolo=True)
    await runtime.manage("enable", str(BUILTIN / "guard.py"))
    asked, core = [], Core()
    runtime.interactions.handler = answering("no", asked)
    result = await runtime.interception.run("tool.call", bash(command), core)
    assert isinstance(result, Refusal) and "declined" in result.reason and core.ran == []
    assert "".join(char for char in command if char.isprintable()) in asked[0]  # Shown, never as escapes.
    runtime.interactions.handler = answering("yes", asked)
    assert await runtime.interception.run("tool.call", bash(command), core) == ToolResult("ran")


async def test_guard_passes_ordinary_commands_and_refuses_when_no_one_can_confirm(runtime):
    runtime.state["context"] = facts(yolo=True)
    await runtime.manage("enable", str(BUILTIN / "guard.py"))
    core = Core()
    assert await runtime.interception.run("tool.call", bash("rm notes.txt && ls"), core) == ToolResult("ran")
    # Headless: no TUI to ask. Refuse with a reason, and keep the registration healthy.
    result = await runtime.interception.run("tool.call", bash("rm -rf /tmp/x"), core)
    assert isinstance(result, Refusal) and "no one can confirm" in result.reason
    assert not runtime.entries["guard"].active.failures and core.ran == ["rm notes.txt && ls"]
