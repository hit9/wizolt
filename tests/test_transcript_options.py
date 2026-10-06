"""The two `[transcript]` look settings, `thinking` and `close`.

`thinking` says what the reasoning preview keeps while it arrives and `close` what a long run of
silent tool calls leaves behind, so both are asserted at the render sites that read them: which
rows a mode draws is not a matter of time. The fallbacks and the tab's saves go through the
boundaries the product uses -- a config table in, a config file out.
"""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_transcript_picker import TranscriptModal, frames, saved, transcript_loop

from wizolt.base import LogBlock, LogEdge, TurnBox
from wizolt.config import Config
from wizolt.tools import transcript
from wizolt.ui.cli import transcripts
from wizolt.ui.cli.appearance import theme_command

# The rail every streamed preview row hangs on, so a preview row can be told from the spark's.
RAIL = LogBlock.prefix(TurnBox.CONTENT_LEVEL + 1, LogEdge.CONTINUE)
# How the sample box opens one of its rows, and how it closes it.
BOX_ROW = "  │ "
BOX_EDGE = " │"


@pytest.fixture
def command_loop(tmp_path, monkeypatch):
    return transcript_loop(tmp_path, monkeypatch)


def box_rows(frame):
    """The rows inside the sample box at the bottom of a frame, border and padding stripped."""
    return [line[len(BOX_ROW) : -len(BOX_EDGE)].rstrip() for line in frame.splitlines() if line.startswith(BOX_ROW)]


def move_to(row):
    """Keys that move the cursor onto `row`, from where the tab opens: `h` from the first tab
    wraps backwards onto the Transcript tab, and the cursor opens on the saved format."""
    order = [f"{group}:{value}" for group in transcripts.GROUPS for value in transcripts.choices(group)]
    return ["h", *(["j"] * order.index(row))]


async def preview_at(command_loop, row):
    """The sample box the tab draws for the row the cursor reaches."""
    modal = command_loop.presentation.tui = TranscriptModal(move_to(row))
    assert await theme_command(command_loop, "") is None
    return box_rows(frames(modal)[-1])


# --- the defaults ---------------------------------------------------------------------------


def test_an_unset_or_unknown_look_setting_falls_back_to_the_builtin():
    """A key nobody set, a `transcript` key that is not a table, and a value this build does not
    know all land on the builtin look: the renderer falls back safely whatever the file says."""
    assert (transcript.thinking(Config()), transcript.close(Config())) == ("expanded", "rule")
    assert transcript.thinking(SimpleNamespace()) == "expanded"  # no `transcript` attribute at all
    assert transcript.thinking(SimpleNamespace(transcript=None)) == "expanded"
    assert transcript.thinking(SimpleNamespace(transcript="preset:minimal")) == "expanded"
    assert transcript.thinking(SimpleNamespace(transcript={})) == "expanded"
    assert transcript.thinking(SimpleNamespace(transcript={"thinking": "loud"})) == "expanded"
    assert transcript.close(SimpleNamespace(transcript={"close": "dash"})) == "rule"


def test_a_known_look_setting_is_returned_as_it_stands():
    assert transcript.thinking(SimpleNamespace(transcript={"thinking": "collapsed"})) == "collapsed"
    assert transcript.thinking(SimpleNamespace(transcript={"thinking": "hidden", "close": "none"})) == "hidden"
    assert transcript.close(SimpleNamespace(transcript={"thinking": "hidden", "close": "none"})) == "none"


def test_validate_reports_an_unknown_thinking_or_close_and_accepts_the_known_ones():
    assert transcript.validate({}) == []
    assert transcript.validate({"thinking": "hidden", "close": "none"}) == []
    assert transcript.validate({"thinking": "loud"}) == ["transcript.thinking: unknown value 'loud'; choose from expanded, collapsed, hidden"]
    assert transcript.validate({"close": "dash"}) == ["transcript.close: unknown value 'dash'; choose from rule, blank, none"]


# --- choosing on the tab --------------------------------------------------------------------


async def test_space_chooses_both_look_settings_and_enter_saves_them(command_loop):
    keys = [*move_to("thinking:hidden"), " ", "j", "j", " ", "enter"]  # two rows down is close:blank
    modal = command_loop.presentation.tui = TranscriptModal(keys)
    result = await theme_command(command_loop, "")
    assert result is not None
    assert result.splitlines() == ["transcript.thinking: hidden (saved)", "transcript.close: blank (saved)"]
    assert saved(command_loop) == {"runtime": {"theme": "auto"}, "transcript": {"thinking": "hidden", "close": "blank"}}
    assert Path(command_loop.session.config.path).read_text().startswith("# keep comment")
    assert command_loop.session.config.transcript == {"thinking": "hidden", "close": "blank"}
    assert transcript.thinking(command_loop.session.config) == "hidden"
    assert transcript.close(command_loop.session.config) == "blank"
    # Both rows carry the chosen mark and the wording of the choice they stand for -- and the
    # values they replaced carry neither any more.
    last = frames(modal)[-1]
    assert "* hidden" in last and "* a blank line" in last
    assert "* every line" not in last and "* a full-width line" not in last


async def test_enter_without_a_choice_writes_nothing(command_loop):
    """Moving through the rows is not choosing them."""
    path = Path(command_loop.session.config.path)
    modal = command_loop.presentation.tui = TranscriptModal([*move_to("format:standard"), "j", "enter"])
    assert await theme_command(command_loop, "") is None
    assert saved(command_loop) == {"runtime": {"theme": "auto"}}
    assert "[transcript]" not in path.read_text()
    assert command_loop.session.config.transcript == {}
    assert "* standard" in frames(modal)[-1]


async def test_escape_discards_a_chosen_look_setting(command_loop):
    modal = command_loop.presentation.tui = TranscriptModal([*move_to("close:none"), " ", "escape"])
    assert await theme_command(command_loop, "") is None
    assert any("* nothing" in frame for frame in frames(modal))
    assert saved(command_loop) == {"runtime": {"theme": "auto"}}
    assert command_loop.session.config.transcript == {}
    assert transcript.close(command_loop.session.config) == "rule"


# --- the sample the tab previews -------------------------------------------------------------


async def test_the_sample_follows_the_highlighted_thinking_row(command_loop):
    expanded = await preview_at(command_loop, "thinking:expanded")
    assert "✻ thinking" in expanded
    assert all(any(line in row for row in expanded) for line in transcripts.SAMPLE_REASONING)

    collapsed = await preview_at(command_loop, "thinking:collapsed")
    assert any(transcripts.SAMPLE_REASONING[0] in row for row in collapsed)
    assert not any(transcripts.SAMPLE_REASONING[1] in row for row in collapsed)

    hidden = await preview_at(command_loop, "thinking:hidden")
    assert "✻ thinking" in hidden  # the phase still arrives; only its trace is gone
    assert any("only the divider below names the phase" in row for row in hidden)
    assert not any(line in row for row in hidden for line in transcripts.SAMPLE_REASONING)


async def test_the_sample_follows_the_highlighted_close_row(command_loop):
    rule = await preview_at(command_loop, "close:rule")
    call = next(index for index, row in enumerate(rule) if "cargo bench export" in row)
    # A full-width seam inside the box: the terminal's 80 columns of it, clipped to the box's inner
    # 72 cells with the three-dot ellipsis the clip leaves in their place.
    assert rule[call + 1] == "─" * 69 + "..."

    blank = await preview_at(command_loop, "close:blank")
    call = next(index for index, row in enumerate(blank) if "cargo bench export" in row)
    assert blank[call + 1] == ""  # one blank row, then the note naming it
    assert "(one blank row)" in blank[call + 2]

    none = await preview_at(command_loop, "close:none")
    call = next(index for index, row in enumerate(none) if "cargo bench export" in row)
    assert none[call + 1].strip() == "the next call follows straight on"  # no seam at all


# --- what the live view draws ----------------------------------------------------------------


class RecordingUi:
    """The two seams `close` chooses between, recorded instead of drawn."""

    def __init__(self, due=True):
        self.seams = []
        self.due = due

    def rule_due(self, min_rows):
        return self.due

    def emit_phase_rule(self):
        self.seams.append("rule")

    def separate(self):
        self.seams.append("blank")


def stream_rows(loop):
    return [line for line in "".join(text for _, text in loop.view.model_stream_fragments()).splitlines() if line.startswith(RAIL)]


def test_the_reasoning_preview_keeps_the_rows_the_mode_asks_for(command_loop, monkeypatch):
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((120, 20)))
    command_loop.presentation.model_stream_output("reasoning", "\n".join(f"reasoning line {index}" for index in range(8)))

    def rows(mode):
        monkeypatch.setattr(command_loop.session.config, "transcript", {"thinking": mode})
        return stream_rows(command_loop)

    assert rows("expanded") == [RAIL + f"reasoning line {index}" for index in range(2, 8)]  # the newest six
    assert rows("collapsed") == [RAIL + "reasoning line 0"]  # the opening line, not the newest one
    assert rows("hidden") == []


def test_a_collapsed_trace_skips_a_blank_opening_line(command_loop, monkeypatch):
    """Only the first line is ever shown, so a stream opening with a bare newline must fall to the
    first line that says something -- otherwise `collapsed` draws an empty row for the whole
    answer. Reasoning deltas are appended verbatim, so that opening is ordinary."""
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((120, 20)))
    monkeypatch.setattr(command_loop.session.config, "transcript", {"thinking": "collapsed"})
    command_loop.presentation.model_stream_output("reasoning", "\nweighing the two paths\nand a third")

    assert stream_rows(command_loop) == [RAIL + "weighing the two paths"]

    # A new request clears the trace; with nothing but blank lines so far, the trace is empty
    # rather than an empty rail row.
    command_loop.presentation.model_stream_output("reasoning", "")
    command_loop.presentation.model_stream_output("reasoning", "\n\n")
    assert stream_rows(command_loop) == []


def test_the_answer_preview_ignores_the_thinking_mode(command_loop, monkeypatch):
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((120, 20)))
    monkeypatch.setattr(command_loop.session.config, "transcript", {"thinking": "hidden"})

    command_loop.presentation.model_stream_output("output", "\n".join(f"answer line {index}" for index in range(8)))

    assert stream_rows(command_loop) == [RAIL + f"answer line {index}" for index in range(2, 8)]


def silent_batches(presentation, count):
    for _ in range(count):
        presentation.count_silent_batch()


@pytest.mark.parametrize(("style", "seam"), [("rule", "rule"), ("blank", "blank")])
def test_close_chooses_the_seam_a_silent_run_ends_with(command_loop, monkeypatch, style, seam):
    """A long silent run closes with the seam the table asks for, and the next seam is a full run
    away: one batch short of it draws nothing more."""
    presentation = command_loop.presentation
    presentation.ui = ui = RecordingUi()
    monkeypatch.setattr(command_loop.session.config, "transcript", {"close": style})
    run = presentation.TOOL_RUN_RULE_BATCHES

    silent_batches(presentation, run - 1)
    assert ui.seams == []
    silent_batches(presentation, 1)
    assert ui.seams == [seam]
    silent_batches(presentation, run - 1)
    assert ui.seams == [seam]
    silent_batches(presentation, 1)
    assert ui.seams == [seam, seam]


def test_close_none_draws_no_seam_and_still_restarts_the_run(command_loop, monkeypatch):
    """`none` is a real choice, not a missing seam: switched back mid-session, the next seam waits
    a full run after the one `none` swallowed."""
    presentation = command_loop.presentation
    presentation.ui = ui = RecordingUi()
    monkeypatch.setattr(command_loop.session.config, "transcript", {"close": "none"})
    run = presentation.TOOL_RUN_RULE_BATCHES

    silent_batches(presentation, run)
    assert ui.seams == []
    monkeypatch.setattr(command_loop.session.config, "transcript", {"close": "rule"})
    silent_batches(presentation, run - 1)
    assert ui.seams == []
    silent_batches(presentation, 1)
    assert ui.seams == ["rule"]


def test_a_run_too_close_to_the_rule_above_draws_no_seam_whatever_close_says(command_loop, monkeypatch):
    """The distance holds the seam back for every style, and the count keeps running, so the seam
    lands on the batch that finally clears it."""
    presentation = command_loop.presentation
    presentation.ui = ui = RecordingUi(due=False)
    monkeypatch.setattr(command_loop.session.config, "transcript", {"close": "blank"})

    silent_batches(presentation, presentation.TOOL_RUN_RULE_BATCHES)
    assert ui.seams == []
    ui.due = True
    silent_batches(presentation, 1)
    assert ui.seams == ["blank"]
