"""Named presentation sites: registration contracts and the worker ``present`` operation."""

from dataclasses import asdict

import pytest

from wizolt.plugins.protocol import PresenterSpec, Snapshot, decode_panel
from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, Panel, Plugin, PluginError, ToolCounts
from wizolt.sdk.presentation import ActivityStatus, ToolCard, ToolSummary, decode, encode


@pytest.fixture
async def runtime():
    instance = PluginRuntime(lambda: Context("root", "main", "/tmp", "idle", 0, 0, "model", 0))
    yield instance
    await instance.close()


async def enable(runtime, tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text("from wizolt.sdk import Panel, Text\nSDK_VERSION = 1\n" + body)
    await runtime.manage("enable", str(path))
    return runtime.entries[name].active


async def present(generation, site, view, context=None):
    context = context or Context("root", "main", "/tmp", "idle", 0, 0, "model", 0)
    raw = await generation.worker.request("present", timeout=5, site=site, view=encode(view), context=asdict(context))
    panel = decode_panel(raw)
    Snapshot.check_panel(panel)
    return panel


CARD = """
def setup(p):
    async def card(ctx, view):
        return Panel((Text(view.tool + " ran", "tool"),))
    p.presenter("tool.call", card, match={"tool": "Bash"})
"""


async def test_registration_rejects_unknown_sites_and_bad_matchers():
    plugin = Plugin("demo")

    async def render(context, view):
        return Panel()

    with pytest.raises(PluginError, match="Unknown presentation site"):
        plugin.presenter("paint.brush", render)
    with pytest.raises(PluginError, match="activity matches only no fields"):
        plugin.presenter("activity", render, match={"tool": "Bash"})
    with pytest.raises(PluginError, match="Expected async"):
        plugin.presenter("tool.result", lambda context, view: Panel())
    with pytest.raises(PluginError, match="Duplicate presenter"):
        plugin.presenter("tool.result", render)
        plugin.presenter("tool.result", render)


def test_spec_decode_validates_sites_and_matchers():
    spec = PresenterSpec.decode("tool.result", {"match": {"tool": ["Bash", "Job"]}})
    assert spec.match == {"tool": frozenset({"Bash", "Job"})}
    assert spec.matches(ToolSummary("1", "Bash"))
    assert not spec.matches(ToolSummary("1", "Read"))
    with pytest.raises(PluginError, match="Invalid presenter registration"):
        PresenterSpec.decode("paint.brush", {})
    with pytest.raises(PluginError, match="Invalid presenter registration"):
        PresenterSpec.decode("tool.call", {"match": {"status": ["ok"]}})


def test_views_round_trip_through_checked_json():
    view = ToolSummary("call-1", "Bash", {"command": "ls"}, "ok", "done", 1.5, "tr.3")
    assert decode("tool.result", encode(view)) == view
    activity = ActivityStatus("working", 2.0, "output", "text", (), ToolCounts(started=2))
    assert decode("activity", encode(activity)) == activity
    with pytest.raises(PluginError, match="Unknown tool summary status"):
        encode(ToolSummary("1", "Bash", status="maybe"))


async def test_worker_presents_a_bounded_panel(runtime, tmp_path):
    generation = await enable(runtime, tmp_path, "cards", CARD)
    panel = await present(generation, "tool.call", ToolCard("call-1", "Bash", {"command": "ls"}))
    assert [row.text for row in panel.rows] == ["Bash ran"]
    # The site's matcher travels with the capabilities, and inspection lists it.
    assert generation.plugin.presenters["tool.call"].match["tool"] == frozenset({"Bash"})
    assert "presenters" in (await runtime.manage("list"))["plugins"][0]


async def test_worker_rejects_oversized_presentations(runtime, tmp_path):
    generation = await enable(
        runtime,
        tmp_path,
        "flood",
        "def setup(p):\n    async def card(ctx, view):\n        return Panel((Text('x' * 5000),))\n    p.presenter('tool.call', card)\n",
    )
    with pytest.raises(PluginError):
        await present(generation, "tool.call", ToolCard("call-1", "Bash"))
