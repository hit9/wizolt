"""The status bar and divider: their facts, presets and sweeps over the shared format language.

The language itself (fields, `{% if %}`, styles, fills, joins, expressions) is `wizolt.formats`;
this module binds it to the status facts and plugin fields. No session, terminal IO or theme
ownership lives here. Callers supply values and resolved styles.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from wizolt import formats
from wizolt.formats import Expression, Fragments, Value, expand

FIELDS = frozenset(
    [
        "provider",
        "model",
        "agent.name",
        "agent.id",
        "agent.state",
        "agents.count",
        "agents.running",
        "agents.waiting",
        "reasoning",
        "yolo",
        "context.percent",
        "cache.percent",
        "mcp.count",
        "mcp.label",
        "skills.count",
        "plugins.count",
        "running",
        "activity",
        "elapsed",
        "rate",
        "spinner",
        "queue.total",
        "queue.followup",
        "queue.next_turn",
        "reset_pending",
        "label",
    ]
)


SWEEPS = {
    "none": "0",
    "comet": "exp(-((u - (1 - cos(t * 1.0471975512)) / 2) / (0.018 + 0.42 * sqrt(max(0, sin(t * 1.0471975512) * ((1 - cos(t * 1.0471975512)) / 2 - u))))) ** 2)",
    "scan": "exp(-((x - (t * 24 % (w + 12) - 6)) / 4) ** 2)",
    "breathe": "(1 - cos(t * 2)) / 2",
    "reverse": "exp(-((x - (w + 6 - t * 24 % (w + 12))) / 4) ** 2)",
    "wave": "(1 + sin(x / 6 - t * 3)) / 2",
    "twin": "max(exp(-((x - pingpong(t * 16, w)) / 3) ** 2), exp(-((x - w + pingpong(t * 16, w)) / 3) ** 2))",
    "pulse": "((1 - cos(t * 4)) / 2) ** 6",
    "ripple": "max(0, cos(abs(u - 0.5) * 12.5663706144 - t * 2.0943951024)) ** 6 * (0.35 + 0.65 * (1 - abs(u - 0.5) * 2))",
    "aurora": "0.08 + 0.92 * (0.5 + 0.5 * sin(u * 7 - t * 0.5235987756)) * (0.5 + 0.5 * cos(u * 4 + t * 1.0471975512))",
}


class Sweep:
    def __init__(self, source: str):
        self.source = expand(source, SWEEPS)
        self.expression = Expression(self.source, frozenset(("x", "t", "w", "u")))
        self.error = ""
        self.brightness(0, 0, 80)
        if self.error:
            raise ValueError(self.error)

    def brightness(self, x: float, t: float, w: float, *, u: float | None = None) -> float:
        try:
            value = float(self.expression.evaluate({"x": x, "t": t, "w": max(1, w), "u": u if u is not None else x / max(1, w - 1)}))
            if not math.isfinite(value):
                raise ValueError("non-finite brightness")
            return min(1.0, max(0.0, value))
        except (ArithmeticError, ValueError, TypeError) as error:
            self.error = f"sweep: {error}"
            return 0.0


def meter(cells: int) -> str:
    """`cells` cells filled in proportion to the context percentage, rounded to the nearest cell;
    filled cells take the surrounding style."""
    step = 100 / cells
    return "".join(f"{{% if context.percent >= {round(step * i - step / 2)} %}}▰{{% else %}}[subtle]▱[/]{{% endif %}}" for i in range(1, cells + 1))


def pressure(body: str, normal: str = "status_context", warning: str = "status.warning", error: str = "status.error bold") -> str:
    """`body` in `normal` style, turning to `warning` from 70% context and `error` from 90%."""
    return (
        f"{{% if context.percent >= 90 %}}[{error}]{body}[/]{{% else %}}"
        f"{{% if context.percent >= 70 %}}[{warning}]{body}[/]{{% else %}}[{normal}]{body}[/]{{% endif %}}{{% endif %}}"
    )


# Powerline arrows: a join after a segment points right into the next one; a join before a
# segment points left into the previous one.
JOIN_AFTER = "{join:\ue0b0}"
JOIN_BEFORE = "{join:\ue0b2}"


def joined(chip: str, *, right: bool) -> str:
    """`chip` with its join on the side facing its neighbor: before it for a right-edge group."""
    return JOIN_BEFORE + chip if right else chip + JOIN_AFTER


def segment(body: str, style: str, *, priority: int = 0, when: str = "", right: bool = False) -> str:
    """One complete segment, including its join, disappears as a unit on a narrow row."""
    source = joined(f"[{style}] {body} [/]", right=right)
    if priority:
        source = f"{{% optional priority={priority} %}}{source}{{% endoptional %}}"
    return f"{{% if {when} %}}{source}{{% endif %}}" if when else source


AGENT_GROUP = (
    "{% if agents.count > 1 %}{% optional priority=15 %}[status_agent]agents {agents.count} · run {agents.running}"
    "[/][status_mcp] │ [/]{% endoptional %}{% endif %}"
)
PLUGIN_GROUP = "{% if plugins.count %}{% optional priority=5 %}[status_mcp]plugins {plugins.count} │ [/]{% endoptional %}{% endif %}"
# The service counts every preset carries: less prominent than identity or usage, so the
# first group to elide on a narrow row (the same priority as the plugins count).
SERVICE_COUNTS = "{mcp.label} · skills {skills.count}"
IDENTITY = (
    "{% if agent.name %}[status_agent bold]{agent.name}[/][status_mcp] / [/]{% endif %}"
    "{% if yolo %}[status_yolo bold]yolo[/][status_mcp] · [/]{% endif %}" + AGENT_GROUP + PLUGIN_GROUP
)
# Segmented presets own their surfaces, including identity and group counts; a row is cut by
# background rectangles, never by inline badges or text dividers. Blocks carries the idea to the
# extreme: every identity part is its own solid rectangle.
BLOCKS_IDENTITY = (
    "{% if agent.name %}[status.agent] {agent.name} [reset]{% endif %}"
    "{% if yolo %}{% optional priority=40 %}[status.yolo] yolo [reset]{% endoptional %}{% endif %}"
    "{% if agents.count > 1 %}{% optional priority=15 %}[status.detail] agents {agents.count} run {agents.running} [reset]{% endoptional %}{% endif %}"
    "{% if plugins.count %}{% optional priority=5 %}[status.detail] plugins {plugins.count} [reset]{% endoptional %}{% endif %}"
)
SEGMENT_USAGE = pressure(" ctx {context.percent}% ", "status.usage", "status.usage.warning", "status.usage.error")
SEGMENT_DETAILS = (
    segment("{provider}", "status.provider", priority=35)
    + segment("{reasoning}", "status.reason", priority=20, when="reasoning")
    + segment("yolo", "status.yolo", priority=40, when="yolo")
    + segment(
        "agents {agents.count} · run {agents.running}",
        "status.detail",
        priority=15,
        when="agents.count > 1",
    )
    + segment("plugins {plugins.count}", "status.detail", priority=5, when="plugins.count")
)


def usage_segment(split: bool) -> str:
    return "{% optional priority=30 %}" + joined(SEGMENT_USAGE, right=split) + "{% endoptional %}"


def compose(left: str, right: str, split: bool, *, separator: str = "", tail: str = "", band: bool = False) -> str:
    """Place a preset's two groups at opposite ends (`split`), or together on the left, parted by
    the preset's own `separator`. A `band` preset's surface still spans the row either way."""
    if split:
        return left + "{>}" + right + tail
    return left + separator + right + ("{>}" if band else "") + tail


# Presets own geometry, not colors. Semantic ink is shared by the transparent layouts;
# solid segments resolve the same roles against their own surface through Theme.bar_styles.
# Optional groups keep their separators inside, so shrinking never leaves empty segments.
# Each builder takes whether the usage group sits at the right edge; segments turn their joins
# toward their neighbors, so both placements stay connected.
STATUS_LAYOUTS: dict[str, Callable[[bool], str]] = {
    # Default keeps its plain text contract. Only semantic inks change here.
    "default": lambda split: compose(
        "{% if agent.name %}[status_agent bold][[{agent.name}]] [/]{% endif %}{% if yolo %}[status_yolo][[yolo]] [/]{% endif %}"
        + "{% if agents.count > 1 %}{% optional priority=25 %}[status_agent][[agents {agents.count} · run {agents.running}"
        + "]] [/]{% endoptional %}{% endif %}"
        + "[status_provider]{provider}/[/][status_model bold]{model}[/]"
        + "{% optional priority=20 %}[subtle] · [/][status_reason]{reasoning}[/]{% endoptional %}"
        + "{% optional priority=5 %}[subtle] | [/][status_services]"
        + SERVICE_COUNTS
        + "[/][status_mcp]{% if plugins.count %} · plugins {plugins.count}{% endif %}[/]{% endoptional %}",
        pressure("ctx {context.percent}%") + "{% optional priority=10 %}[status_cache] · cache {cache.percent}%[/]{% endoptional %}",
        split,
        separator="[subtle] | [/]",
    ),
    "minimal": lambda split: compose(
        IDENTITY + "[status_base bold]{model}[/]",
        "{% optional priority=5 %}[status_services]" + SERVICE_COUNTS + "[/] {% endoptional %}" + pressure(meter(5) + " {context.percent}%"),
        split,
        separator=" ",
    ),
    "split": lambda split: compose(
        "[status.split] [status_provider]▎ [/]"
        + IDENTITY
        + "{% optional priority=35 %}[status_provider]{provider}/[/]{% endoptional %}[status_model bold]{model}[/]"
        + "{% optional priority=20 %}[status_mcp] · [/][status_reason]{reasoning}[/]{% endoptional %}",
        "{% optional priority=5 %}[status_services]"
        + SERVICE_COUNTS
        + "[/][status_mcp] │ [/]{% endoptional %}"
        + "{% optional priority=30 %}"
        + pressure("{% optional priority=12 %}" + meter(8) + " {% endoptional %}ctx {context.percent}%")
        + "{% endoptional %}"
        + "{% optional priority=10 %}[status_mcp] │ [/][status_cache]cache {cache.percent}%[/]{% endoptional %}",
        split,
        separator="[status_mcp] │ [/]",
        tail=" [reset]",
        band=True,
    ),
    "compact": lambda split: compose(
        IDENTITY
        + "{% optional priority=10 %}[status_provider]{provider} [/]{% endoptional %}[status.model] {model} [/]"
        + "{% optional priority=20 %}[status_mcp] › [/][status_reason]{reasoning}[/]{% endoptional %}",
        "{% optional priority=5 %}[status_services]" + SERVICE_COUNTS + "[/][status_mcp] › [/]{% endoptional %}" + pressure("{context.percent}%"),
        split,
        separator="[status_mcp] › [/]",
    ),
    "brackets": lambda split: compose(
        IDENTITY
        + "[status_provider][[{provider}[/][status_mcp]/[/][status_model bold]{model}]][/] "
        + "{% optional priority=20 %}[status_reason][[{reasoning}]][/] {% endoptional %}",
        "{% optional priority=5 %}[status_services] [["
        + SERVICE_COUNTS
        + "]] [/] {% endoptional %}"
        + pressure("[[ctx {context.percent}%]]")
        + "{% optional priority=10 %}[status_cache] [[cache {cache.percent}%]][/]{% endoptional %}",
        split,
    ),
    "monitor": lambda split: compose(
        "[status.monitor] [status_context]◆ [/]"
        + IDENTITY
        + "{% optional priority=35 %}[status_provider]{provider}/[/]{% endoptional %}[status_model bold]{model}[/]"
        + "{% optional priority=20 %}[status_mcp]  [/][status.reason] {reasoning} [/]{% endoptional %}",
        "{% optional priority=5 %}[status_services]"
        + SERVICE_COUNTS
        + "[/][status_mcp] │ [/]{% endoptional %}"
        + "{% optional priority=30 %}"
        + pressure("ctx {context.percent}%{% optional priority=12 %} " + meter(6) + "{% endoptional %}")
        + "{% endoptional %}"
        + "{% optional priority=10 %} [status.cache] cache {cache.percent}% [/]{% endoptional %}",
        split,
        separator="[status_mcp] │ [/]",
        tail=" [reset]",
        band=True,
    ),
    "blocks": lambda split: compose(
        BLOCKS_IDENTITY
        + "{% optional priority=35 %}[status.provider] {provider} [reset]{% endoptional %}"
        + "[status.model] {model} [reset]"
        + "{% optional priority=20 %}[status.reason] {reasoning} [reset]{% endoptional %}",
        "{% optional priority=5 %}[status.services] {mcp.label} skills {skills.count} [reset]{% endoptional %}"
        + "{% optional priority=10 %}[status.cache] cache {cache.percent}% [reset]{% endoptional %}"
        + "{% optional priority=30 %}"
        + SEGMENT_USAGE
        + "{% endoptional %}",
        split,
    ),
    "vim": lambda split: compose(
        "[status.band]{% if agent.name %}[status.agent] {agent.name} [/]{% endif %} "
        + "{% if yolo %}[status_yolo bold]yolo[/] · {% endif %}"
        + AGENT_GROUP
        + PLUGIN_GROUP
        + "{% optional priority=35 %}[status_provider]{provider}[/][status_mcp] / [/]{% endoptional %}[status_base bold]{model}[/]"
        + "{% optional priority=20 %} [status_reason underline]{reasoning}[/]{% endoptional %}",
        "{% optional priority=5 %}[status_services]"
        + SERVICE_COUNTS
        + "[/][status_mcp] │ [/]{% endoptional %}"
        + "{% optional priority=30 %}"
        + SEGMENT_USAGE
        + "{% endoptional %}",
        split,
        separator="[status_mcp] │ [/]",
        tail="[reset]",
        band=True,
    ),
    "lualine": lambda split: compose(
        "[status.band]" + segment("{agent.name}", "status.agent", when="agent.name") + segment("{model}", "status.model") + SEGMENT_DETAILS,
        segment(SERVICE_COUNTS, "status.services", priority=5, right=split)
        + segment("cache {cache.percent}%", "status.cache.segment", priority=10, right=split)
        + usage_segment(split),
        split,
        tail="[reset]",
        band=True,
    ),
    "powerline": lambda split: compose(
        segment("{agent.name}", "status.detail bold", when="agent.name") + segment("{model}", "status.model") + SEGMENT_DETAILS,
        segment(SERVICE_COUNTS, "status.services", priority=5, right=split) + usage_segment(split),
        split,
        tail="[reset]",
    ),
}

# Waiting for the user is actionable even when ordinary group metrics have been elided.
# Put the alert before optional identity/details so hard clipping cannot hide it on a narrow
# terminal. Only the alert uses the warning surface; each preset keeps its ordinary geometry.
STATUS_ATTENTION = {
    "powerline": segment("wait {agents.waiting}", "status.attention", when="agents.waiting"),
    "lualine": segment("wait {agents.waiting}", "status.attention", when="agents.waiting"),
    "blocks": "{% if agents.waiting %}[status.attention] wait {agents.waiting} [reset]{% endif %}",
    "default": "{% if agents.waiting %}[status.warning bold][[wait {agents.waiting}]] [/] {% endif %}",
}


def status_template(name: str, split: bool) -> str:
    """Preset `name`, with its usage group at the right edge (`split`) or beside the rest."""
    attention = STATUS_ATTENTION.get(name, "{% if agents.waiting %}[status.warning bold]wait {agents.waiting}[/][status_mcp] │ [/]{% endif %}")
    return attention + STATUS_LAYOUTS[name](split)


# A preset reference keeps both groups on the left; the other placement is saved as its template.
STATUS_PRESETS = {name: status_template(name, False) for name in STATUS_LAYOUTS}


def status_layout(source: str) -> tuple[str, bool] | None:
    """The preset and placement (`split`) that `source` spells, or None for any other template.

    Only exact matches count: a template changed in any other way is its author's, and is never
    rewritten to move its groups."""
    if source.startswith("preset:"):
        name = source.removeprefix("preset:")
        return (name, False) if name in STATUS_LAYOUTS else None
    return next(((name, split) for name in STATUS_LAYOUTS for split in (False, True) if source == status_template(name, split)), None)


def status_source(name: str, split: bool) -> str:
    """The `ui.statusbar.format` value for a preset placement."""
    return status_template(name, True) if split else "preset:" + name


DIVIDER_PRESETS = {
    "plain": "[divider_rule]──[/]{% if running %} [spinner]{spinner}[/][divider.label]{label}[/] {% endif %}[divider_rule]{fill:─}[/]",
    "comet": "[divider_rule]───[/]{% if running %} [spinner]{spinner}[/][divider.label]{label}[/] {% endif %}[divider_rule]{fill:─}[/]",
    "minimal": "{% if running %}[spinner]{spinner}[/][divider.label]{label}[/]{% else %}[divider_rule]{fill:─}[/]{% endif %}",
    "powerline": "{% if running %}[spinner]{spinner}[/][divider.activity] {activity} · {elapsed:duration} {join:}[reset]{% endif %}[divider_rule]{fill:─}[/]{% if running %}{join:}[divider.metrics] {rate} {% if queue.total > 0 %}· {queue.total} queued {% endif %}{% if reset_pending %}· reset pending {% endif %}[reset]{% endif %}",
    "dashed": "[divider_rule]╌╌╌[/]{% if running %} [spinner]{spinner}[/][divider.label]{label}[/] {% endif %}[divider_rule]{fill:╌}[/]",
    "dotted": "[divider_rule]┈┈┈[/]{% if running %} [spinner]{spinner}[/][divider.label]{label}[/] {% endif %}[divider_rule]{fill:┈}[/]",
    "double": "[divider_rule]══[/]{% if running %}[divider_rule]╡[/] [spinner]{spinner}[/][divider.label]{label}[/] [divider_rule]╞[/]{% endif %}[divider_rule]{fill:═}[/]",
    "right": "[divider_rule]{fill:─}[/]{% if running %}[divider_rule]┤[/] [spinner]{spinner}[/][divider.label]{label}[/] [divider_rule]├──[/]{% endif %}",
    "capsule": "[divider_rule]{fill:─}[/]{% if running %}[fg=menu_bg][/][divider.badge] [spinner]{spinner}[/]{label} [/][fg=menu_bg][/]{% endif %}[divider_rule]{fill:─}[/]",
    "frame": "[divider_rule]╭─[/]{% if running %}[divider_rule]┤[/] [spinner]{spinner}[/][divider.label]{label}[/] [divider_rule]├[/]{% endif %}[divider_rule]{fill:─}╮[/]",
    "rail": "{% if running %}[spinner]{spinner}[/][divider.label]{activity}[/] {% endif %}[divider_rule]{fill:─}[/]"
    + "{% if running %} [fg=status_base]{elapsed:duration}[/]{% optional priority=10 %}{% if rate %}[muted] · [/][divider.label]{rate}[/]{% endif %}{% endoptional %}"
    + "{% if queue.followup > 0 %}[warning] · {queue.followup} queued[/]{% endif %}"
    + "{% if queue.next_turn > 0 %}[warning] · {queue.next_turn} next turn[/]{% endif %}"
    + "{% if reset_pending %}[warning] · reset pending[/]{% endif %}{% endif %}",
}


class Template(formats.Template):
    """A bar format: the language in `wizolt.formats` over the status facts and plugin fields."""

    def __init__(self, source: str, presets: Mapping[str, str] | None = None):
        super().__init__(source, presets, fields=FIELDS, plugin_fields=True)


DEFAULTS = {"statusbar": "preset:default", "divider": "preset:comet", "sweep": "preset:comet"}
PRESETS = {"statusbar": STATUS_PRESETS, "divider": DIVIDER_PRESETS, "sweep": SWEEPS}


class BarLayout:
    """Session-local compiled settings. Failed reloads leave the previous settings intact."""

    def __init__(self):
        self.presets = {kind: dict(values) for kind, values in PRESETS.items()}
        self.sources = dict(DEFAULTS)
        self.templates = {key: Template(value, PRESETS[key]) for key, value in DEFAULTS.items() if key != "sweep"}
        self.sweep = Sweep(DEFAULTS["sweep"])
        self.errors: list[str] = []

    def configure(self, sources: Mapping[str, str], styles: Callable[[set[str], str], Mapping[str, str]]) -> list[str]:
        templates = dict(self.templates)
        sweep = self.sweep
        problems = []
        sample: dict[str, Value] = dict.fromkeys(FIELDS, 0)
        for key, source in sources.items():
            try:
                if not isinstance(source, str):
                    raise TypeError("must be a string")
                if key == "sweep":
                    sweep = Sweep(source)
                else:
                    template = Template(source, self.presets[key])
                    resolved = styles(template.styles, key)
                    for running in (False, True):
                        sample.update(running=running, yolo=running)
                        template.render(sample, 80, resolved)
                    templates[key] = template
            except (ValueError, TypeError, ArithmeticError, KeyError) as error:
                problems.append(f"ui.{('divider.sweep' if key == 'sweep' else key + '.format')}: {error}")
        if not problems:
            self.sources.update(sources)
            self.templates, self.sweep = templates, sweep
            self.errors = []
        return problems

    def load(self, raw: Mapping[str, Any], styles: Callable[[set[str], str], Mapping[str, str]]) -> list[str]:
        sources = dict(DEFAULTS)
        for section, keys in (("statusbar", ("format",)), ("divider", ("format", "sweep"))):
            table = raw.get(section, {})
            if not isinstance(table, dict):
                return [f"ui.{section} must be a table"]
            unknown = set(table) - {*keys, "theme"}
            if unknown:
                return [f"ui.{section}: unknown settings: {', '.join(sorted(unknown))}"]
            for key in keys:
                if key in table:
                    sources["sweep" if key == "sweep" else section] = table[key]
        missing = {
            kind: source
            for kind, source in sources.items()
            if isinstance(source, str) and source.startswith("preset:plugins.") and source[7:] not in self.presets[kind]
        }
        problems = self.configure({kind: DEFAULTS[kind] if kind in missing else source for kind, source in sources.items()}, styles)
        if not problems:
            self.sources.update(missing)
        return problems

    def project_presets(self, presets: dict[str, dict[str, str]], styles: Callable[[set[str], str], Mapping[str, str]]) -> None:
        """Recompile referenced presets on reload; missing choices fall back without erasing intent."""
        self.presets = presets
        for kind, source in tuple(self.sources.items()):
            if not source.startswith("preset:plugins."):
                continue
            if self.configure({kind: source}, styles):
                self.configure({kind: DEFAULTS[kind]}, styles)
                self.sources[kind] = source

    def render(
        self, kind: str, values: Mapping[str, Value], width: int, styles: Callable[[set[str], str], Mapping[str, str]], *, ramp: tuple[str, ...] = ()
    ) -> Fragments:
        template = self.templates[kind]
        try:
            return template.render(
                values,
                width,
                styles(template.styles, kind),
                sweep=self.sweep if kind == "divider" and values.get("running") else None,
                t=float(values.get("elapsed", 0)),
                ramp=ramp,
            )
        except (ValueError, TypeError, ArithmeticError, KeyError) as error:
            message = f"ui.{kind}: {error}"
            if message not in self.errors:
                self.errors = [message]
            fallback = Template(DEFAULTS[kind], PRESETS[kind])
            return fallback.render(values, width, styles(fallback.styles, kind))
