"""Transcript record previews and config saves for the appearance picker.

The `/theme` Transcript tab: it shows what a `[transcript] format` does to a sample record and
writes the choice to `[transcript] format`. The preview renders through the same engine the
transcript uses (`wizolt.tools.transcript`), so what the tab shows is what the record prints --
including the two rows the engine always adds, a failed call's error and the stored-result
citation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import StyleAndTextTuples

from wizolt.base import ConfigError, LogBlock, LogEdge, Text, TurnBox
from wizolt.config import ConfigFile
from wizolt.tools import transcript
from wizolt.ui.render import Theme, UiPrinter

if TYPE_CHECKING:
    from wizolt.ui.cli.loop import CommandLoop

# The sample record the tab previews: a settled call and the facts a template sees. Plain lines,
# not a tool's stored envelope -- the preview is about the record's shape, not one tool's parsing.
SAMPLE_OUTPUT = (
    "src/jobs/export.rs:42:  export_rows(&pool, &cfg);",
    "src/db/rows.rs:118:     fn export_rows(",
    "src/db/rows.rs:140:     rows.push(row);",
    "src/db/rows.rs:201:     };",
    "src/db/rows.rs:204: }",
)
SAMPLE_CITATION = "tr.12 [auto]"
SAMPLE_REASONING = (
    "I should inspect the existing implementation first.",
    "The request path retries with a closed client.",
    "Reconnecting per attempt fixes it.",
)

# The tab's three settings, in the order it lists them. Each is a key of the `[transcript]` table,
# and the picker stores their selections under these names.
GROUPS = ("format", "thinking", "close")
SETTINGS = {"format": "transcript", "thinking": "transcript_thinking", "close": "transcript_close"}
TITLES = {"format": "Record format", "thinking": "Thinking display", "close": "Tool-run divider"}
LABELS = {
    "expanded": "every line",
    "collapsed": "first line only",
    "hidden": "hidden",
    "rule": "a full-width line",
    "blank": "a blank line",
    "none": "nothing",
}


def choices(group: str) -> tuple[str, ...]:
    """The values one group offers, in the order the tab lists them."""
    if group == "format":
        return tuple(transcript.PRESETS)
    return transcript.THINKING if group == "thinking" else transcript.CLOSE


def body(source: str) -> str:
    """The template a format stands for, with every preset spelled out: editing `standard`
    starts from the record shape it names, rather than from the builtin assembly it selects. A
    preset name the engine does not know previews the builtin assembly, which is what the
    transcript itself falls back to."""
    if source.startswith("preset:"):
        return transcript.PRESETS.get(source[7:], transcript.PRESETS["standard"])
    return source


def name(source: str, custom: str) -> str:
    """The preset `source` is, or `custom` (the picker's marker). A format equal to a preset's
    body counts as that preset, so editing one back to its original shape does not read as custom."""
    for preset, text in transcript.PRESETS.items():
        if source in ("preset:" + preset, text):
            return preset
    return custom


def saved(loop: CommandLoop) -> str:
    """The format in effect: the config table's global key, or the builtin assembly when unset."""
    raw = loop.session.config.transcript
    value = raw.get("format") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else "preset:standard"


def problems(source: str) -> list[str]:
    """A draft's problems, for the format panel's inline feedback."""
    try:
        transcript.RecordTemplate(body(source))
    except ValueError as error:
        return [str(error).removeprefix("transcript.format: ")]
    return []


def format_label(selection: str) -> str:
    """One format row: the preset and the shape it names, or the user's own format."""
    if selection == "custom":
        return "custom (f to edit)"
    return f"{selection}   {body('preset:' + selection).splitlines()[0][:48]}"


def current(loop: CommandLoop, group: str) -> str:
    """The value in effect for one group: the format as the picker names it, else the raw value."""
    if group == "format":
        return name(saved(loop), "custom")
    if group == "thinking":
        return transcript.thinking(loop.session.config)
    return transcript.close(loop.session.config)


def thinking_preview(mode: str, columns: int) -> StyleAndTextTuples:
    """What the reasoning trace looks like while it arrives, at the same rail the live preview
    draws: `expanded` keeps the newest lines, `collapsed` the opening one, `hidden` none."""
    rail = LogBlock.prefix(TurnBox.CONTENT_LEVEL + 1, LogEdge.CONTINUE)
    head: StyleAndTextTuples = [(Theme.fg("muted"), "✻ thinking\n")]
    if mode == "hidden":
        return [*head, (Theme.fg("muted"), "  only the divider below names the phase\n")]
    shown = SAMPLE_REASONING[:1] if mode == "collapsed" else SAMPLE_REASONING
    rows: StyleAndTextTuples = list(head)
    for line in shown:
        rows.extend([(Theme.fg("muted"), rail), (Theme.fg("muted"), " " + Text.clip_width(line, max(1, columns - len(rail) - 2)) + "\n")])
    return rows


def close_preview(style: str, columns: int) -> StyleAndTextTuples:
    """What a long run of silent tool calls leaves behind, drawn the way the transcript draws it."""
    call = [(Theme.fg("tool"), "  ● bash "), (Theme.fg("text"), "cargo bench export\n")]
    if style == "none":
        return [*call, (Theme.fg("muted"), "  the next call follows straight on\n")]
    if style == "blank":
        return [*call, ("", "\n"), (Theme.fg("muted"), "  (one blank row)\n")]
    return [*call, (Theme.fg("rule"), "─" * max(1, columns) + "\n")]


def preview(source: str, columns: int) -> StyleAndTextTuples:
    """The sample record at `source`, drawn with the transcript's own roles and edges."""
    try:
        template = transcript.RecordTemplate(body(source))
    except ValueError as error:
        return [(Theme.fg("error"), f"  {str(error).removeprefix('transcript.format: ')}\n")]
    values = transcript.record_values(
        tool="Bash",
        args="rg -n export_rows src",
        output="\n".join(SAMPLE_OUTPUT),
        elapsed=0.4,
        citation=SAMPLE_CITATION,
        failed=False,
        exit_code="0",
    )
    values["elided"] = transcript.elided_count(template, values)
    block = transcript.template_block(
        template.render(values),
        citation=SAMPLE_CITATION,
        failed=False,
        output="\n".join(SAMPLE_OUTPUT),
        nested=False,
        lexer="",
    )
    return [(style, text) for style, text in UiPrinter().log_segments(block, columns)]


def select(loop: CommandLoop, group: str, value: str) -> str:
    """Apply and persist one group's choice, like a bar's format. `value` is what the table stores:
    the format itself for the format group, the choice for the two look settings."""
    raw = loop.session.config.transcript
    table = dict(raw) if isinstance(raw, dict) else {}
    table[group] = value
    loop.session.config.transcript = table
    if loop.presentation.tui is not None:
        loop.presentation.tui.invalidate()
    result = f"transcript.{group}: {value}"
    if loop.session.config.path:
        try:
            ConfigFile.set_ui_value(loop.session.config.path, ("transcript",), group, value)
        except (OSError, ValueError, ConfigError) as error:
            result += f"\nApplied for this session; not saved: {error}"
        else:
            result += " (saved)"
    return result
