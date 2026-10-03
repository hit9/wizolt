"""loop skills (split from tests/test_loop_commands.py)."""

import colorsys
from itertools import pairwise

import pytest
from agent_harness import session
from prompt_toolkit.utils import get_cwidth
from test_loop_commands import _write_skill

from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import create_session
from wizolt.agent.prompts import SYSTEM_PROMPT
from wizolt.base import Text, ToolError
from wizolt.skill.library import SkillLibrary
from wizolt.tools import SkillTool, Tool
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.commands import skills_command
from wizolt.ui.cli.status import TABS, StatusReport, StatusTabs, StatusView
from wizolt.ui.render import StatusBar, Theme, WidthDependent
from wizolt.ui.themes import contrast
from wizolt.ui.tui import TUI_MODAL_PENDING


def test_skill_library_index_and_lookup(tmp_path):
    _write_skill(tmp_path, "release-notes", "Draft a CHANGELOG entry.", "Do the thing.")
    s = session(tmp_path)

    index = s.skills.index()
    assert index.startswith("--- SKILLS ---")
    assert "- release-notes [project]: Draft a CHANGELOG entry." in index
    assert s.skills.get("Release-Notes").name == "release-notes"  # case-insensitive
    assert s.skills.get("missing") is None


def test_project_skills_prefer_wizolt_and_fall_back_to_minacode(tmp_path):
    legacy = tmp_path / ".minacode" / "skills" / "guide"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("---\nname: guide\ndescription: legacy\n---\nlegacy body\n", encoding="utf-8")

    skill = session(tmp_path).skills.get("guide")
    assert skill is not None
    assert skill.description == "legacy"

    _write_skill(tmp_path, "guide", "current", "current body")
    skill = session(tmp_path).skills.get("guide")
    assert skill is not None
    assert skill.description == "current"


def test_skill_project_overrides_user(tmp_path):
    user_skill = tmp_path / "data" / "skills" / "guide"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("---\nname: guide\ndescription: user version\n---\nuser body\n", encoding="utf-8")

    user_session = session(tmp_path)
    skill = user_session.skills.get("guide")
    assert skill.source == "user"
    assert skill.description == "user version"

    _write_skill(tmp_path, "guide", "project version", "project body")

    project_session = session(tmp_path)
    skill = project_session.skills.get("guide")
    assert skill.source == "project"
    assert skill.description == "project version"


async def test_skill_tool_expands_skill_dir(tmp_path):
    folder = _write_skill(tmp_path, "build", "build it", 'Run python "{skill_dir}/scripts/go.py".', scripts={"go.py": "print(1)"})
    s = session(tmp_path)

    output = await SkillTool(s, ["build"]).call()
    assert output.startswith('<Skill name="build" source="project">')
    assert f'python "{folder}/scripts/go.py"' in output
    assert "{skill_dir}" not in output


async def test_skill_tool_unknown_lists_available(tmp_path):
    _write_skill(tmp_path, "known", "known skill", "body")
    s = session(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        await SkillTool(s, ["nope"]).call()
    assert "unknown skill 'nope'" in str(excinfo.value)
    assert "known" in str(excinfo.value)


def test_skill_mentions_name_the_skill_without_inlining_body(tmp_path):
    _write_skill(tmp_path, "triage", "triage a bug", "Reproduce first.")
    s = session(tmp_path)

    resolved = s.skills.resolve_mentions("please $triage this")
    assert "--- SKILL MENTIONS ---" in resolved
    assert "- triage [project]: triage a bug" in resolved
    assert "Reproduce first." not in resolved
    # a bare word without $ is not a mention; an unknown $token is ignored
    assert s.skills.resolve_mentions("triage this") == ""
    assert s.skills.resolve_mentions("$unknown") == ""


def test_skill_tool_absent_only_when_no_skills(tmp_path):
    _write_skill(tmp_path, "available", "available skill", "body")
    withskill = ContextManager(session(tmp_path))
    assert "--- SKILLS ---" in withskill.skills_context()
    assert any(t["function"]["name"] == "Skill" for t in Tool.resolved_schemas(withskill.session))
    messages = withskill.model_messages("system", [{"role": "user", "content": "hi"}])
    assert any(m["content"].startswith("--- SKILLS ---") for m in messages)

    # When truly no skills exist, the tool and section drop out and the prefix stays clean.
    bare = ContextManager(session(tmp_path))
    bare.session.skills = SkillLibrary({})
    assert bare.skills_context() == ""
    tools = Tool.resolved_schemas(bare.session)
    assert not any(t["function"]["name"] == "Skill" for t in tools)
    assert all("--- SKILLS ---" not in str(message.get("content", "")) for message in bare.model_messages(SYSTEM_PROMPT))


def test_skills_command_lists_installed(tmp_path):
    base = CommandLoop(Agent(session(tmp_path), output_fn=lambda t: None), output_fn=lambda t: None)
    assert "plugin-workshop" in skills_command(base, "")

    _write_skill(tmp_path, "release-notes", "Draft a CHANGELOG entry.", "body")
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda t: None), output_fn=lambda t: None)
    output = skills_command(loop, "")
    assert "### Skills · 2" in output
    assert "| skill | source | from | description |" in output
    assert "| `release-notes` | project | `.wizolt/skills/release-notes` | Draft a CHANGELOG entry. |" in output


async def test_skill_loads_dedup_on_repeat(tmp_path):
    _write_skill(tmp_path, "guide", "a guide", "FULL GUIDE INSTRUCTIONS")
    s = session(tmp_path)
    body = await SkillTool(s, ["guide"]).call()
    messages = [{"role": "tool", "content": "tr.1 " + body}, {"role": "tool", "content": "tr.7 " + body}]

    deduped = ContextManager(s).dedup_skill_loads(messages)
    assert "FULL GUIDE INSTRUCTIONS" in deduped[0]["content"]  # first copy kept
    assert "FULL GUIDE INSTRUCTIONS" not in deduped[1]["content"]  # repeat collapsed
    assert "repeat load of skill guide" in deduped[1]["content"]
    assert "tr.1" in deduped[1]["content"]


def test_status_and_bar_show_skill_count(tmp_path):
    _write_skill(tmp_path, "one", "d1", "b")
    _write_skill(tmp_path, "two", "d2", "b")
    s = session(tmp_path)
    s.config.mcp = {
        "connected": {"url": "https://connected.example/mcp"},
        "disconnected": {"url": "https://disconnected.example/mcp"},
    }
    s.mcp.tools["connected"] = []
    s.mcp.resources["connected"] = []
    loop = CommandLoop(Agent(s, output_fn=lambda t: None), output_fn=lambda t: None)

    count = len(s.skills.skills)
    assert count == 3  # two project skills and the built-in plugin development skill
    report = StatusReport.of(loop)
    activity = dict(report.snapshot.activity.rows)
    assert activity["mcp servers"] == "1"
    assert activity["skills"] == str(count)
    assert f"/ {loop.agent.context.request_token_budget() / 1_000:.1f}K" in report.text()
    # Before any request, usage is unavailable rather than a column of zeros.
    assert report.snapshot.usage[0].rows == (("requests", "none yet"),)
    bar_text = "".join(text for _, text in StatusBar(s).fragments())
    assert f"skills {count}" in bar_text


def test_status_shows_agents_md_state(tmp_path):
    # No candidate file in cwd: still on, but nothing loaded.
    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)
    assert configuration(loop)["agents.md"] == "on (global missing)"

    # Loaded from the project's AGENTS.md.
    (tmp_path / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    loaded = session(tmp_path)
    loaded_loop = CommandLoop(Agent(loaded, output_fn=lambda text: None), output_fn=lambda text: None)
    assert configuration(loaded_loop)["agents.md"] == "on (./AGENTS.md; global missing)"

    # Disabled at runtime: reports off.
    loaded.settings.agents_md = False
    assert configuration(loaded_loop)["agents.md"] == "off (global missing)"


def configuration(loop: CommandLoop) -> dict[str, str]:
    return dict(StatusReport.of(loop).snapshot.configuration.rows)


def test_status_keeps_active_turn_in_context_percentage(tmp_path):
    s = session(tmp_path)
    s.settings.max_context_tokens = 100_000
    s._active_turn_messages = [{"role": "user", "content": "active " + "x" * 200_000}]
    context = ContextManager(s)
    tools = Tool.resolved_schemas(s)
    # Persisted-only baseline (no active turn); status must reflect the active turn on top of this.
    persisted_percent = context.request_tokens(context.model_messages(SYSTEM_PROMPT), tools) * 100 // context.request_token_budget()
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)

    context = StatusReport.of(loop).snapshot.context

    # status recomputes context_percent with the active turn included, so it exceeds persisted-only
    # and the report shows that recomputed value (not a stale or persisted-only figure).
    assert s.state.context_percent > persisted_percent
    assert (context.percent, context.reported) == (s.state.context_percent, False)


def test_status_context_uses_last_real_tokens_when_available(tmp_path):
    s = session(tmp_path)
    s.settings.max_context_tokens = 100_000
    estimate_percent = 61  # what the estimate would claim before the call recomputes it
    s.state.context_percent = estimate_percent
    s.usage.last_prompt_tokens = 20_000  # provider reported 20K for the last request
    s.usage.last_prompt_budget = 80_000  # the budget that request was prepared against
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)

    report = StatusReport.of(loop)
    assert (report.snapshot.context.used, report.snapshot.context.budget, report.snapshot.context.percent) == (20_000, 80_000, 25)
    # A provider's figure carries no estimate marker, and says where it came from.
    assert value(report.text(), "used") == "20.0K / 80.0K · 25%  last request, reported"

    # The recorded budget, not today's configuration, stays the denominator.
    s.config.provider.max_tokens = 60_000
    assert StatusReport.of(loop).snapshot.context.budget == 80_000


def test_status_usage_reports_cache_ratios_and_writes(tmp_path):
    s = session(tmp_path)
    s.usage.calls = 3
    s.usage.last_cached_prompt_tokens = 76_000
    s.usage.last_cache_write_prompt_tokens = 1_200
    s.usage.last_prompt_tokens = 76_100
    s.usage.cached_prompt_tokens = 83_400
    s.usage.cache_write_prompt_tokens = 4_500
    s.usage.prompt_tokens = 100_000
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)

    usage = dict(StatusReport.of(loop).snapshot.usage[0].rows)

    assert usage == {
        "requests": "3",
        "input": "100.0K",
        "output": "0",
        "cache read": "83%",
        "last cache read": "100%",
        "cache write": "4.5K",
    }


def test_status_breakdown_is_an_estimate_of_disjoint_parts(tmp_path):
    s = session(tmp_path)
    s.settings.max_context_tokens = 100_000
    s.messages = [{"role": "user", "content": "x" * 40_000}]
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)

    context = StatusReport.of(loop).snapshot.context
    parts = dict(context.parts)

    assert list(parts) == ["system", "tools", "mcp tools", "instructions", "skills", "messages"]
    assert parts["messages"] >= 10_000 and parts["system"] > 0 and parts["tools"] > 0
    # Without MCP, its part is absent from the legend rather than shown as a measured zero.
    assert parts["mcp tools"] == 0
    text = StatusReport.of(loop).text(100)
    assert value(text, "messages").startswith("■") and "mcp tools" not in text
    assert value(text, "total") == "~" + Text.abbreviate_count(sum(parts.values()))
    assert value(text, "compacts at") == Text.abbreviate_count(context.threshold)
    assert "Next request · estimated" in text


def test_context_bar_parts_are_told_apart_without_losing_their_share(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Rules\n" + "Keep it short.\n" * 400, encoding="utf-8")
    s = session(tmp_path)  # instructions present, MCP tools absent: tools sat beside instructions
    s.settings.max_context_tokens = 60_000
    s.messages = [{"role": "user", "content": "x" * 60_000}]
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)
    tabs = StatusTabs(StatusReport.of(loop).snapshot)
    parts = tabs.bar(60)
    drawn = [style for style, text in parts if "█" in text]

    # Every cell is accounted for, solid, so shares stay proportional and the bar unbroken.
    assert sum(get_cwidth(text) for _, text in parts) == 60 and not any("▐" in text for _, text in parts)
    assert len(drawn) >= 3
    # The legend's squares wear the same colors as the bar.
    squares = [style for row in tabs.context(76) for style, text in row if text == "■ "]
    assert drawn == [square for square in squares if square in drawn]


@pytest.mark.parametrize("theme", list(Theme.BUILTIN))
def test_context_bar_colors_stay_apart_in_every_theme(theme, monkeypatch):
    """Theme roles read as one color in several themes (slate drew two blues side by side): the
    parts take a generated series instead, apart in hue and readable on the background."""
    monkeypatch.setattr(Theme, "_mode", theme)
    colors = Theme.series(6)
    hues = [colorsys.rgb_to_hsv(*(channel / 255 for channel in Theme.rgb(color)))[0] * 360 for color in colors]
    background = Theme.active().background or ("#ffffff" if Theme.appearance() == "light" else "#1e1e1e")

    assert len(set(colors)) == 6
    # Neighbors in the bar sit far apart on the color wheel; every color stands off the background.
    assert all(min(abs(a - b), 360 - abs(a - b)) >= 90 for a, b in pairwise(hues))
    assert all(contrast(color, background) >= 3 for color in colors)


def test_status_marks_an_estimate_over_the_compaction_threshold(tmp_path):
    s = session(tmp_path)
    s.settings.max_context_tokens = 40_000
    s.messages = [{"role": "user", "content": "x" * 200_000}]
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)

    report = StatusReport.of(loop)
    over = sum(tokens for _, tokens in report.snapshot.context.parts) - report.snapshot.context.threshold

    assert over > 0
    assert value(report.text(100), "over by") == Text.abbreviate_count(over)


def value(text: str, label: str) -> str:
    """The value on the first row of a framed report that `label` opens."""
    for line in text.splitlines():
        inside = line.strip().strip("│").strip()
        if inside.startswith(label + "  "):
            return inside.removeprefix(label).strip()
    raise AssertionError(f"no {label!r} row in:\n{text}")


def test_status_values_wear_theme_roles_and_unavailable_ones_stay_quiet(tmp_path):
    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)

    def role(tab, value):
        """The theme role of the fragment that shows `value` on `tab`."""
        tabs = StatusTabs(StatusReport.of(loop).snapshot)
        return next(style.split("class:role.")[1].split()[0] for row in tabs.rows(tab, 76) for style, text in row if text.strip().endswith(value))

    assert role("Usage", "none yet") == "muted"
    s.usage.add({"prompt_tokens": 50_000, "completion_tokens": 900, "prompt_tokens_details": {"cached_tokens": 40_000}}, budget=100_000)
    # Measured values stand out in the theme's roles; the words between them stay muted.
    assert (role("Overview", "900"), role("Overview", "80%"), role("Overview", "cached")) == ("syntax_number", "status_cache", "muted")
    assert role("Overview", "50.0K") == "status_context"  # the reading comes first, in the context color
    assert role("Overview", "main") == "status_agent" and role("Overview", s.config.active_provider) == "status_provider"
    assert (role("Usage", "50.0K"), role("Usage", "80%")) == ("syntax_number", "status_cache")
    # The reading turns with the statusbar's pressure thresholds.
    s.usage.last_prompt_tokens = 95_000
    assert role("Overview", "95%") == "error"


def status_loop(tmp_path) -> CommandLoop:
    s = session(tmp_path)
    s.state.goal = "a goal long enough to wrap " * 4
    s.usage.add({"prompt_tokens": 5_000, "completion_tokens": 100}, budget=100_000)
    return CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)


@pytest.mark.parametrize("width", [120, 80, 44, 21, 18])
def test_printed_status_frames_every_tab_at_any_width(tmp_path, width):
    loop = status_loop(tmp_path)
    lines = StatusReport.of(loop).text(width).splitlines()
    text = "\n".join(lines)

    # One frame around everything, never wider than the pane, however narrow (regression: at 21
    # columns and under it overflowed by up to four).
    assert max(get_cwidth(line) for line in lines) <= width
    assert lines[0].strip().strip("╭─╮") == "" and lines[-1].strip().strip("╰─╯") == ""
    assert all(line.strip().startswith("│") for line in lines[1:-1])
    if width < 44:
        return  # rows wrap under their own column; layout and order are checked wider
    # Overview first, then each tab once, in order; the session id appears once.
    assert value(text, "agent") == "main · " + loop.session.state.last_turn_status
    order = [text.index(title) for title in ("agent ", "Context", "Next request", "Usage", "All requests", "Activity", "Session", "docs")]
    assert order == sorted(order)
    if width >= 80:  # narrower, the id wraps under its own column
        assert text.count(loop.session.uid) == 1


def test_status_shares_one_label_column_across_tabs(tmp_path):
    loop = status_loop(tmp_path)
    tabs = StatusTabs(StatusReport.of(loop).snapshot)

    for tab in TABS:
        for row in tabs.rows(tab, 80):
            first = row[0][1] if row else ""
            if first.endswith("  ") or first.isspace():
                # A padded label or a wrapped line's indent: one column for every tab alike.
                assert len(first) == tabs.column


def test_status_frame_is_a_quiet_structure_line(tmp_path):
    loop = status_loop(tmp_path)

    styles = {style for style, text in StatusReport.of(loop).fragments(100) if "╭" in text or "╰" in text}

    assert styles == {Theme.fg("muted")}


def test_status_view_opens_on_a_concise_overview_and_switches_tabs(tmp_path):
    loop = status_loop(tmp_path)
    view = StatusView(loop, StatusReport.of(loop).snapshot)

    def screen():
        return "".join(fragment[1] for fragment in view.fragments())

    overview = screen()
    assert value(overview, "usage") == "1 request · 5.0K in · 100 out · 0% cached"
    assert "workspace" not in overview and "Activity" not in overview
    rows = len(overview.splitlines())
    for key, expected in (("l", "last request, reported"), ("l", "All requests"), ("4", "workspace"), ("l", "context ")):
        assert view.handle_key(key, key) is TUI_MODAL_PENDING
        assert expected in screen()
        # Every tab keeps the frame's height, so switching never moves it.
        assert len(screen().splitlines()) == rows
    assert view.handle_key("escape") is None


async def test_status_command_shows_the_view_interactively_and_prints_otherwise(tmp_path):
    loop = status_loop(tmp_path)
    blocks = []
    loop.presentation.ui.emit_block = blocks.append
    assert await loop.command("/status") == (True, False)
    [block] = blocks
    assert isinstance(block, WidthDependent) and block.text(60) != block.text(120)

    shown = []

    class Modal:
        app = None
        modal_window = None

        async def show_modal(self, fragments_fn, key_fn, **kwargs):
            shown.append("".join(fragment[1] for fragment in fragments_fn()))
            return key_fn("escape")

    loop.presentation.tui = Modal()
    loop.interactive_input = True
    assert await loop.command("/status") == (True, False)
    assert len(blocks) == 1 and "Overview" in shown[0]


def test_session_from_config_file_theme_param(tmp_path):
    cfg = tmp_path / "wizolt.toml"
    cfg.write_text('[runtime]\ntheme = "light"\n')
    s = create_session(path=str(cfg), theme="dark")
    assert s.settings.theme == "dark"

    s2 = create_session(path=str(cfg))
    assert s2.settings.theme == "light"

    s3 = create_session(path=str(cfg), theme="")
    assert s3.settings.theme == "light"
