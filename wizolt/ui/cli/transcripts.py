"""Transcript record previews and config saves for the appearance picker.

The `/theme` Transcript tab: it shows what a `[transcript] format` does to a sample record and
writes the choice to `[transcript] format`. The preview settles a sample call through the
transcript's own path (`toolblocks.finish_display`), so what the tab shows is what the record
prints -- the builtin assembly for `standard`, the format's rows otherwise, and the rows the host
always adds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import StyleAndTextTuples

from wizolt.base import ConfigError, LogBlock, LogEdge, Text, ToolCall, TurnBox
from wizolt.config import ConfigFile
from wizolt.tools import Tool, toolblocks, transcript
from wizolt.tools.toolblocks import ToolDisplay
from wizolt.ui.render import Theme, UiPrinter

if TYPE_CHECKING:
    from wizolt.session import Session
    from wizolt.ui.cli.loop import CommandLoop

# The sample call the tab previews: a search whose output runs past the preview, so a format's
# output rows and its closing row both have something to show.
SAMPLE_CALL = ToolCall("sample", "Bash", ["rg -n export_rows src"])
SAMPLE_KEY = "tr.12"
SAMPLE_OUTPUT = (
    "src/jobs/export.rs:42:  export_rows(&pool, &cfg);",
    "src/db/rows.rs:118:     fn export_rows(",
    "src/db/rows.rs:140:     rows.push(row);",
    "src/db/rows.rs:201:     };",
    "src/db/rows.rs:204: }",
)
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
    """The rows a format stands for, with a preset spelled out. `standard` is the builtin
    assembly, which has no rows to show or edit, so it stands for nothing."""
    if source.startswith("preset:"):
        return transcript.PRESETS.get(source[7:], source)
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
    """A draft's problems, for the format panel's inline feedback: the config check's own."""
    try:
        transcript.parsed(source)
    except ValueError as error:
        return [str(error)]
    return []


def format_label(selection: str) -> str:
    """One format row: the preset and the shape it names, or the user's own format."""
    if selection == "custom":
        return "custom (f to edit)"
    rows = body("preset:" + selection)
    return f"{selection}   {rows.splitlines()[0][:48] if rows else 'the built-in rendering'}"


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
    if mode == "hidden":
        # The live region draws nothing at all, spark included; the sample must not promise one.
        return [(Theme.fg("muted"), "  nothing while it thinks; only the divider below names the phase\n")]
    head: StyleAndTextTuples = [(Theme.fg("muted"), "✻ thinking\n")]
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


def preview(session: Session, source: str, columns: int) -> StyleAndTextTuples:
    """The sample call settled at `source` by the transcript's own path (`finish_display`), so the
    tab shows exactly what a record prints: `standard` is the builtin assembly itself."""
    if issues := problems(source):
        return [(Theme.fg("error"), f"  {issues[0]}\n")]
    output = Tool.process_result("BashToolResult", 0, "\n".join(SAMPLE_OUTPUT), "")
    block = toolblocks.finish_display(session, SAMPLE_CALL, SAMPLE_KEY, output, failed=False, elapsed=0.4, d=ToolDisplay(auto=True), source=source)
    if isinstance(block, str):
        return [("", block + "\n")]
    return [(style, text) for style, text in UiPrinter().log_segments(block, columns)]


def select(loop: CommandLoop, group: str, value: str) -> str:
    """Apply and persist one group's choice, like a bar's format. `value` is what the table stores:
    the format itself for the format group, the choice for the two look settings."""
    raw = loop.session.config.transcript
    table = dict(raw) if isinstance(raw, dict) else {}
    table[group] = value
    loop.session.config.transcript = table
    if loop.presentation.tui is not None:
        # The records already printed follow a new format: they are redrawn the way a theme
        # switch redraws the transcript. The look settings only shape what is printed next.
        if group == "format":
            loop.presentation.tui.recolor()
        else:
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
