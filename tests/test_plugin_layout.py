"""Component order and space are host facts, shared by UI, SDK and offline previews."""

import json

import pytest
from agent_harness import session_with_provider

from wizolt.plugins.session import SessionPlugins
from wizolt.sdk import PluginError


def component(tmp_path, name, slot="above_input", rows=1, responsive=False):
    path = tmp_path / (name + ".py")
    count = f"min({rows}, ctx.layout.rows)" if responsive else str(rows)
    path.write_text(f'''from wizolt.sdk import Panel, Text
SDK_VERSION = 1
def draw(ctx):
    return Panel(tuple(Text(f"{name}:{{ctx.layout.columns}}:{{ctx.layout.rows}}:{{ctx.viewport.rows}}") for _ in range({count})))
def setup(p):
    p.component({slot!r}, draw)
''')
    return str(path)


async def test_order_is_stable_saved_and_owned_by_host_not_manager(tmp_path):
    session = session_with_provider(tmp_path)
    runtime = session.plugins
    runtime.REFRESH_INTERVAL = 3600
    fresh = SessionPlugins(session)
    try:
        for name in ("zebra", "alpha"):
            await runtime.manage("enable", component(tmp_path, name))
        await runtime.refresh()
        assert [item.plugin for item in runtime.components()] == ["alpha", "zebra"]
        await runtime.manage("enable", "layout")
        result = await runtime.invoke("layout", "tool", "move_component", {"component": "zebra.above_input", "before": "alpha.above_input"})
        assert [item["plugin"] for item in json.loads(result)] == ["zebra", "alpha"]
        await runtime.manage("disable", "layout")
        await runtime.manage("disable", "zebra")
        await runtime.manage("enable", "zebra")
        await runtime.refresh()
        assert [p.rows[0].text.split(":")[0] for p in runtime.panels("above_input")] == ["zebra", "alpha"]
        await fresh.load()
        assert [item.plugin for item in fresh.components()] == ["zebra", "alpha"]
        await runtime.call_host("ui.components.reset_order", {"slot": "above_input"})
        assert [item.plugin for item in runtime.components()] == ["alpha", "zebra"]
        assert [item.plugin for item in fresh.components()] == ["zebra", "alpha"], "other agents change only on explicit reload"
        await fresh.hot_reload()
        assert [item.plugin for item in fresh.components()] == ["alpha", "zebra"]
    finally:
        await runtime.close()
        await fresh.close()


async def test_height_is_allocated_across_slots_and_resize_reaches_callbacks(tmp_path):
    runtime = session_with_provider(tmp_path).plugins
    runtime.REFRESH_INTERVAL = 3600
    try:
        # Registration order must not consume space ahead of visual order.
        await runtime.manage("enable", component(tmp_path, "last", "below_input", 2))
        await runtime.manage("enable", component(tmp_path, "middle", "above_input", 3, responsive=True))
        await runtime.manage("enable", component(tmp_path, "first", "above_divider", 2))
        runtime.resize(45, 24)  # Four prompt rows in total.
        await runtime.refresh()
        first, middle, last = runtime.components()
        assert (first.rows, middle.rows, last.rows) == (2, 2, 0)
        assert middle.available_rows == 2 and middle.visibility == "visible"
        assert last.visibility == "hidden by height budget"
        assert runtime.panels("above_input")[0].rows[0].text == "middle:45:2:24"
        runtime.resize(30, 12)
        await runtime.refresh()
        assert runtime.components()[0].visibility == "clipped by height budget"
        assert sum(item.rows for item in runtime.components()) == 1
        runtime.resize(120, 40)
        await runtime.refresh()
        assert [item.rows for item in runtime.components()] == [2, 3, 1]
        assert runtime.panels("below_input")[0].rows[0].text == "last:120:1:40"
    finally:
        await runtime.close()


async def test_layout_rejections_do_not_change_preferences_and_bad_disk_keeps_live_order(tmp_path):
    runtime = session_with_provider(tmp_path).plugins
    try:
        await runtime.manage("enable", component(tmp_path, "one"))
        await runtime.manage("enable", component(tmp_path, "two", "below_input"))
        for arguments in (
            {"component": "one.above_input", "before": "two.below_input", "after": ""},
            {"component": "absent", "before": "one.above_input", "after": ""},
            {"component": "one.above_input", "before": "", "after": ""},
        ):
            with pytest.raises(PluginError):
                await runtime.call_host("ui.components.move", arguments)
        assert not runtime.order.directory.exists()
        runtime.order.directory.mkdir(parents=True)
        (runtime.order.directory / "above_input.json").write_text("[broken")
        runtime.reload_layout()
        assert runtime.order.orders == {} and runtime.problems
    finally:
        await runtime.close()


def test_offline_preview_uses_the_requested_height(tmp_path, capsys):
    from wizolt.ui.cli.plugin_testing import main

    path = component(tmp_path, "preview", rows=5, responsive=True)
    assert main(["test", path, "--project", str(tmp_path), "--width", "40", "--height", "12", "--output", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    rows = report["frames"][0]["panels"]["above_input"]["rows"]
    assert rows == [{"text": "preview:40:1:12", "role": "text"}]


async def test_human_manager_moves_without_a_management_plugin(tmp_path):
    from types import SimpleNamespace

    from wizolt.ui.cli.plugins import PluginManager

    runtime = session_with_provider(tmp_path).plugins
    replies = iter(("move up", "reset slot order"))

    class Terminal:
        async def show_modal(self, *args, **kwargs):
            return next(replies)

    loop = SimpleNamespace(presentation=SimpleNamespace(tui=Terminal()))
    try:
        for name in ("one", "two"):
            await runtime.manage("enable", component(tmp_path, name))
        manager = PluginManager(loop, runtime)
        await manager.arrange("two")
        assert [item.plugin for item in runtime.components()] == ["two", "one"]
        assert "layout" not in runtime.entries
        await manager.arrange("two")
        assert [item.plugin for item in runtime.components()] == ["one", "two"]
    finally:
        await runtime.close()


async def test_observers_cannot_mutate_layout(tmp_path):
    runtime = session_with_provider(tmp_path).plugins
    path = tmp_path / "intruder.py"
    path.write_text('''SDK_VERSION = 1
def setup(p):
    async def event(e):
        await p.ui.components.reset_order()
    p.on("turn.started", event)
''')
    try:
        await runtime.manage("enable", str(path))
        await runtime.start_turn()
        assert runtime.entries["intruder"].active.error
        assert not runtime.order.directory.exists()
    finally:
        await runtime.close()


async def test_multi_slot_plugin_samples_once_and_only_components_receive_allocation(tmp_path):
    runtime = session_with_provider(tmp_path).plugins
    runtime.REFRESH_INTERVAL = 3600
    path = tmp_path / "multi.py"
    path.write_text('''from wizolt.sdk import Panel, Text
SDK_VERSION = 1
def setup(p):
    state = {"samples": 0}
    async def sample(event):
        assert event.context.layout is None
        state["samples"] += 1
    def value(ctx):
        assert ctx.layout is None
        return state["samples"]
    def draw(ctx):
        return Panel((Text(str(ctx.layout.rows)),))
    p.on("sample", sample)
    p.field("samples", value)
    for slot in ("above_input", "below_input", "status"):
        p.component(slot, draw)
''')
    try:
        await runtime.manage("enable", str(path))
        before = runtime.fields()["plugins.multi.samples"]
        await runtime.refresh()
        assert runtime.fields()["plugins.multi.samples"] == before + 1
        assert [runtime.panels(slot)[0].rows[0].text for slot in ("above_input", "below_input", "status")] == ["4", "3", "12"]
    finally:
        await runtime.close()
