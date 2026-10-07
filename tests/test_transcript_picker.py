"""The `/theme` Transcript tab: the record formats it offers, the sample it previews, and the
format panel that edits one.

The tab draws a record through the same engine the transcript uses, so the sample is asserted as
text -- which rows a preset produces -- rather than as styles.
"""

import json
import os
import tomllib
from pathlib import Path

import pytest
from test_command_ui import ModalHarness
from tui_harness import loop

from wizolt.tools import transcript
from wizolt.ui.cli import appearance, transcripts
from wizolt.ui.cli.appearance import CUSTOM, theme_command
from wizolt.ui.render import Theme

# A template no preset names, as the format panel's draft becomes one.
CUSTOM_TEMPLATE = "{name} {args} ({duration})"


class TranscriptModal(ModalHarness):
    recolored = 0

    def invalidate(self):
        pass

    def recolor(self):
        self.recolored += 1

    def set_input_style(self, style):
        self.input_style = style


@pytest.fixture
def command_loop(tmp_path, monkeypatch):
    return transcript_loop(tmp_path, monkeypatch)


def transcript_loop(tmp_path, monkeypatch):
    """A command loop with a config file on disk, at a fixed terminal size and colour mode."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(Theme, "_mode", "dark")
    monkeypatch.setattr(Theme, "_diff_style", "auto")
    monkeypatch.setattr(Theme, "_custom", {})
    monkeypatch.setattr(Theme, "_bar_themes", {"statusbar": "inherit", "divider": "inherit"})
    # The picker's geometry decides whether the sample is framed and how wide its rows are, so a
    # developer's own terminal must not change what these tests read.
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback=None: os.terminal_size((80, 30)))
    monkeypatch.setattr(appearance, "picker_height", lambda: 24)
    command_loop = loop(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text('# keep comment\n[runtime]\ntheme = "auto"\n')
    command_loop.session.config.path = str(path)
    command_loop.interactive_input = True
    return command_loop


def saved(command_loop):
    return tomllib.loads(Path(command_loop.session.config.path).read_text())


def configure_transcript(command_loop, source):
    """A `[transcript] format` on disk and in the running config, the way a hand-edited file
    leaves both; `json.dumps` escapes exactly what a TOML basic string escapes."""
    command_loop.session.config.transcript = {"format": source}
    Path(command_loop.session.config.path).write_text(f'# keep comment\n[runtime]\ntheme = "auto"\n[transcript]\nformat = {json.dumps(source)}\n')


def frames(modal):
    return ["".join(fragment[1] for fragment in frame) for frame in modal.frames]


def clear_and_type(template):
    """Keys replacing the panel's draft -- which opens as the `minimal` preset's own body -- with
    `template`."""
    return [*(["backspace"] * len(transcript.PRESETS["minimal"])), *template]


# --- the tab's rows -------------------------------------------------------------------------


async def test_the_tab_lists_the_engine_presets_with_the_selected_one_marked(command_loop):
    modal = command_loop.presentation.tui = TranscriptModal(["h"])
    await theme_command(command_loop, "")
    text = frames(modal)[-1]
    assert "1. * standard   the built-in rendering" in text  # standard has no rows of its own
    assert "2.   minimal   {marker} {name} {args}" in text  # a preset row shows the rows it stands for
    assert "custom (f to edit)" not in text


async def test_a_custom_row_appears_only_when_a_custom_format_is_saved(command_loop):
    modal = command_loop.presentation.tui = TranscriptModal(["h"])
    await theme_command(command_loop, "")
    assert "custom (f to edit)" not in frames(modal)[-1]
    configure_transcript(command_loop, "{tool} {args}")
    modal = command_loop.presentation.tui = TranscriptModal(["h"])
    await theme_command(command_loop, "")
    text = frames(modal)[-1]
    assert "3. * custom (f to edit)" in text


# --- the sample ----------------------------------------------------------------------------


async def test_the_sample_renders_the_record_for_the_highlighted_row(command_loop):
    modal = command_loop.presentation.tui = TranscriptModal(["h", "j"])
    await theme_command(command_loop, "")
    standard, minimal = frames(modal)[1], frames(modal)[2]
    # `standard` is the builtin assembly itself: the call line, the stream's tail and its trailer,
    # exactly as a settled Bash call prints.
    assert "Bash  rg -n export_rows src → tr.12 [auto]" in standard
    assert "├ src/db/rows.rs:140:     rows.push(row);" in standard
    assert "└ … +2 more lines · Ctrl-O for more" in standard
    # The checklist preset is the whole record on its call line.
    assert "● bash  rg -n export_rows src → tr.12 [auto]" in minimal
    assert "rows.push(row);" not in minimal


# --- saving and cancelling ------------------------------------------------------------------


async def test_enter_saves_the_global_format_and_keeps_the_rest_of_the_file(command_loop):
    modal = command_loop.presentation.tui = TranscriptModal(["h", "j", " ", "enter"])
    result = await theme_command(command_loop, "")
    assert result is not None and "transcript.format: preset:minimal (saved)" in result
    assert any("2. * minimal" in frame for frame in frames(modal))
    assert saved(command_loop) == {"runtime": {"theme": "auto"}, "transcript": {"format": "preset:minimal"}}
    assert Path(command_loop.session.config.path).read_text().startswith("# keep comment")
    assert command_loop.session.config.transcript == {"format": "preset:minimal"}
    assert transcript.effective_format(command_loop.session.config, "Bash") == "preset:minimal"
    assert modal.recolored == 1, "the records already on screen are redrawn in the saved format"


async def test_saving_only_thinking_leaves_the_printed_transcript_alone(command_loop):
    """Thinking shapes the live reasoning preview, which nothing printed carries."""
    modal = command_loop.presentation.tui = TranscriptModal(["h", "j", "j", "j", " ", "enter"])
    result = await theme_command(command_loop, "")
    assert result is not None and "transcript.thinking: collapsed (saved)" in result
    assert modal.recolored == 0


async def test_saving_the_tool_run_divider_redraws_the_printed_transcript(command_loop):
    """Run seams already printed -- resumed history included -- read the divider at each redraw,
    so saving one asks for the redraw that reaches them."""
    modal = command_loop.presentation.tui = TranscriptModal(["h", *(["j"] * 6), " ", "enter"])
    result = await theme_command(command_loop, "")
    assert result is not None and "transcript.close: blank (saved)" in result
    assert modal.recolored == 1


async def test_escape_discards_the_selected_format(command_loop):
    modal = command_loop.presentation.tui = TranscriptModal(["h", "j", " ", "escape"])
    assert await theme_command(command_loop, "") is None
    assert any("1. * standard" in frame for frame in frames(modal))
    assert saved(command_loop) == {"runtime": {"theme": "auto"}}
    assert command_loop.session.config.transcript == {}
    assert transcript.effective_format(command_loop.session.config, "Bash") == ""


# --- the format panel -------------------------------------------------------------------------


async def test_f_opens_the_format_panel_on_the_transcript_setting(command_loop):
    modal = command_loop.presentation.tui = TranscriptModal(["h", "f", "e"])
    assert await theme_command(command_loop, "") is None
    view, draft = frames(modal)[-2], frames(modal)[-1]
    assert "transcript.format · selected, saved" in view
    assert "c copy value · t copy TOML · e edit" in view
    # `standard` is the builtin assembly: there are no rows to show it expanding to.
    assert "value" in view and "preset:standard" in view and "expands to" not in view
    assert "transcript.format · draft" in draft and "Ctrl-S apply · Esc discard" in draft
    assert "└ … +2 more lines · Ctrl-O for more" in draft  # the sample beside it is the selection's


async def test_f_on_a_preset_edits_the_rows_it_stands_for(command_loop):
    modal = command_loop.presentation.tui = TranscriptModal(["h", "j", "f", "e"])
    assert await theme_command(command_loop, "") is None
    view, draft = frames(modal)[-2], frames(modal)[-1]
    assert "expands to" in view and transcript.PRESETS["minimal"] in view
    assert transcript.PRESETS["minimal"] in draft


async def test_ctrl_s_accepts_a_custom_template_and_enter_saves_it(command_loop):
    keys = ["h", " ", "j", " ", "f", "e", *clear_and_type(CUSTOM_TEMPLATE), "c-s", "escape", "enter"]
    modal = command_loop.presentation.tui = TranscriptModal(keys)
    result = await theme_command(command_loop, "")
    assert result is not None and f"transcript.format: {CUSTOM_TEMPLATE} (saved)" in result
    # The panel keeps the accepted template as its value and says what Enter will do with it.
    assert any("selected, not saved yet" in frame and CUSTOM_TEMPLATE in frame for frame in frames(modal))
    assert any("Draft applied. Enter in the picker saves it" in frame for frame in frames(modal))
    assert saved(command_loop)["transcript"] == {"format": CUSTOM_TEMPLATE}
    assert command_loop.session.config.transcript == {"format": CUSTOM_TEMPLATE}


def test_ctrl_s_makes_an_edited_template_the_selection(command_loop):
    picker = appearance.AppearancePicker(command_loop, 0)
    try:
        for key in ("h", "j", " ", "f", "e", *clear_and_type(CUSTOM_TEMPLATE), "c-s"):
            picker.handle_key(key, key if len(key) == 1 else "")
        assert picker.format is not None and picker.format.draft is None
        assert picker.selected["transcript"] == CUSTOM
        assert picker.custom["transcript"] == CUSTOM_TEMPLATE
        assert picker.transcript_source(picker.selected["transcript"]) == CUSTOM_TEMPLATE
    finally:
        picker.restore()


async def test_a_malformed_draft_is_reported_and_never_applied(command_loop):
    keys = ["h", "j", "f", "e", "{", "n", "o", "p", "e", "}", "c-s", "escape", "escape", "escape"]
    modal = command_loop.presentation.tui = TranscriptModal(keys)
    assert await theme_command(command_loop, "") is None
    text = frames(modal)
    assert any("unknown field or format 'nope'" in frame for frame in text)
    assert any("Fix the errors above before applying." in frame for frame in text)
    assert any("Draft discarded." in frame for frame in text)
    assert "1. * standard" in text[-2]  # the panel closed onto the tab it was opened from
    assert saved(command_loop) == {"runtime": {"theme": "auto"}}
    assert command_loop.session.config.transcript == {}


def test_an_unknown_preset_opens_and_edits_its_own_text(command_loop):
    """A preset name this build does not have must not break a key handler: the panel shows the
    value as the text it is, and `e` edits that text rather than expanding a name it cannot."""
    configure_transcript(command_loop, "preset:compact")
    picker = appearance.AppearancePicker(command_loop, 0)
    try:
        for key in ("h", "f"):
            picker.handle_key(key, key)
        assert picker.format is not None
        view = "".join(fragment[1] for row in picker.format.rows(76, 20) for fragment in row)
        # Shown as its own text: no expansion block, because there is nothing it expands to.
        assert "preset:compact" in view and "expands to" not in view
        picker.handle_key("e", "e")
        assert picker.format.draft is not None and picker.format.draft.text == "preset:compact"
        picker.handle_key("escape", "escape")
        assert picker.format is not None and picker.format.draft is None
    finally:
        picker.restore()


def test_a_draft_accepted_in_the_panel_keeps_its_custom_row(command_loop):
    """After Ctrl-S the edited template is the selection, so the tab must show the row holding it
    -- otherwise the list would star a preset while Enter saved something else."""
    picker = appearance.AppearancePicker(command_loop, 0)
    try:
        for key in ("h", "j", " ", "f", "e", *clear_and_type(CUSTOM_TEMPLATE), "c-s"):
            picker.handle_key(key, key if len(key) == 1 else "")
        picker.handle_key("escape", "escape")
        state = picker.current_list()
        assert state.choices == (
            "format",
            "format:standard",
            "format:minimal",
            "format:custom",
            "thinking",
            "thinking:expanded",
            "thinking:collapsed",
            "thinking:hidden",
            "close",
            "close:rule",
            "close:blank",
            "close:none",
        )
        assert state.selected_choice() == "format:custom"
        assert picker.selected["transcript"] == CUSTOM
        assert picker.transcript_source(CUSTOM) == CUSTOM_TEMPLATE
    finally:
        picker.restore()


def test_a_custom_format_edited_to_a_preset_body_is_that_preset(command_loop):
    """Starting from the custom row does not make a preset's own shape custom: saved as the
    template text, `standard` would leave the builtin assembly for the template engine."""
    from prompt_toolkit.document import Document

    configure_transcript(command_loop, CUSTOM_TEMPLATE)
    picker = appearance.AppearancePicker(command_loop, 0)
    try:
        for key in ("h", "f", "e"):
            picker.handle_key(key, key)
        assert picker.format is not None and picker.format.draft is not None
        picker.format.draft = Document(transcript.PRESETS["standard"])
        picker.handle_key("c-s", "")
        assert picker.selected["transcript"] == "standard"
        assert picker.transcript_source(picker.selected["transcript"]) == "preset:standard"
    finally:
        picker.restore()


def test_escape_from_the_format_panel_closes_it_without_applying(command_loop):
    picker = appearance.AppearancePicker(command_loop, 0)
    try:
        for key in ("h", "f"):
            picker.handle_key(key, key)
        panel = picker.format
        assert panel is not None and panel.setting == "transcript.format"
        panel.handle_key("e", "e")
        assert panel.draft is not None and panel.draft.text == ""  # standard has no rows: one starts from nothing
        panel.handle_key("escape", "escape")  # the draft goes, the panel stays
        assert panel.draft is None and picker.format is panel
        picker.handle_key("escape", "escape")  # through the picker: the panel is what closes
        assert picker.format is None
        assert picker.selected["transcript"] == "standard"
    finally:
        picker.restore()
    assert transcripts.saved(command_loop) == "preset:standard"


# --- a broken format --------------------------------------------------------------------------


def test_a_malformed_format_previews_a_problem_line_instead_of_raising(command_loop):
    text = "".join(fragment[1] for fragment in transcripts.preview(command_loop.session, "{nope}", 60))
    assert text.strip() == transcripts.problems("{nope}")[0] == "row 1: line 1, column 1: unknown field or format 'nope'"


async def test_a_malformed_format_in_the_config_shows_its_problem_in_the_sample(command_loop):
    configure_transcript(command_loop, "{nope}")
    modal = command_loop.presentation.tui = TranscriptModal(["h"])
    assert await theme_command(command_loop, "") is None
    assert "unknown field or format 'nope'" in frames(modal)[-1]


# --- the helpers the tab leans on --------------------------------------------------------------


def test_a_preset_body_counts_as_its_preset_name():
    assert transcripts.body("preset:minimal") == transcript.PRESETS["minimal"]
    assert transcripts.body("{tool} {args}") == "{tool} {args}"
    assert transcripts.name("preset:minimal", CUSTOM) == "minimal"
    assert transcripts.name(transcript.PRESETS["minimal"], CUSTOM) == "minimal"
    assert transcripts.name("{marker} {name} {args} ", CUSTOM) == CUSTOM
    assert transcripts.name("{tool} {args}", CUSTOM) == CUSTOM


def test_an_unset_or_blank_format_is_the_standard_preset(command_loop):
    assert transcripts.saved(command_loop) == "preset:standard"
    for table in ({}, {"format": ""}, {"format": 3}, "not a table"):
        command_loop.session.config.transcript = table
        assert transcripts.saved(command_loop) == "preset:standard"


def test_an_unknown_preset_reads_as_a_custom_row_and_its_sample_names_the_problem(command_loop):
    """A config naming a preset this build does not have keeps its own value, and the tab reads it
    as a custom row. Its sample says what is wrong, as the config check does, instead of drawing a
    record the user did not ask for."""
    configure_transcript(command_loop, "preset:compact")
    assert transcripts.saved(command_loop) == "preset:compact"
    assert transcripts.name(transcripts.saved(command_loop), CUSTOM) == CUSTOM
    assert transcripts.body("preset:compact") == "preset:compact"
    sample = "".join(fragment[1] for fragment in transcripts.preview(command_loop.session, "preset:compact", 60))
    assert "unknown preset 'compact'" in sample
