"""Named presentation sites: registration, the worker operation, and the runner's tool sites."""

from dataclasses import asdict

import pytest
from agent_harness import session_with_provider

from wizolt.agent.context import ContextManager
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.agent.runner import ToolRunner
from wizolt.model import ModelClient
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


async def test_sole_matching_presenter_renders_and_nonmatching_stays_builtin(runtime, tmp_path):
    await enable(runtime, tmp_path, "cards", CARD)
    panel = await runtime.presenters.render("tool.call", ToolCard("call-1", "Bash"))
    assert panel is not None and panel.rows[0].text == "Bash ran"
    # The prefilter runs host-side: a nonmatching view never reaches the worker.
    assert await runtime.presenters.render("tool.call", ToolCard("call-1", "Read")) is None


async def test_overlapping_presenters_stay_builtin_until_the_user_picks_one(runtime, tmp_path):
    await enable(runtime, tmp_path, "alpha", CARD)
    await enable(
        runtime,
        tmp_path,
        "beta",
        'def setup(p):\n    async def card(ctx, view):\n        return Panel((Text("beta card", "tool"),))\n    p.presenter("tool.call", card)\n',
    )
    view = ToolCard("call-1", "Bash")
    assert await runtime.presenters.render("tool.call", view) is None
    assert runtime.presenters.conflicts["tool.call"] == ("alpha", "beta")
    runtime.presenters.choices.save({"tool.call": "beta"})
    panel = await runtime.presenters.render("tool.call", view)
    assert panel is not None and panel.rows[0].text == "beta card"
    runtime.presenters.conflicts.clear()


async def test_failed_presenter_falls_back_and_is_skipped_afterwards(runtime, tmp_path):
    generation = await enable(
        runtime,
        tmp_path,
        "broken",
        'def setup(p):\n    async def card(ctx, view):\n        raise RuntimeError("no cards today")\n    p.presenter("tool.result", card)\n',
    )
    view = ToolSummary("call-1", "Bash", {"command": "ls"})
    assert await runtime.presenters.render("tool.result", view) is None
    assert "presenter:tool.result" in generation.failures
    # A failed registration is skipped without another worker invocation.
    assert await runtime.presenters.render("tool.result", view) is None


async def test_slow_presenter_falls_back_within_its_budget(runtime, tmp_path):
    generation = await enable(
        runtime,
        tmp_path,
        "asleep",
        "import asyncio\ndef setup(p):\n    async def card(ctx, view):\n        await asyncio.sleep(5)\n    p.presenter('tool.result', card)\n",
    )
    assert await runtime.presenters.render("tool.result", ToolSummary("call-1", "Bash"), timeout=0.05) is None
    assert "presenter:tool.result" in generation.failures


@pytest.fixture
async def session(tmp_path):
    instance = session_with_provider(tmp_path)
    bootstrap_features(instance)
    instance.settings.yolo = True
    yield instance
    await instance.plugins.close()


async def enable_session(session, tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text("from wizolt.sdk import Panel, Text\nSDK_VERSION = 1\n" + body)
    await session.plugins.manage("enable", str(path))


def runner(session, answers=None):
    printed = []
    replies = iter(answers or ())
    instance = ToolRunner(session, ContextManager(session), input_fn=lambda prompt: next(replies), output_fn=lambda text: printed.append(str(text)))
    return instance, printed


BOTH = """
def setup(p):
    async def card(ctx, view):
        return Panel((Text("card:" + view.tool, "tool"),))
    async def summary(ctx, view):
        return Panel((Text("summary:" + view.status, "success"),))
    p.presenter("tool.call", card)
    p.presenter("tool.result", summary)
"""


async def test_tool_sites_present_through_the_runner(session, tmp_path):
    await enable_session(session, tmp_path, "cards", BOTH)
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    instance, printed = runner(session)
    await instance.run([ModelClient.tool_call("r1", "Read", {"path": str(path)})])
    text = "\n".join(printed)
    assert "card:Read" in text and "summary:ok" in text
    assert "tr." in text  # The stored-result citation stays host-owned and rides the block.


async def test_unpresented_calls_keep_the_builtin_block(session, tmp_path):
    body = BOTH.replace('p.presenter("tool.call", card)', 'p.presenter("tool.call", card, match={"tool": "Bash"})').replace(
        'p.presenter("tool.result", summary)', 'p.presenter("tool.result", summary, match={"tool": "Bash"})'
    )
    await enable_session(session, tmp_path, "bashonly", body)
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    instance, printed = runner(session)
    await instance.run([ModelClient.tool_call("r1", "Read", {"path": str(path)})])
    text = "\n".join(printed)
    assert "card:Read" not in text and "summary:ok" not in text
    assert "notes.txt" in text  # The builtin call line still names the invocation.


async def test_failing_presenters_fall_back_to_the_builtin_block(session, tmp_path):
    await enable_session(
        session,
        tmp_path,
        "broken",
        'def setup(p):\n    async def summary(ctx, view):\n        raise RuntimeError("no summaries")\n    p.presenter("tool.result", summary)\n',
    )
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    instance, printed = runner(session)
    await instance.run([ModelClient.tool_call("r1", "Read", {"path": str(path)})])
    assert "notes.txt" in "\n".join(printed)


ACTIVITY = """
def setup(p):
    async def line(ctx, view):
        tools = ", ".join(item.name for item in view.active_tools)
        return Panel((Text("status:" + view.status + (":" + tools if tools else ""), "meta"),))
    p.presenter("activity", line)
"""


async def test_activity_site_refreshes_the_cached_snapshot(runtime, tmp_path):
    await enable(runtime, tmp_path, "meter", ACTIVITY)
    runtime.read_stream = lambda: ("output", "answering")
    runtime.activity.start("c1", "Bash")
    await runtime.refresh()
    panel = runtime.presenters.activity
    assert panel is not None and panel.rows[0].text.startswith("status:idle:Bash")
    # An empty or failed refresh keeps the last snapshot; a new pass replaces it.
    runtime.activity.finish(runtime.activity.active["1"], "completed")
    await runtime.refresh()
    assert runtime.presenters.activity.rows[0].text == "status:idle"


async def test_activity_panel_replaces_the_stream_region(session, tmp_path):
    from wizolt.ui.cli.presentation import Presentation
    from wizolt.ui.cli.view import View

    await enable_session(session, tmp_path, "meter", ACTIVITY)
    session.plugins.read_stream = lambda: ("output", "partial answer")
    session.plugins.activity.start("c1", "Bash")
    await session.plugins.refresh()
    view = View(session, Presentation(session, output_fn=lambda _: None))
    fragments = view.plugin_activity_fragments()
    assert any("status:" in text for _, text in fragments)
    # The builtin stream preview is the fallback, not an addition: without a cached panel it is
    # what the region shows, and it stays host-owned.
    assert "thinking" not in "".join(text for _, text in fragments)
