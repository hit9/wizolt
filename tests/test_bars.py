"""UI templates: observable layout, literal interpolation and rejected executable input."""

from itertools import pairwise

import pytest
from prompt_toolkit.utils import get_cwidth

from wizolt.ui.bars import DIVIDER_PRESETS, FIELDS, STATUS_PRESETS, SWEEPS, Expression, Sweep, Template


def text(parts):
    return "".join(value for _, value in parts)


@pytest.mark.parametrize(
    "formula",
    [
        '__import__("os").system("true")',
        "x.__class__",
        'open("/tmp/evil", "w")',
        "[x for x in (1, 2)]",
        "(lambda: 1)()",
        "x[0]",
        "2 ** 999",
        "1e999",
        "exp(999)",
        '"x" * 999999999',
        "1 / 0",
        "sqrt(-1)",
    ],
)
def test_sweeps_reject_code_and_unbounded_arithmetic(formula):
    with pytest.raises(ValueError):
        Sweep(formula)


def test_expression_dotted_fields_are_keys_not_attribute_access():
    expression = Expression("agent.state == 'running' and context.percent >= 80", FIELDS)
    assert expression.evaluate({"agent.state": "running", "context.percent": 85})
    assert not expression.evaluate({"agent.state": "idle", "context.percent": 85})
    with pytest.raises(ValueError, match="unknown field"):
        Expression("agent.__class__", FIELDS)


def test_sweep_motion_and_runtime_domain_errors():
    sweep = Sweep("exp(-((x - pingpong(t * 10, w)) / 2) ** 2)")
    assert sweep.brightness(10, 1, 80) == 1
    assert sweep.brightness(20, 1, 80) < 0.01
    assert sweep.brightness(70, 9, 80) == 1
    broken = Sweep("1 / (1 - t)")
    assert broken.brightness(0, 1, 80) == 0
    assert broken.error
    assert Sweep("10").brightness(0, 0, 80) == 1
    assert Sweep("-1").brightness(0, 0, 80) == 0


def test_template_interpolation_cannot_inject_markup_or_terminal_controls():
    template = Template("[accent]{model}[/] {{name}} [[text]]")
    parts = template.render({"model": "\x1b[2J[error]{provider}\n"}, 100, {"accent": "fg:#abcdef"})
    assert text(parts) == " [2J[error]{provider}  {name} [text]"
    assert parts[0][0].strip() == "fg:#abcdef"
    assert "\x1b" not in text(parts) and "\n" not in text(parts)


def test_conditions_optional_spans_alignment_and_wide_characters():
    template = Template("{model}{% optional priority=10 %} · {reasoning}{% endoptional %}{>}{% if yolo %}YOLO{% else %}ok{% endif %}")
    values = {"model": "模型", "reasoning": "medium", "yolo": False}
    assert text(template.render(values, 10, {})) == "模型    ok"
    assert get_cwidth(text(template.render(values, 6, {}))) == 6
    assert text(template.render(values, 3, {})).endswith("ok")


def test_patterned_fills_share_the_width_and_center_wide_labels():
    template = Template("{fill:─·} 模型 {fill:·─}")
    assert text(template.render({}, 14, {})) == "─·─· 模型 ·─·─"
    assert text(template.render({}, 15, {})) == "─·─·─ 模型 ·─·─"
    assert text(Template("A{>}B{>}C").render({}, 8, {})) == "A   B  C"
    assert text(Template("L{fill:ab}M{fill:cd}RIGHT").render({}, 6, {})) == "LRIGHT"
    for width in range(20):
        assert get_cwidth(text(template.render({}, width, {}))) <= width


@pytest.mark.parametrize("pattern", ["", "a" * 33, "界", "a\u0301", "\x1b", "\n", "\t", "\x9b", "\u200b"])
def test_fill_patterns_reject_controls_wide_and_combining_characters(pattern):
    with pytest.raises(ValueError, match="fill needs"):
        Template("{fill:" + pattern + "}")


def test_sweep_crosses_multiple_patterned_fills_without_touching_the_label():
    template = Template("[rule]{fill:─·}[/][label]AB[/][rule]{fill:·─}[/]")
    parts = template.render({}, 10, {"rule": "rule", "label": "label"}, sweep=Sweep("x / max(1, w - 1)"), ramp=("dim", "bright"))
    assert parts == [("dim", "─·─·"), ("label", "AB"), ("bright", "·─·─")]


def test_normalized_sweep_skips_labels_without_changing_column_coordinates():
    template = Template("[label]{label}[/][rule]{fill:─}[/]")
    styles = {"label": "label", "rule": "rule"}
    # A long label must not hide a light at the start of the fill track.
    parts = template.render({"label": "L" * 16}, 20, styles, sweep=Sweep("u == 0"), ramp=("dim", "bright"))
    assert parts == [("label", "L" * 16), ("bright", "─"), ("dim", "───")]
    parts = template.render({"label": "L" * 16}, 20, styles, sweep=Sweep("x == 16 and w == 20"), ramp=("dim", "bright"))
    assert parts == [("label", "L" * 16), ("bright", "─"), ("dim", "───")]
    centered = Template("[rule]{fill:─}[/][label]long label[/][rule]{fill:─}[/]")
    parts = centered.render({}, 18, styles, sweep=Sweep("u"), ramp=tuple(str(i) for i in range(8)))
    assert parts == [(str(i), "─") for i in range(4)] + [("label", "long label")] + [(str(i), "─") for i in range(4, 8)]


def test_normalized_sweeps_have_distinct_shapes_and_width_independent_timing():
    comet = Sweep("preset:comet")
    # At 1.5s it crosses the midpoint moving right: bright head, fading left tail, dark ahead.
    assert comet.brightness(0, 1.5, 80, u=0.5) > 0.99
    assert comet.brightness(0, 1.5, 80, u=0.4) > 0.5
    assert comet.brightness(0, 1.5, 80, u=0.6) < 0.01
    assert comet.brightness(0, 4.5, 80, u=0.6) > 0.5  # The tail reverses direction.
    ripple = Sweep("preset:ripple")
    assert ripple.brightness(0, 0, 80, u=0.5) > 0.99
    assert ripple.brightness(0, 1.5, 80, u=0.25) > 0.6
    assert ripple.brightness(0, 1.5, 80, u=0.25) == ripple.brightness(0, 1.5, 80, u=0.75)
    assert ripple.brightness(0, 1.5, 80, u=0.5) < 0.01
    aurora = Sweep("preset:aurora")
    levels = [aurora.brightness(0, 1, 80, u=i / 100) for i in range(101)]
    assert max(levels) - min(levels) > 0.2
    assert max(abs(a - b) for a, b in pairwise(levels)) < 0.05
    for sweep in (comet, ripple, aurora):
        assert sweep.brightness(0, 1, 40, u=0.4) == sweep.brightness(0, 1, 200, u=0.4)


def test_powerline_joins_use_adjacent_backgrounds_and_restore_nested_styles():
    template = Template("[a] A {join:}[b] B [/][reset]{>}{join:}[a] C [reset]")
    parts = template.render({}, 30, {"a": "fg:#000000 bg:#ff0000", "b": "fg:#ffffff bg:#0000ff"})
    assert ("fg:#ff0000 bg:#0000ff", "") in parts
    assert ("fg:#ff0000","") in parts
    assert get_cwidth(text(parts)) == 30


@pytest.mark.parametrize("source", ["{% if yolo %}bad", "{unknown}", "{model:999999999f}", "{% exec foo %}", "[/]", "{% if yolo %}[a]{% endif %}"])
def test_invalid_templates_report_errors(source):
    with pytest.raises(ValueError):
        Template(source)


def test_every_preset_renders_bounded_idle_and_running_rows():
    values = dict.fromkeys(FIELDS, 0)
    values.update(
        model="model", provider="provider", reasoning="medium", label="working (12s)", rate="12 tok/s", **{"mcp.label": "mcp 2"}
    )
    for presets in (STATUS_PRESETS, DIVIDER_PRESETS):
        for name in presets:
            template = Template("preset:" + name, presets)
            styles = dict.fromkeys(template.styles, "fg:#ffffff bg:#000000")
            for running in (False, True):
                values["running"] = running
                for width in (0, 1, 8, 30, 80):
                    parts = template.render(values, width, styles)
                    assert get_cwidth(text(parts)) <= width, name
    for name in SWEEPS:
        sweep = Sweep("preset:" + name)
        assert 0 <= sweep.brightness(10, 2, 80) <= 1


@pytest.mark.parametrize("name", [name for name in DIVIDER_PRESETS if name != "frame"])
@pytest.mark.parametrize("width", [0, 1, 3, 4, 8, 80])
def test_idle_divider_presets_have_no_gaps(name, width):
    template = Template("preset:" + name, DIVIDER_PRESETS)
    styles = dict.fromkeys(template.styles, "fg:#ffffff")
    rule = {"dashed": "╌", "dotted": "┈", "double": "═"}.get(name, "─")
    assert text(template.render({"running": False}, width, styles)) == rule * width


@pytest.mark.parametrize("name", SWEEPS)
def test_builtin_sweeps_stay_defined_and_animate_at_different_widths(name):
    sweep = Sweep("preset:" + name)
    for width in (0, 1, 12, 80, 500):
        levels = [sweep.brightness(x, t, width) for t in (0, 0.1, 0.7, 3, 300, 86400) for x in (0, width // 2, width)]
        assert not sweep.error
        assert all(0 <= level <= 1 for level in levels)
        assert len(set(levels)) > 1 if name != "none" else set(levels) == {0}


def test_structured_dividers_keep_their_shape_and_full_activity_label():
    from wizolt.ui.render import Theme

    idle = {"running": False}
    samples = {"capsule": "─" * 12, "frame": "╭──────────╮"}
    for name, expected in samples.items():
        template = Template("preset:" + name, DIVIDER_PRESETS)
        styles = Theme.bar_styles(template.styles)
        assert text(template.render(idle, 12, styles)) == expected
        for width in range(50):
            assert get_cwidth(text(template.render(idle, width, styles))) == width
        label = "working (12s · 42 tok/s) [ 2 queued ]"
        values = {"running": True, "label": label}
        rendered = text(template.render(values, 80, styles))
        assert label in rendered
        assert get_cwidth(rendered) == 80
        if name == "capsule":
            assert abs(len(rendered) - rendered.index(label) * 2 - len(label)) <= 1
        for width in range(50):
            assert get_cwidth(text(template.render(values, width, styles))) == width


def test_rail_separates_activity_and_metrics_and_keeps_queue_kinds_distinct():
    from wizolt.ui.render import Theme

    template = Template("preset:rail", DIVIDER_PRESETS)
    values = dict.fromkeys(FIELDS, 0)
    values.update(running=True, activity="thinking", elapsed=12, rate="42 tok/s", spinner="● ", **{"queue.followup": 2, "queue.next_turn": 1})
    styles = Theme.bar_styles(template.styles)
    rendered = text(template.render(values, 100, styles))
    assert rendered.startswith("● thinking ")
    assert rendered.endswith("12s · 42 tok/s · 2 queued · 1 next turn")
    for width in range(100):
        assert get_cwidth(text(template.render(values, width, styles))) <= width
    values.update(rate="", reset_pending=True)
    rendered = text(template.render(values, 100, styles))
    assert "tok/s" not in rendered and rendered.endswith("reset pending")


@pytest.mark.parametrize("name", STATUS_PRESETS)
def test_status_presets_preserve_identity_and_resolve_theme_styles(name):
    from wizolt.ui.bars import BarLayout
    from wizolt.ui.render import Theme

    layout = BarLayout()
    assert not layout.configure({"statusbar": "preset:" + name}, Theme.bar_styles)
    values = dict.fromkeys(FIELDS, 0)
    values.update(model="my-model", provider="test", reasoning="high", yolo=True, **{"agent.name": "reviewer"})
    rendered = text(layout.render("statusbar", values, 200, Theme.bar_styles))
    assert "my-model" in rendered and "yolo" in rendered and "reviewer" in rendered
    if name != "minimal":
        assert "test" in rendered
    assert not layout.errors


@pytest.mark.parametrize("name", STATUS_PRESETS)
def test_all_status_presets_show_group_counts_only_when_children_exist(name):
    from wizolt.ui.render import Theme

    template = Template("preset:" + name, STATUS_PRESETS)
    values = dict.fromkeys(FIELDS, 0)
    values.update(model="model", provider="test", **{"agent.name": "main", "agents.count": 1})
    styles = Theme.bar_styles(template.styles)
    assert "agents " not in text(template.render(values, 200, styles))
    values.update(**{"agents.count": 4, "agents.running": 2, "agents.waiting": 1})
    rendered = text(template.render(values, 200, styles))
    assert "main" in rendered and "agents 4 · run 2 · wait 1" in rendered
    if name == "default":
        assert "[main]" in rendered and "[agents 4 · run 2 · wait 1]" in rendered
    else:
        assert "[main]" not in rendered and "[agents" not in rendered
    for width in (0, 1, 15, 30, 60, 100):
        assert get_cwidth(text(template.render(values, width, styles))) <= width
    values["agents.waiting"] = 0
    rendered = text(template.render(values, 200, styles))
    assert "agents 4 · run 2" in rendered and "wait" not in rendered


def test_custom_status_template_can_always_show_total_and_runtime_counts():
    template = Template("[status_agent]{agents.count} agents · {agents.running} running · {agents.waiting} waiting[/]")
    assert text(template.render({"agents.count": 1, "agents.running": 0, "agents.waiting": 0}, 80, {"status_agent": "fg:#abcdef"})) == "1 agents · 0 running · 0 waiting"


@pytest.mark.parametrize("name", ["default", "minimal", "compact", "brackets"])
def test_single_sided_status_presets_do_not_spread_across_the_terminal(name):
    template = Template("preset:" + name, STATUS_PRESETS)
    values = dict.fromkeys(FIELDS, 0)
    values.update(model="model", provider="test", reasoning="high", **{"mcp.label": "mcp 0"})
    styles = dict.fromkeys(template.styles, "")
    assert text(template.render(values, 200, styles)) == text(template.render(values, 300, styles))


@pytest.mark.parametrize("name", ["split", "monitor", "blocks", "vim", "lualine", "powerline"])
def test_segmented_presets_keep_left_identity_before_right_metrics_on_narrow_terminals(name):
    from wizolt.ui.render import Theme

    template = Template("preset:" + name, STATUS_PRESETS)
    values = dict.fromkeys(FIELDS, 0)
    values.update(model="model", provider="test", reasoning="high", **{"agent.name": "main", "context.percent": 42, "mcp.label": "mcp 3"})
    for width in (20, 30, 40, 80, 160):
        rendered = text(template.render(values, width, Theme.bar_styles(template.styles)))
        assert get_cwidth(rendered) == width
        assert "main" in rendered and "model" in rendered
        if width >= 80:
            assert "ctx 42%" in rendered


@pytest.mark.parametrize("agent", ("main", "reviewer"))
def test_lualine_shows_provider_model_and_effort_with_distinct_styles(agent):
    from wizolt.ui.render import Theme

    template = Template("preset:lualine", STATUS_PRESETS)
    values = dict.fromkeys(FIELDS, 0)
    values.update(provider="zai", model="glm-5.3", reasoning="high", yolo=True, **{"agent.name": agent})
    parts = template.render(values, 160, Theme.bar_styles(template.styles))
    rendered = text(parts)
    assert "CHAT" not in rendered
    assert agent in rendered and "yolo" in rendered
    assert "[yolo]" not in rendered and f"[{agent}]" not in rendered
    assert rendered.index(agent) < rendered.index("glm-5.3") < rendered.index("zai") < rendered.index("high")
    styles = [next(style for style, value in parts if label in value) for label in ("zai", "glm-5.3", "high")]
    assert len(set(styles)) == 3


def test_context_meter_fills_to_the_nearest_cell():
    template = Template("preset:minimal", STATUS_PRESETS)
    styles = dict.fromkeys(template.styles, "")
    for percent, filled in ((0, 0), (9, 0), (10, 1), (37, 2), (89, 4), (90, 5), (100, 5)):
        rendered = text(template.render({"model": "m", "context.percent": percent}, 80, styles))
        assert rendered == "m " + "▰" * filled + "▱" * (5 - filled) + f" {percent}%"


def test_blocks_join_rectangles_without_unpainted_gaps():
    from wizolt.ui.render import Theme

    template = Template("preset:blocks", STATUS_PRESETS)
    values = {"agent.name": "main", "model": "model", "provider": "test", "reasoning": "high", "context.percent": 37, "cache.percent": 88}
    parts = template.render(values, 120, Theme.bar_styles(template.styles))
    # Only the alignment space between left and right groups is transparent.
    gaps = [(style, value) for style, value in parts if value.isspace() and "bg:" not in style]
    assert len(gaps) == 1 and len(gaps[0][1]) > 1


@pytest.mark.parametrize("name", ["powerline", "lualine"])
def test_arrow_presets_give_identity_settings_and_group_counts_separate_segments(name):
    from prompt_toolkit.styles import Style

    from wizolt.ui.render import Theme

    template = Template("preset:" + name, STATUS_PRESETS)
    values = {
        "agent.name": "reviewer", "model": "glm-5.3", "provider": "zai", "reasoning": "high", "yolo": True,
        "agents.count": 3, "agents.running": 2, "agents.waiting": 1, "context.percent": 37,
        "cache.percent": 88, "mcp.label": "mcp 2", "skills.count": 3,
    }
    parts = template.render(values, 240, Theme.bar_styles(template.styles))
    rendered = text(parts)
    labels = ("reviewer", "glm-5.3", "zai", "high", "yolo", "agents 3")
    for before, after in pairwise(labels):
        assert "" in rendered[rendered.index(before) + len(before):rendered.index(after)]
    style = Style([])
    backgrounds = [style.get_attrs_for_style_str(next(spec for spec, label in parts if word in label)).bgcolor for word in labels[:-1]]
    assert all(backgrounds) and all(left != right for left, right in pairwise(backgrounds))
    for width in (24, 28, 40):
        narrow = text(template.render(values, width, Theme.bar_styles(template.styles)))
        assert "reviewer" in narrow and "glm-5.3" in narrow
        assert "" not in narrow and get_cwidth(narrow) <= width


@pytest.mark.parametrize("name", ["minimal", "split", "compact", "brackets", "monitor", "blocks", "vim"])
def test_restyled_statusbars_color_context_by_pressure(name):
    from wizolt.ui.render import Theme

    template = Template("preset:" + name, STATUS_PRESETS)
    styles = Theme.bar_styles(template.styles)
    values = dict.fromkeys(FIELDS, 0)
    values.update(model="m", provider="p", reasoning="high", **{"mcp.label": "mcp 0"})

    def context_style(percent):
        values["context.percent"] = percent
        return next(style for style, value in template.render(values, 200, styles) if f"{percent}%" in value)

    assert len({context_style(37), context_style(76), context_style(94)}) == 3
    assert "ansired" in context_style(94)


def test_layout_reload_is_atomic_and_rejects_style_injection():
    from wizolt.ui.bars import BarLayout
    from wizolt.ui.render import Theme

    layout = BarLayout()
    assert not layout.load({"statusbar": {"format": "preset:minimal"}}, Theme.bar_styles)
    original = dict(layout.sources)
    assert layout.load({"divider": {"sweep": '__import__("os")'}}, Theme.bar_styles)
    assert layout.sources == original
    for style in ("class:evil", "fg=red bg:blue", "fg=#fff\x1b[2J", '__import__("os")'):
        assert layout.configure({"statusbar": "[" + style + "]{model}[/]"}, Theme.bar_styles)
        assert layout.sources == original
    assert layout.load({"divider": {"sweep": 99}}, Theme.bar_styles)
    assert layout.load({"statusbar": False}, Theme.bar_styles)


def test_layout_configuration_renders_real_fields_and_idle_divider(tmp_path):
    from tui_harness import loop

    from wizolt.config import Config

    command_loop = loop(tmp_path)
    command_loop.session.config.ui = Config.from_dict(
        {
            "ui": {
                "statusbar": {"format": "{provider}/{model}{>}ctx {context.percent}%"},
                "divider": {"format": "{% if running %}{activity}{% else %}idle{% endif %}", "sweep": "preset:none"},
            }
        }
    ).ui
    command_loop.configure_theme()
    assert not command_loop.theme_problems
    bar = command_loop.presentation.status_bar
    assert bar.values()["model"] in text(bar.fragments())
    assert text(command_loop.view.idle_divider_fragments()) == "idle"
    assert text(command_loop.view.queue_divider_fragments()) == "working"


def test_ui_config_save_preserves_comments_mode_and_other_tables(tmp_path):
    import os
    import tomllib

    from wizolt.config import ConfigFile

    path = tmp_path / "config.toml"
    path.write_text('# keep me\n[runtime]\ntheme = "auto"\n')
    path.chmod(0o600)
    ConfigFile.set_ui_value(str(path), ("ui", "divider"), "format", "preset:powerline")
    ConfigFile.set_ui_value(str(path), ("ui", "divider"), "sweep", "preset:none")
    document = tomllib.loads(path.read_text())
    assert document["ui"]["divider"] == {"format": "preset:powerline", "sweep": "preset:none"}
    assert document["runtime"]["theme"] == "auto" and "# keep me" in path.read_text()
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_theme_highlights_are_validated_and_follow_theme_switches(tmp_path, monkeypatch):
    from wizolt.ui.render import Theme

    monkeypatch.setattr(Theme, "_custom", {})
    monkeypatch.setattr(Theme, "_mode", "dark")
    (tmp_path / "mine.toml").write_text(
        'base = "slate"\n[highlights."status.model"]\nfg = "#123456"\nbg = "#abc"\nbold = true\n[highlights.bad]\nfg = "#fff bg:red"\n'
    )
    problems = Theme.load_custom(str(tmp_path))
    assert len(problems) == 1 and "bad" in problems[0]
    Theme.set_mode("mine")
    assert Theme.bar_styles({"status.model"})["status.model"] == "fg:#123456 bg:#aabbcc bold"
    Theme.set_mode("dark")
    assert "#123456" not in Theme.bar_styles({"status.model"})["status.model"]


@pytest.mark.parametrize("source", ["[accent]" * 40 + "{model}", "{model}" * 600], ids=["style-depth", "token-count"])
def test_template_complexity_is_bounded_before_rendering(source):
    with pytest.raises(ValueError, match="exceeds"):
        Template(source)


@pytest.mark.parametrize("name", STATUS_PRESETS)
def test_status_context_colors_change_only_at_warning_thresholds(name):
    from wizolt.ui.render import Theme

    template = Template("preset:" + name, STATUS_PRESETS)
    values = dict.fromkeys(FIELDS, 0)
    values.update(provider="p", model="m", reasoning="high")
    colors = []
    for percent in (37, 69, 70, 89, 90, 95):
        values["context.percent"] = percent
        parts = template.render(values, 160, Theme.bar_styles(template.styles))
        colors.append(next(style for style, value in parts if str(percent) + "%" in value))
    assert colors[0] == colors[1]
    assert colors[2] == colors[3]
    assert colors[4] == colors[5]
    assert len(set(colors)) == 3
