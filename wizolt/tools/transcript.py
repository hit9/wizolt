"""The ``[transcript] format``: one format string per row of a settled tool call's record.

The record layer of `design/TRANSCRIPT_APPEARANCE.md`. Each line of the format is one row, written
in the bar-format language (`wizolt.formats.Template`: fields with format specs, ``{% if %}`` blocks with
expressions) over the facts the host already computed for the call. A row that renders empty
disappears, so a conditional row needs no syntax of its own. One row form is reserved: a line
that is exactly ``{output}``, ``{output|tail:N}`` or ``{output|head:N}`` stands for the call's
output rows, which only the host may split. Rows are plain text: ``[styles]``, fills, joins and
optional spans are bar concerns, and a record's colors are host-owned roles.

The table's other two keys, `thinking` and `close`, are look-level choices over the same record
stream: how reasoning appears while it arrives, and how a long run of silent tool calls is closed.

Structure stays host-owned: which children exist (diffs, approval cards, the ToolScript
envelope), tree edges, roles and the stored-result citation. Two red lines are enforced in code,
not by trust: a rendered block that shows output keeps its citation, and a failed call keeps an
error row. ``preset:standard`` (and an unset key) is the builtin assembly itself, so the default
is today's transcript by construction and never reaches this module's engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from wizolt.base import Json, LogBlock, LogEdge, LogLine, LogRole, oneline
from wizolt.formats import Template, clean

# The output rows a record may show, whatever a row asks for: the same bound the builtin preview has.
MAX_OUTPUT_LINES = 64

# `standard` names the builtin assembly and has no rows to show or edit; `minimal` is a format.
PRESETS: dict[str, str] = {"standard": "", "minimal": "{marker} {name} {args}"}

FIELDS = frozenset({"marker", "tool", "name", "args", "duration", "elapsed", "exit", "citation", "elided", "error", "failed"})
OUTPUT_ROW = re.compile(r"\{output(?:\|(tail|head):(\d+))?\}")

# How the model's reasoning appears while it arrives, and how a long run of silent tool calls is
# closed before the next model text. Both are the transcript's own look, so both live here.
THINKING = ("expanded", "collapsed", "hidden")
CLOSE = ("rule", "blank", "none")


@dataclass(frozen=True)
class OutputRows:
    """The reserved row: the call's output lines, all of them or a head/tail of `count`."""

    take: str = ""
    count: int = 0

    def pick(self, lines: list[str]) -> list[str]:
        if self.take == "tail":
            lines = lines[-self.count :] if self.count else []
        elif self.take == "head":
            lines = lines[: self.count]
        return lines[:MAX_OUTPUT_LINES]


class RecordTemplate:
    """One parsed record format: a row per line. Parsing happens once per source (`parsed`)."""

    def __init__(self, source: str):
        self.source = source
        rows: list[OutputRows | Template] = []
        for number, line in enumerate(source.split("\n"), 1):
            if match := OUTPUT_ROW.fullmatch(line.strip()):
                rows.append(OutputRows(match.group(1) or "", int(match.group(2) or 0)))
                continue
            try:
                row = Template(line, fields=FIELDS)
            except ValueError as error:
                raise ValueError(f"row {number}: {error}") from None
            if row.styles or _width_driven(row.nodes):
                raise ValueError(f"row {number}: a record row is plain text: no [styles], fills, joins or optional spans")
            rows.append(row)
        self.rows = tuple(rows)
        # A format without output rows never needs the call's output split or previewed.
        self.reads_output = any(isinstance(row, OutputRows) for row in rows)

    def shown(self, total: int) -> int:
        """How many of `total` output lines the record shows: the most any output row picks."""
        lines = [""] * total
        return max((len(row.pick(lines)) for row in self.rows if isinstance(row, OutputRows)), default=total)

    def render(self, values: dict[str, Any], output: list[str]) -> list[str]:
        lines: list[str] = []
        for row in self.rows:
            if isinstance(row, OutputRows):
                lines.extend(row.pick(output))
            elif text := _row_text(row.nodes, values).rstrip():
                lines.append(text)  # A row that renders nothing is left out, not drawn blank.
        return lines


def _row_text(nodes, values: dict[str, Any]) -> str:
    """One row's text: its selected literals and fields, joined. Rows carry no styles, fills,
    joins or optional spans (checked at parse time), so the width layout a bar needs -- cells,
    measuring and clipping every character -- is skipped: this runs for every settled call."""
    parts: list[str] = []
    for node in nodes:
        if node.kind == "text":
            parts.append(clean(node.text))
        elif node.kind == "field":
            name, _, spec = node.text.partition(":")
            value = values.get(name, "")
            if isinstance(value, bool):
                continue  # A condition (`failed`) is read by `{% if %}`; it prints no `True`/`False`.
            parts.append(clean(f"{int(float(value))}s" if spec == "duration" else format(value, spec))[:4096])
        elif node.kind == "if":
            parts.append(_row_text(node.children if node.expression.evaluate(values) else node.alternate, values))
    return "".join(parts)


def _width_driven(nodes) -> bool:
    return any(node.kind in ("fill", "join", "optional") or _width_driven(node.children) or _width_driven(node.alternate) for node in nodes)


@lru_cache(maxsize=64)
def _parse(source: str) -> RecordTemplate | str:
    """Cached by source, failures included (as their message): the hot path parses each distinct
    format once. The message, not the exception: raising one cached exception object again on
    every record would grow its traceback, and the frames it holds, without bound."""
    try:
        return RecordTemplate(source)
    except ValueError as error:
        return str(error)


def parsed(source: str) -> RecordTemplate | None:
    """None when the record keeps the builtin assembly (unset, or `preset:standard`); the parsed
    format otherwise. Raises ValueError for an unknown preset or a broken format."""
    source = source.strip()
    if source.startswith("preset:"):
        name = source[7:]
        if name not in PRESETS:
            raise ValueError(f"unknown preset {name!r}; choose from {', '.join(PRESETS)}")
        source = PRESETS[name]
    if not source:
        return None
    result = _parse(source)
    if isinstance(result, RecordTemplate):
        return result
    raise ValueError(result)  # A fresh error per call; see `_parse`.


def render_record(template: RecordTemplate, values: dict[str, Any], output: list[str]) -> list[str] | None:
    """One record's lines, or None to keep the builtin assembly: a format that cannot render this
    call's facts (a format spec on an unknown time, say) never loses the record."""
    try:
        return template.render(values, output)
    except (ValueError, TypeError, ArithmeticError):
        return None


def output_lines(output: str) -> list[str]:
    """The output rows a format may show: the nonblank lines, in order."""
    return [line for line in output.splitlines() if line.strip()]


def record_values(
    *,
    tool: str,
    args: str,
    output: str,
    elapsed: float | None,
    citation: str,
    failed: bool,
    elided: int = 0,
    exit_code: str = "",
) -> dict[str, Any]:
    """The facts one settled call offers its rows.

    An unknown fact is empty text, never a zero: `{elapsed}` and `{duration}` print nothing when
    the call has no measured time, and `{exit}` nothing when the tool reports no code. `{failed}`
    is a condition for `{% if failed %}`; `{error}` is the failure's first line."""
    first = next((line.strip() for line in output.splitlines() if line.strip()), "") if failed else ""
    return {
        "marker": "●",
        "tool": tool,
        "name": tool.lower(),
        "args": args,
        "elapsed": elapsed if elapsed is not None else "",
        "duration": f"{elapsed:.1f}s" if elapsed is not None else "",
        "exit": exit_code,
        "citation": citation,
        "elided": elided,
        "error": first,
        "failed": failed,
    }


def call_label(line: str, tool: str) -> tuple[str, str]:
    """A call row as (label, arguments): the label takes the tool's color, the rest is argument
    text. The label runs through the tool's own name when the row names it early -- as `{tool}`
    or `{name}`, after a marker such as `●` -- so the name is drawn as a tool in any format;
    otherwise it is the row's first word, like the builtin `Bash  args`."""
    words = line.split(" ")
    cut = next((index + 1 for index, word in enumerate(words[:3]) if tool and word.lower() == tool.lower()), 1)
    return " ".join(words[:cut]), " ".join(words[cut:])


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
    tool: str = "",
) -> LogBlock:
    """Place a rendered record in the block tree. The first line is the call line (left out when
    the runner already drew one above a live preview); the rest are output rows; `extras` are
    engine-owned structure rows (an MCP summary, a ToolScript envelope, an Ask answer, a vision
    trace) that the record's shape never owns.

    The two red lines are enforced here, whatever the format said: a failed call keeps an
    error row, and a block that shows output keeps its citation.
    """
    root: LogLine | None = None
    if not nested:
        name, args = call_label(lines[0] if lines else "", tool)
        meta = (("  " + batch_suffix) if batch_suffix else "") + ((" → " + citation) if citation else "")
        root = LogLine(name, args, LogRole.ERROR if failed else LogRole.TOOL, meta=meta, syntax="" if failed else lexer)
    # The first row is the call row. Nested, the runner already drew the call line above the live
    # preview -- in its own words, before the call ran -- so the format's call row would print the
    # call twice; only the rows after it belong under the preview.
    children = [LogLine("", line, LogRole.OUTPUT, LogEdge.CONTINUE) for line in lines[1:]]
    children.extend(extras or [])
    if failed:
        # Labelled as the builtin assembly labels it: a refusal is not an error.
        label = "refused" if "user refused" in output else "error"
        children.append(LogLine(label, oneline(output, 220), LogRole.ERROR, LogEdge.END))
    else:
        carries = citation and citation not in (root.meta if root else "") and not any(citation in child.meta or citation in child.text for child in children)
        if children and carries:
            # The citation rides the last row, apart from its text, as the builtin `cited` places it.
            last = children[-1]
            children[-1] = LogLine(last.label, last.text, last.role, LogEdge.END, meta=last.meta + " · " + citation, syntax=last.syntax)
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
    source = raw.get("format", "")
    if not isinstance(source, str):
        problems.append("transcript.format must be a string")
    else:
        problems.extend(_problems("transcript.format", source))
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
        parsed(source)
    except ValueError as error:
        return [f"{label}: {error}"]
    return []


def _table(config: Any) -> dict[str, Any]:
    """The raw `[transcript]` table, or an empty one when the config holds anything else."""
    raw = getattr(config, "transcript", None)
    return raw if isinstance(raw, dict) else {}


def thinking(config: Any) -> str:
    """How reasoning appears while it arrives: every line, its first line, or nothing."""
    value = _table(config).get("thinking")
    return value if value in THINKING else THINKING[0]


def close(config: Any) -> str:
    """How a long run of silent tool calls is closed before the next model text."""
    value = _table(config).get("close")
    return value if value in CLOSE else CLOSE[0]


def effective_format(config: Any, tool: str) -> str:
    """The record format for one tool: its sparse override, else the global key. Empty = builtin."""
    raw = _table(config)
    override = raw.get("tool", {})
    if isinstance(override, dict):
        per_tool = override.get(tool)
        if isinstance(per_tool, dict) and isinstance(per_tool.get("format"), str):
            return per_tool["format"]
    value = raw.get("format", "")
    return value if isinstance(value, str) else ""
