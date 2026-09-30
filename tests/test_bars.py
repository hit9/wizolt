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
