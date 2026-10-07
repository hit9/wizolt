"""The format-string language: single-line templates over named facts, and bounded expressions.

Shared by the status bar and divider (`ui.bars`) and transcript records (`tools.transcript`), so
one syntax, one validator and one renderer serve every user-written format. Each caller names the
facts its formats may read. No session, terminal IO or theme ownership lives here: callers supply
values and resolved styles; parsing is done once, rendering only lays out fragments at a width.
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
    """An allowlisted AST interpreter; never evaluates Python attributes, code or user objects.

    `plugin_fields` also admits `plugins.NAME.FIELD` names, which read as zero when absent."""

    def __init__(self, source: str, fields: frozenset[str], *, plugin_fields: bool = False):
        if len(source) > 1024:
            raise ValueError("expression exceeds 1024 characters")
        try:
            tree = ast.parse(source, mode="eval")
        except (SyntaxError, RecursionError) as error:
            raise ValueError(f"invalid expression: {error}") from error
        if sum(1 for _ in ast.walk(tree)) > 100:
            raise ValueError("expression exceeds 100 nodes")
        self.plugin_fields = plugin_fields
        self.evaluate = self._compile(tree.body, fields)

    def _compile(self, node: ast.expr, fields: frozenset[str]) -> Callable[[Mapping[str, Value]], Any]:
        if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool):
            value = node.value
            if isinstance(value, (int, float)) and (abs(value) > 1e12 or not math.isfinite(value)):
                raise ValueError("numeric constant out of range")
            return lambda values: value
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = ast.unparse(node)
            if name not in fields and not (self.plugin_fields and plugin_field(name)):
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


def expand(source: str, presets: Mapping[str, str]) -> str:
    if source.startswith("preset:"):
        name = source[7:]
        if name not in presets:
            raise ValueError(f"unknown preset {name!r}; choose from {', '.join(presets)}")
        return presets[name]
    return source


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
    """Control characters (C0, DEL, C1) as spaces. Printable text, nearly all of it, is returned
    as it is without visiting each character: this runs for every field a format renders."""
    if text.isprintable():
        return text
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
    """Parse once; resolve conditional/optional spans before width and Powerline joins.

    `fields` names the facts the template may read; `plugin_fields` also admits plugin fields."""

    def __init__(self, source: str, presets: Mapping[str, str] | None = None, *, fields: frozenset[str], plugin_fields: bool = False):
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
                            node.expression = Expression(statement[3:], fields, plugin_fields=plugin_fields)
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
                        if (name not in fields and not (plugin_fields and plugin_field(name))) or (spec and not re.fullmatch(r"duration|d|\.[0-6]f", spec)):
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
        self,
        values: Mapping[str, Value],
        width: int,
        styles: Mapping[str, str],
        *,
        sweep: Any = None,
        t: float = 0,
        ramp: tuple[str, ...] = (),
    ) -> Fragments:
        """`sweep` is anything with `brightness(x, t, w, *, u)` (`ui.bars.Sweep`); it paints fills."""
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
