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

from wizolt.base import ConfigError
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


def names() -> tuple[str, ...]:
    """The format presets the tab offers, in the engine's own order."""
    return tuple(transcript.PRESETS)


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
        elided=0,
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


def select(loop: CommandLoop, source: str) -> str:
    """Apply `source` as the global record format and persist it, like a bar's format."""
    raw = loop.session.config.transcript
    loop.session.config.transcript = {**(raw if isinstance(raw, dict) else {}), "format": source}
    if loop.presentation.tui is not None:
        loop.presentation.tui.invalidate()
    result = f"transcript.format: {source}"
    if loop.session.config.path:
        try:
            ConfigFile.set_ui_value(loop.session.config.path, ("transcript",), "format", source)
        except (OSError, ValueError, ConfigError) as error:
            result += f"\nApplied for this session; not saved: {error}"
        else:
            result += " (saved)"
    return result
