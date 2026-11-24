"""The bundled plugin catalog and context bar through real workers."""

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
        await plugins.manage("enable", "context_bar")
        listing = {item["name"]: item for item in (await plugins.manage("list"))["plugins"]}
        assert listing["context_bar"]["description"].startswith("A bar above your input")  # Live plugins too.
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
    assert set(records) == {"context_bar", "layout", "system_prompt", "tool_visibility", "usage"} and not any(item.enabled for item in records.values())


async def bar_panel(runtime, window, columns=60):
    runtime.state["context"] = facts(window=window)
    runtime.resize(columns, 24)
    if "context_bar" not in runtime.entries:
        await runtime.manage("enable", str(BUILTIN / "context_bar.py"))
    await runtime.refresh()
    panels = runtime.panels("above_input")
    return panels[0] if panels else None


async def test_context_bar_stacks_categories_and_the_unestimated_rest(runtime):
    panel = await bar_panel(runtime, ContextWindow(50_000, 200_000, 180_000, (("system prompt", 10_000), ("assistant", 30_000))))
    bar, legend = panel.rows
    # Measured against the input budget the status bar's ctx divides (50k * 100 // 180k), not
    # the whole 200k window: the bar used to read 25% beside a status row reading 27%.
    assert text(bar).endswith(" 27%") and len(text(bar)) == 60
    # Categories in request order with their theme roles, then "other" for the fill the
    # estimates do not explain, then the free tail.
    assert [span.role for span in bar.spans] == ["context_system", "context_chat", "context_other", "rule", "text"]
    assert sum(len(span.text) for span in bar.spans[:3]) == round(50_000 * 56 / 180_000)
    assert "system 10k" in text(legend) and "other 10k" in text(legend) and text(legend).endswith("50k/180k")
    # The legend lists the largest shares first, so the big ones survive a narrow pane.
    assert text(legend).startswith("assistant 30k")


async def test_context_bar_spreads_estimates_above_the_reported_fill_inside_it(runtime):
    # The local estimates can run ahead of the last reported request; the bar still ends where
    # the reported fill does, so its cells and its percentage tell the same story.
    panel = await bar_panel(runtime, ContextWindow(90_000, 200_000, 180_000, (("system prompt", 20_000), ("tool out", 100_000))))
    bar, _legend = panel.rows
    assert text(bar).endswith(" 50%")
    assert sum(len(span.text) for span in bar.spans if span.role != "rule" and span.role not in ("text", "warning")) == 28


async def test_context_bar_draws_what_compaction_left_apart_from_the_conversation(runtime):
    window = ContextWindow(60_000, 200_000, 180_000, (("skills", 10_000), ("summary", 20_000), ("assistant", 30_000)))
    bar, legend = (await bar_panel(runtime, window)).rows
    # Its own role, distinct from both neighbours, in request order before the conversation.
    assert [span.role for span in bar.spans][:3] == ["context_skills", "context_summary", "context_chat"]
    assert "summary 20k" in text(legend)


async def test_context_bar_keeps_small_categories_and_fits_narrow_terminals(runtime):
    window = ContextWindow(100_000, 200_000, 0, (("system prompt", 100), ("system tools", 9_900), ("skills", 40_000), ("tool out", 50_000)))
    panel = await bar_panel(runtime, window, columns=40)
    bar, legend = panel.rows
    assert len(text(bar)) == 40 and bar.spans[0] == Text("▉", "context_system")  # Tiny, still drawn.
    assert "…" in text(legend) and len(text(legend)) == 40 and text(legend).endswith("100k/200k")
    # Too narrow for the percentage beside the bar: the legend's totals carry it instead.
    bar, legend = (await bar_panel(runtime, window, columns=13)).rows
    assert "%" not in text(bar) and text(legend).strip() == "100k/200k 50%"
    # Narrower still: the totals no longer fit either, and the percentage is what stays.
    bar, legend = (await bar_panel(runtime, window, columns=12)).rows
    assert len(text(legend)) == 12 and text(legend).strip() == "50%"


async def test_context_bar_never_draws_wider_than_the_panel(runtime):
    window = ContextWindow(100_000, 200_000, 0, (("system prompt", 100), ("assistant", 99_900)))
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
    assert bar.spans[-1] == Text(" 95%", "error")


@pytest.mark.parametrize("depth", ["DEPTH_8_BIT", "DEPTH_24_BIT"])
def test_context_categories_are_distinct_in_terminal_output(depth, monkeypatch):
    from itertools import combinations
    from math import dist

    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.output import ColorDepth
    from rich.text import Text as RichText

    from wizolt.plugins.builtin.context_bar import KEYS, draw
    from wizolt.ui.cli.plugins import PluginView
    from wizolt.ui.render import Theme, UiPrinter
    from wizolt.ui.themes import contrast

    for variable in ("COLORTERM", "PROMPT_TOOLKIT_COLOR_DEPTH", "NO_COLOR"):
        monkeypatch.delenv(variable, raising=False)
    parts = tuple((name, 100) for name in KEYS if name != "other")
    panel = draw(facts(columns=180, window=ContextWindow(100 * len(KEYS), 2000, 2000, parts)))
    bar, legend = panel.rows
    segments = [span for span in bar.spans if span.text.endswith(("█", "▉"))]
    assert len(segments) == len(KEYS)
    assert all(span.text.endswith("▉") for span in segments[:-1]) and segments[-1].text.endswith("█")
    assert {span.text.strip(): span.role for span in legend.spans if span.text.strip() in dict(KEYS.values())} == dict(KEYS.values())
    for theme in Theme.BUILTIN:
        monkeypatch.setattr(Theme, "_mode", theme)
        rendered = PluginView.render([panel], 180, 2)
        ansi = UiPrinter.render_to_ansi([FormattedText(rendered)], columns=180, color_depth=ColorDepth[depth])
        first = RichText.from_ansi(ansi.splitlines()[0])
        colors = [span.style.color.get_truecolor() for span in first.spans if "█" in first.plain[span.start : span.end]]
        by_role = dict(zip((segment.role for segment in segments), colors, strict=True))
        chat = by_role["context_chat"]
        assert max(chat) - min(chat) <= 32, (theme, depth, chat)
        assert min(dist(a, b) for a, b in combinations(colors, 2)) >= 75, (theme, depth, colors)
        background = Theme.active().background or ("#ffffff" if Theme.appearance() == "light" else "#1e1e1e")
        assert all(contrast(color.hex, background) >= 4.5 for color in colors), (theme, depth)


@pytest.mark.parametrize(("used", "role"), [(79, "text"), (80, "warning"), (94, "warning"), (95, "error")])
def test_context_pressure_thresholds(used, role):
    from wizolt.plugins.builtin.context_bar import draw

    panel = draw(facts(window=ContextWindow(used, 100)))
    assert panel.rows[0].spans[-1] == Text(f" {used}%", role)
