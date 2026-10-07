"""The bundled context bar and pet through real workers: what a user sees and chooses."""

from pathlib import Path

import pytest

from wizolt.plugins.catalog import PluginCatalog
from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, ContextWindow, Line, Text

BUILTIN = Path(__file__).parents[1] / "wizolt/plugins/builtin"


def facts(**changes):
    values = {
        "agent_id": "root",
        "agent_name": "main",
        "cwd": "/tmp",
        "status": "idle",
        "context_percent": 0,
        "elapsed": 0,
        "model": "m",
        "now": 0,
        "columns": 60,
    }
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


def test_every_builtin_imports_only_the_public_sdk():
    import ast

    from wizolt.plugins.loading import sdk_boundary_violations

    # Admission rejects a source that imports past wizolt.sdk; holding every builtin to the same
    # walk keeps the bundled set on the documented contract, not on review vigilance.
    for path in BUILTIN.glob("*.py"):
        violations = sdk_boundary_violations(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        assert not violations, f"{path.stem} imports {violations[0]}"


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
        # The preview is the plugin's About page, rendered, before the facts about where it lives.
        preview = "".join(text for _, text in manager.preview("context_bar"))
        assert preview.lstrip(" │").startswith("A bar above your input") and "│ Use\n" in preview and "## Use" not in preview
        assert preview.index("│ Use\n") < preview.index("Built in")
        assert manager.preview("context_bar") and manager.pages  # Rendered once, then reused.
    finally:
        await plugins.close()


def test_about_pages_reflow_prose_but_keep_code_and_lists():
    from wizolt.ui.cli.plugins import unwrap

    source = "A wrapped\nsentence.\n\n```text\nart  line\nkept\n```\n\n## Use\n\n- one item\n  continued\n- two"
    assert unwrap(source) == "A wrapped sentence.\n\n```text\nart  line\nkept\n```\n\n## Use\n\n- one item continued\n- two"


def test_builtins_ship_disabled(tmp_path):
    records, _ = PluginCatalog.for_user(str(tmp_path)).read()
    assert set(records) == {"context_bar", "layout", "pet", "system_prompt", "tool_visibility", "usage"} and not any(item.enabled for item in records.values())


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


async def test_context_bar_draws_what_compaction_left_apart_from_the_conversation(runtime):
    window = ContextWindow(60_000, 200_000, 180_000, (("skills", 10_000), ("summary", 20_000), ("messages", 30_000)))
    bar, legend = (await bar_panel(runtime, window)).rows
    # Its own role, distinct from both neighbours, in request order before the conversation.
    assert [span.role for span in bar.spans][:3] == ["status_reason", "accent_secondary", "info"]
    assert "summary 20k" in text(legend)


async def test_context_bar_keeps_small_categories_and_fits_narrow_terminals(runtime):
    window = ContextWindow(100_000, 200_000, 0, (("system prompt", 100), ("system tools", 9_900), ("skills", 40_000), ("messages", 50_000)))
    panel = await bar_panel(runtime, window, columns=40)
    bar, legend = panel.rows
    assert len(text(bar)) == 40 and bar.spans[0] == Text("█", "status_provider")  # Tiny, still drawn.
    assert "…" in text(legend) and len(text(legend)) == 40 and text(legend).endswith("100k/200k")
    # Too narrow for the percentage beside the bar: the legend's totals carry it instead.
    bar, legend = (await bar_panel(runtime, window, columns=13)).rows
    assert "%" not in text(bar) and text(legend).strip() == "100k/200k 50%"
    # Narrower still: the totals no longer fit either, and the percentage is what stays.
    bar, legend = (await bar_panel(runtime, window, columns=12)).rows
    assert len(text(legend)) == 12 and text(legend).strip() == "50%"


async def test_context_bar_never_draws_wider_than_the_panel(runtime):
    window = ContextWindow(100_000, 200_000, 0, (("system prompt", 100), ("messages", 99_900)))
    runtime.state["context"] = facts(window=window)
    await runtime.manage("enable", str(BUILTIN / "context_bar.py"))
    for columns in range(4, 40):
        runtime.resize(columns, 24)
        await runtime.refresh()
        [panel] = runtime.panels("above_input")
        for row in panel.rows:
            assert len(text(row)) <= columns, (columns, text(row))


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


def test_the_pet_never_draws_wider_than_the_panel():
    from wizolt.plugins.builtin.pet import PETS, _caption_left, _draw

    # A terminal narrower than the caption has nowhere to put it: the pet draws without one
    # rather than a line that spills past the panel.
    for caption in ("on it", "your move", "nailed it", "taking five", "strolling", "hello"):
        for columns in range(2, 40):
            for walked in (0.0, 3.7, 21.0):
                assert _caption_left(columns, 1, 1, 8, caption) is None or _caption_left(columns, 1, 1, 8, caption) + len(caption) <= columns
    for columns in range(PETS["cat"].width + 1, 40):
        for walked in (0.0, 3.7, 21.0):
            panel = _draw(_pet_context(columns), PETS["cat"], walked, True)
            for row in panel.rows:
                assert sum(len(span.text) for span in row.spans) <= columns, (columns, walked)


def _pet_context(columns):
    from types import SimpleNamespace

    return SimpleNamespace(columns=columns, layout=None, now=0.0, status="idle", window=SimpleNamespace(parts=[], limit=0, budget=0, used=0.0))
