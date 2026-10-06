"""The ``[transcript] format``: one template controlling a settled tool call's own rows.

The record layer of `design/TRANSCRIPT_APPEARANCE.md`. A template composes the static shape
of a tool record -- the call line, its output preview rows, the closing row -- from facts the
host already computed: fields (``{tool}``, ``{args}``, ``{output}`` ...) and filters
(``|tail:N``, ``|firstline``, ``|duration``, ``|lower``). The syntax follows the bar-format
family (``{% if %}`` blocks, ``[role]``-free plain text); its parser is dedicated because a
record is multi-line and has no width-driven fills or joins.

The table's other two keys, `thinking` and `close`, are look-level choices over the same record
stream: how reasoning appears while it arrives, and how a long run of silent tool calls is closed.

Structure stays host-owned: which children exist (diffs, approval cards, the ToolScript
envelope), tree edges, roles and the stored-result citation. Two red lines are enforced in code,
not by trust: a rendered block that shows output keeps its citation, and a failed call keeps an
error row. ``preset:standard`` (and an unset key) is the builtin assembly itself, so the default
reproduces today's transcript by construction; only ``preset:minimal`` and custom templates go
through this engine.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from wizolt.base import Json, LogBlock, LogEdge, LogLine, LogRole, oneline

# A template never sees unbounded output: the loop cap bounds the render even when the user
# asks for a large tail, exactly as the builtin preview is bounded.
MAX_OUTPUT_LINES = 64

PRESETS: dict[str, str] = {
    # `standard` never reaches the engine: it names the builtin assembly (see module docstring).
    # The string below is only what the /theme panel shows and copies as a starting point.
    "standard": "{tool} {args}\n{% for line in output|tail:3 %}{line}\n{% endfor %}{% if elided %}… +{elided} more lines · Ctrl-O for more{% endif %}",
    "minimal": "{marker} {tool|lower} {args}",
}

FIELDS = frozenset({"marker", "tool", "args", "output", "elapsed", "exit", "citation", "elided", "error", "failed"})

# How the model's reasoning appears while it arrives, and how a long run of silent tool calls is
# closed before the next model text. Both are the transcript's own look, so both live here.
THINKING = ("expanded", "collapsed", "hidden")
CLOSE = ("rule", "blank", "none")
FILTERS = frozenset({"lower", "upper", "firstline", "duration", "tail", "head"})


def _expand(source: str) -> str:
    """Resolve `preset:NAME` to its template body. `preset:standard` names the builtin assembly
    and expands to nothing; `builtin_or_template` is the single place that decides."""
    if not source.startswith("preset:"):
        return source
    name = source[7:]
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; choose from {', '.join(PRESETS)}")
    return "" if name == "standard" else PRESETS[name]


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Field:
    name: str
    filters: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class If:
    condition: str
    negated: bool
    children: tuple[Node, ...]
    alternate: tuple[Node, ...]


@dataclass(frozen=True)
class For:
    variable: str
    filters: tuple[tuple[str, str], ...]
    children: tuple[Node, ...]


Node = Text | Field | If | For

_TOKEN = re.compile(r"\{%\s*(.*?)\s*%\}|\{([^{}]+)\}")


def _lex(source: str) -> list[tuple[str, str]]:
    """The source as (kind, body) tokens; kind is `text`, `directive` or `field`."""
    tokens: list[tuple[str, str]] = []
    position = 0
    for match in _TOKEN.finditer(source):
        if text := source[position : match.start()]:
            tokens.append(("text", text))
        statement, field = match.groups()
        tokens.append(("directive", statement) if statement is not None else ("field", field.strip()))
        position = match.end()
    if text := source[position:]:
        tokens.append(("text", text))
    return tokens


def _parse_filters(spec: str) -> tuple[tuple[str, str], ...]:
    """The filters in a field or loop spec: `|lower|tail:3` -> (("lower", ""), ("tail", "3"))."""
    parsed: list[tuple[str, str]] = []
    for part in spec.split("|")[1:]:
        name, _, argument = part.strip().partition(":")
        if name not in FILTERS or (name in ("tail", "head") and not argument.isdigit()):
            raise ValueError(f"unknown filter {part.strip()!r}")
        parsed.append((name, argument))
    return tuple(parsed)


def _render_filters(items: list[str], filters: tuple[tuple[str, str], ...]) -> list[str]:
    for name, argument in filters:
        count = int(argument) if argument else 0
        if name == "tail" and count:
            items = items[-count:]
        elif name == "head" and count:
            items = items[:count]
    return items[:MAX_OUTPUT_LINES]


class RecordTemplate:
    """One parsed record template. `render` is a pure function of its values."""

    def __init__(self, source: str):
        self.source = source
        self.nodes, rest = self._block(_lex(source), ())
        if rest:
            raise ValueError(f"transcript.format: unexpected {rest[0][1].strip()!r}")

    def _block(self, tokens: list[tuple[str, str]], loop: tuple[str, ...]) -> tuple[tuple[Node, ...], list[tuple[str, str]]]:
        """Nodes until a terminator directive (endif/else/endfor); a terminator is left in place."""
        nodes: list[Node] = []
        while tokens:
            kind, body = tokens[0]
            if kind == "text":
                nodes.append(Text(body))
                tokens = tokens[1:]
                continue
            keyword = body.split(" ", 1)[0]
            if keyword in ("endif", "else", "endfor"):
                return tuple(nodes), tokens
            node, tokens = self._node(kind, body, tokens[1:], loop)
            nodes.append(node)
        return tuple(nodes), []

    def _node(self, kind: str, body: str, tokens: list[tuple[str, str]], loop: tuple[str, ...]) -> tuple[Node, list[tuple[str, str]]]:
        if kind == "field":
            name = body.split("|", 1)[0].strip()
            if name not in FIELDS and name not in loop:
                raise ValueError(f"transcript.format: unknown field {body!r}; choose from {', '.join(sorted(FIELDS))}")
            return Field(name, _parse_filters(body)), tokens
        keyword, _, statement = body.partition(" ")
        if keyword == "if":
            return self._if(statement, tokens, loop)
        if keyword == "for":
            return self._for(statement, tokens, loop)
        raise ValueError(f"transcript.format: unknown directive {body!r}")

    def _if(self, statement: str, tokens: list[tuple[str, str]], loop: tuple[str, ...]) -> tuple[If, list[tuple[str, str]]]:
        condition = statement.strip()
        negated = condition.startswith("not ")
        if negated:
            condition = condition[4:]
        if condition not in FIELDS:
            raise ValueError(f"transcript.format: unknown condition {statement!r}")
        children, rest = self._block(tokens, loop)
        alternate: tuple[Node, ...] = ()
        if rest and rest[0][1].strip() == "else":
            alternate, rest = self._block(rest[1:], loop)
        if not rest or rest[0][1].strip() != "endif":
            raise ValueError("transcript.format: missing endif")
        return If(condition, negated, children, alternate), rest[1:]

    def _for(self, statement: str, tokens: list[tuple[str, str]], loop: tuple[str, ...]) -> tuple[For, list[tuple[str, str]]]:
        matched = re.fullmatch(r"(\w+)\s+in\s+output((?:\|\w+(?::\d+)?)*)", statement.strip())
        if matched is None or matched.group(1) in FIELDS or matched.group(1) in loop:
            raise ValueError(f"transcript.format: invalid for {statement!r}: only `var in output` is supported")
        children, rest = self._block(tokens, (*loop, matched.group(1)))
        if not rest or rest[0][1].strip() != "endfor":
            raise ValueError("transcript.format: missing endfor")
        return For(matched.group(1), _parse_filters("output" + matched.group(2)), children), rest[1:]

    def render(self, values: dict[str, Any]) -> list[str]:
        """The template's lines: plain text. Structure and roles are the caller's business."""
        lines = self._evaluate(self.nodes, values)
        # Outer blank lines are layout noise; inner blanks are the author's choice.
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return lines

    def _evaluate(self, nodes: tuple[Node, ...], values: dict[str, Any]) -> list[str]:
        lines = [""]

        def splice(addition: list[str]) -> None:
            lines[-1] += addition[0]
            lines.extend(addition[1:])

        for node in nodes:
            if isinstance(node, Text):
                parts = node.text.split("\n")
                lines[-1] += parts[0]
                lines.extend(parts[1:])
            elif isinstance(node, Field):
                lines[-1] += self._field(node, values)
            elif isinstance(node, If):
                truth = bool(values.get(node.condition)) != node.negated
                splice(self._evaluate(node.children if truth else node.alternate, values))
            else:
                items = _render_filters(list(values.get("output", [])), node.filters)
                for item in items:
                    splice(self._evaluate(node.children, {**values, node.variable: item}))
        return lines

    def _field(self, node: Field, values: dict[str, Any]) -> str:
        value: Any = values.get(node.name, "")
        if isinstance(value, list):
            value = "\n".join(str(item) for item in _render_filters(list(value), node.filters))
            return str(value)
        for name, _argument in node.filters:
            value = _apply(name, value)
        # A boolean is a condition, not copy: `{failed}` prints nothing and `{% if failed %}` reads
        # it. Anything else renders as its text, with an unknown fact rendering as nothing.
        if isinstance(value, bool) or value is None:
            return ""
        return value if isinstance(value, str) else str(value)


def _apply(name: str, value: Any) -> Any:
    if name == "lower":
        return str(value).lower()
    if name == "upper":
        return str(value).upper()
    if name == "firstline":
        text = str(value).strip()
        return text.splitlines()[0] if text else ""
    if name == "duration":
        try:
            return f"{float(value):.1f}s" if value else ""
        except (TypeError, ValueError):  # an unknown elapsed is not a number to render
            return ""
    return value


def builtin_or_template(source: str) -> RecordTemplate | None:
    """None when the record keeps the builtin assembly (unset, or `preset:standard`); a parsed
    template otherwise. Parse problems raise ValueError for the config validator."""
    if not source.strip() or source.strip() == "preset:standard":
        return None
    return RecordTemplate(_expand(source))


def render_record(template: RecordTemplate | None, values: dict[str, Any]) -> list[str] | None:
    """One record's lines, or None to keep the builtin assembly: the one decision the renderer
    consults, so a failed template can never lose a record."""
    if template is None:
        return None
    try:
        return template.render(values)
    except (ValueError, TypeError, ArithmeticError):
        return None


def elided_count(template: RecordTemplate, values: dict[str, Any]) -> int:
    """How many output lines the template's own loops drop: the honest value for `{elided}`,
    so the trailer a template prints matches what it actually hid."""
    items = list(values.get("output", []))
    shown = [len(_render_filters(items, node.filters)) for node in _loops(template.nodes)]
    return max(0, len(items) - max(shown, default=len(items)))


def _loops(nodes: tuple[Any, ...]) -> Iterator[Any]:
    for node in nodes:
        if isinstance(node, For):
            yield node
            yield from _loops(node.children)
        elif isinstance(node, If):
            yield from _loops(node.children)
            yield from _loops(node.alternate)


def record_values(
    *,
    tool: str,
    args: str,
    output: str,
    elapsed: float | None,
    citation: str,
    elided: int,
    failed: bool,
    exit_code: str = "",
) -> dict[str, Any]:
    """The facts one settled call offers a template. `output` arrives bounded by the caller.

    An unknown fact is empty text, never a zero: `{elapsed}` prints nothing when the call has no
    measured time, and `{exit}` nothing when the tool reports no code. A condition field
    (`{failed}`) is a fact for `{% if %}`, so it renders no text either way."""
    lines = [line for line in output.splitlines() if line.strip()][:MAX_OUTPUT_LINES]
    return {
        "marker": "●",
        "tool": tool,
        "args": args,
        "output": lines,
        "elapsed": elapsed if elapsed is not None else "",
        "exit": exit_code,
        "citation": citation,
        "elided": elided,
        "error": output if failed else "",
        "failed": failed,
    }


def template_block(
    lines: list[str],
    *,
    citation: str,
    failed: bool,
    output: str,
    nested: bool,
    extras: list[LogLine] | None = None,
    batch_suffix: str = "",
    lexer: str = "",
) -> LogBlock:
    """Place a rendered record in the block tree. The first line is the call line (a plain
    child instead when the runner already drew one above a live preview); the rest are output
    rows; `extras` are engine-owned structure rows (an MCP summary, a ToolScript envelope, an
    Ask answer, a vision trace) that the record's shape never owns.

    The two red lines are enforced here, whatever the template said: a failed call keeps an
    error row, and a block that shows output keeps its citation.
    """
    root: LogLine | None = None
    if not nested:
        name, _, args = (lines[0] if lines else "").partition(" ")
        meta = (("  " + batch_suffix) if batch_suffix else "") + ((" → " + citation) if citation else "")
        root = LogLine(name, args, LogRole.ERROR if failed else LogRole.TOOL, meta=meta, syntax="" if failed else lexer)
    children = [LogLine("", line, LogRole.OUTPUT, LogEdge.CONTINUE) for line in (lines if nested else lines[1:])]
    children.extend(extras or [])
    if failed:
        children.append(LogLine("error", oneline(output, 220), LogRole.ERROR, LogEdge.END))
    else:
        carries = citation and citation not in (root.meta if root else "") and not any(citation in child.meta or citation in child.text for child in children)
        if children and carries:
            last = children[-1]
            meta = (last.meta + " · " if last.meta else "") + citation
            children[-1] = LogLine(last.label, last.text, last.role, LogEdge.END, meta=meta, syntax=last.syntax)
        elif not children and citation and root is None:
            children.append(LogLine("stored" if citation.startswith("tr.") else "done", citation, LogRole.META, LogEdge.END))
        elif children:
            # The block's own last row closes it, exactly as the builtin assembly does.
            last = children[-1]
            children[-1] = LogLine(last.label, last.text, last.role, LogEdge.END, meta=last.meta, syntax=last.syntax)
    return LogBlock.hierarchy(root, children)


def validate(raw: Json) -> list[str]:
    """Config-time validation of the raw `[transcript]` table: early feedback only, the renderer
    falls back safely whatever it says."""
    if not isinstance(raw, dict):
        return ["transcript must be a table"]
    problems: list[str] = []
    if unknown := set(raw) - {"format", "tool", "thinking", "close"}:
        problems.append(f"transcript: unknown settings: {', '.join(sorted(unknown))}")
    for label, source in [("transcript.format", raw.get("format", ""))]:
        if not isinstance(source, str):
            problems.append(f"{label} must be a string")
        else:
            problems.extend(_problems(label, source))
    for key, choices in (("thinking", THINKING), ("close", CLOSE)):
        value = raw.get(key, choices[0])
        if value not in choices:
            problems.append(f"transcript.{key}: unknown value {value!r}; choose from {', '.join(choices)}")
    table = raw.get("tool", {})
    if not isinstance(table, dict):
        return [*problems, "transcript.tool must be a table"]
    for name, entry in table.items():
        label = f"transcript.tool.{name}"
        if not isinstance(entry, dict) or set(entry) - {"format"} or not isinstance(entry.get("format", ""), str):
            problems.append(f"{label} must be a table with a string format")
        else:
            problems.extend(_problems(label, entry["format"]))
    return problems


def _problems(label: str, source: str) -> list[str]:
    try:
        builtin_or_template(source)
    except ValueError as error:
        message = str(error).removeprefix("transcript.format: ")
        return [f"{label}: {message}"]
    return []


def thinking(config: Any) -> str:
    """How reasoning appears while it arrives: every line, its first line, or nothing."""
    raw = getattr(config, "transcript", None)
    value = raw.get("thinking") if isinstance(raw, dict) else None
    return value if value in THINKING else THINKING[0]


def close(config: Any) -> str:
    """How a long run of silent tool calls is closed before the next model text."""
    raw = getattr(config, "transcript", None)
    value = raw.get("close") if isinstance(raw, dict) else None
    return value if value in CLOSE else CLOSE[0]


def effective_format(config: Any, tool: str) -> str:
    """The record format for one tool: its sparse override, else the global key. Empty = builtin."""
    raw = getattr(config, "transcript", None)
    if not isinstance(raw, dict):
        return ""
    override = raw.get("tool", {})
    if isinstance(override, dict):
        per_tool = override.get(tool)
        if isinstance(per_tool, dict) and isinstance(per_tool.get("format"), str):
            return per_tool["format"]
    value = raw.get("format", "")
    return value if isinstance(value, str) else ""
