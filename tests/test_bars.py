"""UI templates: observable layout, literal interpolation and rejected executable input."""

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
    expression = Expression("worker.active and context.percent >= 80", FIELDS)
    assert expression.evaluate({"worker.active": True, "context.percent": 85})
    assert not expression.evaluate({"worker.active": False, "context.percent": 85})
    with pytest.raises(ValueError, match="unknown field"):
        Expression("worker.__class__", FIELDS)


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


def test_powerline_joins_use_adjacent_backgrounds_and_restore_nested_styles():
    template = Template("[a] A {join:}[b] B [/][reset]{>}{join:}[a] C [reset]")
    parts = template.render({}, 30, {"a": "fg:#000000 bg:#ff0000", "b": "fg:#ffffff bg:#0000ff"})
    assert ("fg:#ff0000 bg:#0000ff", "") in parts
    assert ("fg:#ff0000 bg:default", "") in parts
    assert get_cwidth(text(parts)) == 30


@pytest.mark.parametrize("source", ["{% if yolo %}bad", "{unknown}", "{model:999999999f}", "{% exec foo %}", "[/]", "{% if yolo %}[a]{% endif %}"])
def test_invalid_templates_report_errors(source):
    with pytest.raises(ValueError):
        Template(source)


def test_every_preset_renders_bounded_idle_and_running_rows():
    values = dict.fromkeys(FIELDS, 0)
    values.update(
        model="model", provider="provider", reasoning="medium", label="working (12s)", rate="12 tok/s", **{"mcp.label": "mcp 2", "worker.summary": ""}
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
        'base = "github-dark"\n[highlights."status.model"]\nfg = "#123456"\nbg = "#abc"\nbold = true\n[highlights.bad]\nfg = "#fff bg:red"\n'
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
