"""Single-line UI templates and bounded mathematical sweep expressions.

No session, terminal IO or theme ownership lives here. Callers supply values and resolved styles;
parsing is done once, while rendering only lays out fragments at the current display width.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.utils import get_cwidth

Value = str | float | int | bool
Fragments = list[tuple[str, str]]
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


def plugin_field(name: str) -> bool:
    """Reserve a namespaced scalar lookup; this never resolves Python attributes.

    Plugin fields may be absent before activation or after disable. Like absent built-in values,
    they read as zero; templates remain loadable independently of installed plugin generations.
    """
    return re.fullmatch(r"plugins\.[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*", name) is not None


def pingpong(value: float, width: float) -> float:
    return width - abs(value % (2 * width) - width) if width > 0 else 0.0


FUNCTIONS = {"sin": math.sin, "cos": math.cos, "exp": math.exp, "sqrt": math.sqrt, "abs": abs, "min": min, "max": max, "pingpong": pingpong}
BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow}
COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


class Expression:
    """An allowlisted AST interpreter; never evaluates Python attributes, code or user objects."""

    def __init__(self, source: str, fields: frozenset[str]):
        if len(source) > 1024:
            raise ValueError("expression exceeds 1024 characters")
        try:
            tree = ast.parse(source, mode="eval")
        except (SyntaxError, RecursionError) as error:
            raise ValueError(f"invalid expression: {error}") from error
        if sum(1 for _ in ast.walk(tree)) > 100:
            raise ValueError("expression exceeds 100 nodes")
        self.evaluate = self._compile(tree.body, fields)

    def _compile(self, node: ast.expr, fields: frozenset[str]) -> Callable[[Mapping[str, Value]], Any]:
        if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool):
            value = node.value
            if isinstance(value, (int, float)) and (abs(value) > 1e12 or not math.isfinite(value)):
                raise ValueError("numeric constant out of range")
            return lambda values: value
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = ast.unparse(node)
            if name not in fields and not (fields == FIELDS and plugin_field(name)):
                raise ValueError(f"unknown field {name!r}")
            return lambda values: values.get(name, 0)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            operand = self._compile(node.operand, fields)
            fn = operator.not_ if isinstance(node.op, ast.Not) else operator.neg if isinstance(node.op, ast.USub) else operator.pos
            return lambda values: fn(operand(values))
        if isinstance(node, ast.BinOp) and type(node.op) in BINARY:
            left, right = self._compile(node.left, fields), self._compile(node.right, fields)
            fn = BINARY[type(node.op)]

            def binary(values):
                a, b = left(values), right(values)
                if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                    raise TypeError("arithmetic requires numbers")
                if abs(a) > 1e12 or abs(b) > 1e12 or (isinstance(node.op, ast.Pow) and abs(b) > 32):
                    raise ValueError("arithmetic out of range")
                result = fn(a, b)
                if isinstance(result, complex) or not math.isfinite(result) or abs(result) > 1e12:
                    raise ValueError("arithmetic out of range")
                return result

            return binary
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            operands = [self._compile(value, fields) for value in node.values]
            combine = all if isinstance(node.op, ast.And) else any
            return lambda values: combine(bool(operand(values)) for operand in operands)
        if isinstance(node, ast.Compare) and all(type(op) in COMPARE for op in node.ops):
            operands = [self._compile(value, fields) for value in (node.left, *node.comparators)]
            return lambda values: all(COMPARE[type(op)](operands[i](values), operands[i + 1](values)) for i, op in enumerate(node.ops))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords and 1 <= len(node.args) <= 4:
            fn = FUNCTIONS[node.func.id]
            args = [self._compile(arg, fields) for arg in node.args]
            return lambda values: fn(*(arg(values) for arg in args))
        raise ValueError(f"unsupported expression: {ast.unparse(node)}")


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


def expand(source: str, presets: Mapping[str, str]) -> str:
    if source.startswith("preset:"):
        name = source[7:]
        if name not in presets:
            raise ValueError(f"unknown preset {name!r}; choose from {', '.join(presets)}")
        return presets[name]
    return source


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
SERVICE_DETAILS = "{mcp.label} · skills {skills.count}{% if plugins.count %} · plugins {plugins.count}{% endif %}"
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
        + "{% optional priority=5 %}[subtle] | [/][status_mcp]"
        + SERVICE_DETAILS
        + "[/]{% endoptional %}",
        pressure("ctx {context.percent}%") + "{% optional priority=10 %}[status_cache] · cache {cache.percent}%[/]{% endoptional %}",
        split,
        separator="[subtle] | [/]",
    ),
    "minimal": lambda split: compose(IDENTITY + "[status_base bold]{model}[/]", pressure(meter(5) + " {context.percent}%"), split, separator=" "),
    "split": lambda split: compose(
        "[status.split] [status_provider]▎ [/]"
        + IDENTITY
        + "{% optional priority=35 %}[status_provider]{provider}/[/]{% endoptional %}[status_model bold]{model}[/]"
        + "{% optional priority=20 %}[status_mcp] · [/][status_reason]{reasoning}[/]{% endoptional %}",
        "{% optional priority=30 %}"
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
        pressure("{context.percent}%"),
        split,
        separator="[status_mcp] › [/]",
    ),
    "brackets": lambda split: compose(
        IDENTITY
        + "[status_provider][[{provider}[/][status_mcp]/[/][status_model bold]{model}]][/] "
        + "{% optional priority=20 %}[status_reason][[{reasoning}]][/] {% endoptional %}",
        pressure("[[ctx {context.percent}%]]") + "{% optional priority=10 %}[status_cache] [[cache {cache.percent}%]][/]{% endoptional %}",
        split,
    ),
    "monitor": lambda split: compose(
        "[status.monitor] [status_context]◆ [/]"
        + IDENTITY
        + "{% optional priority=35 %}[status_provider]{provider}/[/]{% endoptional %}[status_model bold]{model}[/]"
        + "{% optional priority=20 %}[status_mcp]  [/][status.reason] {reasoning} [/]{% endoptional %}",
        "{% optional priority=5 %}[status_mcp]{mcp.label} · skills {skills.count} │ [/]{% endoptional %}"
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
        "{% optional priority=10 %}[status.cache] cache {cache.percent}% [reset]{% endoptional %}"
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
        "{% optional priority=5 %}[status_mcp]{mcp.label} │ [/]{% endoptional %}" + "{% optional priority=30 %}" + SEGMENT_USAGE + "{% endoptional %}",
        split,
        separator="[status_mcp] │ [/]",
        tail="[reset]",
        band=True,
    ),
    "lualine": lambda split: compose(
        "[status.band]" + segment("{agent.name}", "status.agent", when="agent.name") + segment("{model}", "status.model") + SEGMENT_DETAILS,
        segment("{mcp.label} · skills {skills.count}", "status.detail", priority=5, right=split)
        + segment("cache {cache.percent}%", "status.cache.segment", priority=10, right=split)
        + usage_segment(split),
        split,
        tail="[reset]",
        band=True,
    ),
    "powerline": lambda split: compose(
        segment("{agent.name}", "status.detail bold", when="agent.name") + segment("{model}", "status.model") + SEGMENT_DETAILS,
        usage_segment(split),
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


@dataclass
class Node:
    kind: str
    text: str = ""
    children: list[Node] = field(default_factory=list)
    alternate: list[Node] = field(default_factory=list)
    expression: Expression | None = None
    priority: int = 0


@dataclass
class Cell:
    style: str
    text: str
    kind: str = "text"


def clean(text: str) -> str:
    return "".join(char if ord(char) >= 32 and not 127 <= ord(char) <= 159 else " " for char in text)


def clip(fragments: Fragments, width: int) -> Fragments:
    result: Fragments = []
    for style, text in fragments:
        part = ""
        for char in text:
            size = max(0, get_cwidth(char))
            if size > width:
                break
            part += char
            width -= size
        if part:
            if result and result[-1][0] == style:
                result[-1] = (style, result[-1][1] + part)
            else:
                result.append((style, part))
        if part != text:
            break
    return result


class Template:
    """Parse once; resolve conditional/optional spans before width and Powerline joins."""

    def __init__(self, source: str, presets: Mapping[str, str] | None = None):
        self.source = expand(source, presets or {})
        if len(self.source) > 8192:
            raise ValueError("template exceeds 8192 characters")
        self.nodes: list[Node] = []
        self.styles: set[str] = set()
        target = self.nodes
        stack: list[tuple[Node, list[Node]]] = []
        pattern = re.compile(r"\{\{|\}\}|\[\[|\]\]|\{%.*?%\}|\{[^{}]*\}|\[[^\[\]]*\]", re.DOTALL)

        def located(position: int, problem: object) -> ValueError:
            line = self.source.count("\n", 0, position) + 1
            column = position - self.source.rfind("\n", 0, position)
            return ValueError(f"line {line}, column {column}: {problem}")

        def literal_text(start: int, stop: int) -> str:
            literal = self.source[start:stop]
            if stray := re.search(r"[{}\[\]]", literal):
                raise located(start + stray.start(), "unmatched delimiter; escape literal brackets by doubling them")
            return literal

        end = 0
        for index, match in enumerate(pattern.finditer(self.source)):
            if index >= 512:
                raise ValueError("template exceeds 512 tokens")
            target.append(Node("text", literal_text(end, match.start())))
            token = match.group()
            try:
                if token in ("{{", "}}", "[[", "]]"):
                    target.append(Node("text", token[0]))
                elif token.startswith("{%"):
                    statement = token[2:-2].strip()
                    if statement.startswith(("if ", "optional priority=")):
                        node = Node("if" if statement.startswith("if ") else "optional")
                        if node.kind == "if":
                            node.expression = Expression(statement[3:], FIELDS)
                        else:
                            node.priority = int(statement.removeprefix("optional priority="))
                        if len(stack) >= 16:
                            raise ValueError("template nesting exceeds 16 levels")
                        target.append(node)
                        stack.append((node, target))
                        target = node.children
                    elif statement == "else" and stack and stack[-1][0].kind == "if":
                        target = stack[-1][0].alternate
                    elif stack and statement == ("endif" if stack[-1][0].kind == "if" else "endoptional"):
                        _, target = stack.pop()
                    else:
                        raise ValueError(f"unexpected directive {statement!r}")
                elif token.startswith("["):
                    style = token[1:-1]
                    if style not in ("/", "reset"):
                        self.styles.add(style)
                    target.append(Node("style", style))
                else:
                    value = token[1:-1]
                    if value == ">":
                        target.append(Node("fill", " "))
                    elif value.startswith("fill:"):
                        fill_pattern = value[5:]
                        if not 1 <= len(fill_pattern) <= 32 or clean(fill_pattern) != fill_pattern or any(get_cwidth(char) != 1 for char in fill_pattern):
                            raise ValueError("fill needs 1–32 printable single-column characters")
                        target.append(Node("fill", fill_pattern))
                    elif value in ("join:", "join:"):
                        target.append(Node("join", value[5:]))
                    elif value.startswith("join:"):
                        # The glyphs are private-use characters that often render as nothing,
                        # so a hand-typed join tends to arrive empty; name them by code point.
                        raise ValueError(f"{value!r}: join takes U+E0B0 (\\ue0b0) or U+E0B2 (\\ue0b2)")
                    else:
                        name, _, spec = value.partition(":")
                        if (name not in FIELDS and not plugin_field(name)) or (spec and not re.fullmatch(r"duration|d|\.[0-6]f", spec)):
                            raise ValueError(f"unknown field or format {value!r}")
                        target.append(Node("field", value))
            except ValueError as error:
                raise located(match.start(), error) from error
            end = match.end()
        target.append(Node("text", literal_text(end, len(self.source))))
        if stack:
            raise ValueError("unclosed template block")
        self._check(self.nodes, 0)

    def _check(self, nodes: list[Node], depth: int) -> int:
        for node in nodes:
            if node.kind in ("if", "optional"):
                if self._check(node.children, depth) != depth or self._check(node.alternate, depth) != depth:
                    raise ValueError("conditional styles must be balanced")
            elif node.kind == "style":
                depth = 0 if node.text == "reset" else depth - 1 if node.text == "/" else depth + 1
                if depth < 0:
                    raise ValueError("unmatched [/]")
                if depth > 16:
                    raise ValueError("style nesting exceeds 16 levels")
        # Top-level styles may deliberately run to the end of the line. Conditional styles
        # must be closed so omitting a branch cannot change the styles of the rest of the row.
        return depth

    def render(
        self, values: Mapping[str, Value], width: int, styles: Mapping[str, str], *, sweep: Sweep | None = None, t: float = 0, ramp: tuple[str, ...] = ()
    ) -> Fragments:
        width = max(0, min(width, 10000))
        optional: list[Node] = []
        omitted: set[int] = set()

        def select(nodes: list[Node]) -> list[Node]:
            result: list[Node] = []
            for node in nodes:
                if node.kind == "if":
                    assert node.expression is not None
                    result.extend(select(node.children if node.expression.evaluate(values) else node.alternate))
                elif node.kind == "optional":
                    if id(node) not in omitted:
                        optional.append(node)
                        result.extend(select(node.children))
                else:
                    result.append(node)
            return result

        def cells() -> list[Cell]:
            result: list[Cell] = []
            stack = [""]
            for node in select(self.nodes):
                if node.kind == "style":
                    if node.text == "reset":
                        stack = [""]
                    elif node.text == "/":
                        if len(stack) > 1:
                            stack.pop()
                    else:
                        stack.append((stack[-1] + " " + styles[node.text]).strip())
                    result.append(Cell(stack[-1], ""))
                elif node.kind == "field":
                    name, _, spec = node.text.partition(":")
                    value = values.get(name, 0 if plugin_field(name) else "")
                    text = f"{int(float(value))}s" if spec == "duration" else format(value, spec)
                    result.append(Cell(stack[-1], clean(text)[:4096]))
                elif node.text:
                    result.append(Cell(stack[-1], clean(node.text), node.kind))
            visible = []
            for cell in result:
                if cell.text and not (cell.kind == "join" and visible and visible[-1].kind == "join"):
                    visible.append(cell)
            return visible

        active = cells()
        for node in sorted(optional, key=lambda node: node.priority):
            if sum(get_cwidth(cell.text) for cell in active if cell.kind != "fill") <= width:
                break
            omitted.add(id(node))
            active = cells()
        fills = sum(cell.kind == "fill" for cell in active)
        fixed = sum(get_cwidth(cell.text) for cell in active if cell.kind != "fill")
        remaining = max(0, width - fixed)
        fill_width, extra = divmod(remaining, fills or 1)
        fragments: Fragments = []
        position = 0
        fill_position = 0

        def background(style: str) -> str:
            return next((part[3:] for part in reversed(style.split()) if part.startswith("bg:")), "default")

        for i, cell in enumerate(active):
            style, text = cell.style, cell.text
            if cell.kind == "fill":
                size = fill_width + (extra > 0)
                extra = max(0, extra - 1)
                text = (text * ((size + len(text) - 1) // len(text)))[:size]
                if sweep is not None and ramp and text.strip():
                    for offset, char in enumerate(text):
                        level = sweep.brightness(position + offset, t, width, u=(fill_position + offset) / max(1, remaining - 1))
                        selected = ramp[round(level * (len(ramp) - 1))]
                        if fragments and fragments[-1][0] == selected:
                            fragments[-1] = (selected, fragments[-1][1] + char)
                        else:
                            fragments.append((selected, char))
                    position += len(text)
                    fill_position += len(text)
                    continue
                fill_position += len(text)
            elif cell.kind == "join":
                left_bg = background(active[i - 1].style) if i else "default"
                right_bg = background(active[i + 1].style) if i + 1 < len(active) else "default"
                fg, bg = (left_bg, right_bg) if text == "" else (right_bg, left_bg)
                # Against no segment the join takes the row's own background, which may be a band.
                style = f"fg:{styles.get('__background', 'default') if fg == 'default' else fg}" + ("" if bg == "default" else f" bg:{bg}")
            if text:
                fragments.append((style, text))
                position += get_cwidth(text)
        # Keep the right-hand group visible when fixed text alone exceeds the width.
        if fixed > width and fills:
            cut = max(i for i, cell in enumerate(active) if cell.kind == "fill")
            right_width = sum(get_cwidth(cell.text) for cell in active[cut + 1 :])
            left_width = sum(get_cwidth(cell.text) for cell in active[:cut] if cell.kind != "fill")
            left = clip(fragments, left_width)
            right: Fragments = []
            skip = left_width
            for style, text in fragments:
                if skip >= get_cwidth(text):
                    skip -= get_cwidth(text)
                    continue
                right.append((style, text))
            return clip(left, max(0, width - right_width)) + clip(right, width)
        return clip(fragments, width)


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
