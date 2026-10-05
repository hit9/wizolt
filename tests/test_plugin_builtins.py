"""The bundled context bar and pet through real workers: what a user sees and chooses."""

from pathlib import Path

import pytest

from wizolt.plugins.catalog import PluginCatalog
from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, ContextWindow, Line, Text

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
        assert listing["context_bar"]["status"] == "off" and listing["context_bar"]["description"].startswith("A bar above your input")
        await plugins.manage("enable", "pet")
        listing = {item["name"]: item for item in (await plugins.manage("list"))["plugins"]}
        assert listing["pet"]["description"].startswith("A rainbow pet")  # Live plugins too.
        from types import SimpleNamespace

        from wizolt.ui.cli.plugins import PluginManager

        manager = PluginManager(SimpleNamespace(), plugins)  # type: ignore[arg-type]
        manager.records = listing
        # The preview opens with what the plugin is, before where it lives.
        assert manager.preview("context_bar").startswith("A bar above your input")
        # About: the author's documentation for an off plugin, plus a live demo once it runs.
        assert "## Use" in manager.about("context_bar") and "What it shows now" not in manager.about("context_bar")
        await plugins.refresh()
        assert "## What it shows now" in manager.about("pet") and "/\\_/\\" in manager.about("pet").partition("What it shows now")[2]
    finally:
        await plugins.close()


def test_about_pages_reflow_prose_but_keep_code_and_lists():
    from wizolt.ui.cli.plugins import unwrap

    source = "A wrapped\nsentence.\n\n```text\nart  line\nkept\n```\n\n## Use\n\n- one item\n  continued\n- two"
    assert unwrap(source) == "A wrapped sentence.\n\n```text\nart  line\nkept\n```\n\n## Use\n\n- one item continued\n- two"


def test_builtins_ship_disabled(tmp_path):
    records, _ = PluginCatalog.for_user(str(tmp_path)).read()
    assert set(records) == {"context_bar", "layout", "pet"} and not any(item.enabled for item in records.values())


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


async def pet_rows(runtime):
    await runtime.refresh()
    [panel] = runtime.panels("above_input")
    return [text(row) for row in panel.rows]


async def test_the_pet_walks_only_while_the_agent_works(runtime):
    await runtime.manage("enable", str(BUILTIN / "pet.py"))
    crown, body = await pet_rows(runtime)
    assert "/\\_/\\" in crown and "(" in body  # The cat by default.
    resting = body.index("(")
    for now in (1, 2, 3):  # Idle time is never credited to the walk.
        runtime.state["context"] = facts(now=now)
        assert (await pet_rows(runtime))[1].index("(") == resting
    for now in (4, 4.5, 5):
        runtime.state["context"] = facts(status="running", now=now)
        await runtime.refresh()
    assert (await pet_rows(runtime))[1].index("(") != resting


async def test_pet_picks_another_pet_in_place(runtime):
    await runtime.manage("enable", str(BUILTIN / "pet.py"))
    offered = []

    async def answer(owner, service, arguments):
        offered.append([item["id"] for item in arguments["view"]["body"]["items"]])
        return {"result": {"action": "submit", "selected": ["bear"]}}

    runtime.interactions.handler = answer
    result = await runtime.invoke("pet", "command", "pet", {})
    assert offered == [["cat", "bear", "owl", "bunny", "fish", "robot"]] and "bear" in result
    assert "ʕ" in (await pet_rows(runtime))[1]  # No reload: the pick applies at once.


def test_every_pet_has_its_own_silhouette():
    from wizolt.plugins.builtin.pet import PETS, _preview

    # Distinct animals, not one face under different hats: no two pets draw the same body row.
    bodies = [_preview(pet).splitlines()[1] for pet in PETS.values()]
    crowns = [pet.crown for pet in PETS.values()]
    assert len(set(crowns)) == len(PETS) and len({(crown, body) for crown, body in zip(crowns, bodies, strict=True)}) == len(PETS)
    assert len({pet.wrap for pet in PETS.values()}) >= 4
