"""Tabbed appearance previews, cancellation and persistence at the command boundary."""

import asyncio
import tomllib
from pathlib import Path

import pytest
from prompt_toolkit.document import Document
from test_command_ui import ModalHarness
from tui_harness import loop

from wizolt.ui.cli.appearance import theme_command
from wizolt.ui.cli.commands import COMMAND_NAMES
from wizolt.ui.cli.view import CommandCompleter
from wizolt.ui.render import InputStyle, Theme


class BarModal(ModalHarness):
    def invalidate(self):
        pass

    def recolor(self):
        pass

    def set_input_style(self, style):
        self.input_style = style


@pytest.fixture
def command_loop(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(Theme, "_mode", "dark")
    monkeypatch.setattr(Theme, "_diff_style", "auto")
    monkeypatch.setattr(Theme, "_custom", {})
    command_loop = loop(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text('# keep comment\n[runtime]\ntheme = "auto"\n')
    command_loop.session.config.path = str(path)
    command_loop.interactive_input = True
    return command_loop


def saved(command_loop):
    return tomllib.loads(Path(command_loop.session.config.path).read_text())


async def test_statusbar_preview_escape_restores_custom_template(command_loop):
    layout = command_loop.presentation.status_bar.layout
    assert not layout.configure({"statusbar": "{model} custom"}, Theme.bar_styles)

    class LiveStatusbar(BarModal):
        async def show_modal(self, fragments_fn, key_fn, **kwargs):
            self.status_frames = []

            def frame():
                self.status_frames.append(command_loop.presentation.status_bar.fragments())
                return fragments_fn()

            return self._drive(frame, key_fn, **kwargs)

    modal = command_loop.presentation.tui = LiveStatusbar(["l", "l", "up", "escape"])
    assert await theme_command(command_loop, "") is None
    assert command_loop.presentation.status_bar.layout is layout
    assert layout.sources["statusbar"] == "{model} custom"
    assert any("" in fragment[1] for fragment in modal.status_frames[-2])
    assert all("" not in fragment[1] for frame in modal.frames for fragment in frame)
    assert "ui" not in saved(command_loop)


async def test_tabs_save_statusbar_layout_and_sweep_together(command_loop, monkeypatch):
    monkeypatch.setattr("wizolt.ui.cli.appearance.picker_height", lambda: 24)
    modal = command_loop.presentation.tui = BarModal(["right", "right", "j", "l", "j", " ", "tab", "j", " ", "enter"], consumed=True)
    result = await theme_command(command_loop, "")
    assert "statusbar.format: preset:minimal" in result
    assert "divider.format: preset:capsule" in result
    assert "divider.sweep: preset:ripple" in result
    assert saved(command_loop)["ui"] == {"statusbar": {"format": "preset:minimal"}, "divider": {"format": "preset:capsule", "sweep": "preset:ripple"}}
    frames = ["".join(fragment[1] for fragment in frame) for frame in modal.frames]
    assert all("Colorscheme" in frame and "StatusBar" in frame for frame in frames)
    assert any("Running (preview)" in frame and "Queued (preview)" in frame for frame in frames)
    assert modal.pos == 10


async def test_escape_after_switching_tabs_restores_everything(command_loop):
    layout = command_loop.presentation.status_bar.layout
    before = dict(layout.sources)
    command_loop.presentation.tui = BarModal(["j", "l", "j", "l", "j", "l", "j", "escape"])
    assert await theme_command(command_loop, "") is None
    assert command_loop.presentation.status_bar.layout is layout
    assert layout.sources == before and Theme.name() == "dark" and Theme.selected_diff_style() == "auto"
    assert "ui" not in saved(command_loop)


async def test_search_letters_do_not_switch_tabs_and_tab_focus_is_retained(command_loop):
    modal = command_loop.presentation.tui = BarModal(["l", "l", "/", "l", "u", "a", "escape", "right", "left", "enter"])
    await theme_command(command_loop, "")
    assert saved(command_loop)["ui"]["statusbar"]["format"] == "preset:lualine"
    assert "/lua (filtered)" in "".join(text for frame in modal.frames for _, text in frame)


async def test_older_presets_and_custom_sweep_can_be_kept(command_loop):
    layout = command_loop.presentation.status_bar.layout
    assert not layout.configure({"divider": "preset:plain", "sweep": "0.5"}, Theme.bar_styles)
    modal = command_loop.presentation.tui = BarModal(["h", "h", "g", "j", "j", "j", "j", "j", "G", "enter"])
    assert await theme_command(command_loop, "") is None
    rendered = "".join(text for frame in modal.frames for _, text in frame)
    assert "current (plain)" in rendered and "custom (current)" in rendered
    assert "dashed" not in rendered and "breathe" not in rendered
    assert layout.sources["divider"] == "preset:plain" and layout.sources["sweep"] == "0.5"
    assert "ui" not in saved(command_loop)


async def test_divider_picker_selects_powerline(command_loop):
    command_loop.presentation.tui = BarModal(["left", "left", "j", "j", "j", "j", " ", "enter"])
    assert "divider.format: preset:powerline" in await theme_command(command_loop, "")
    assert saved(command_loop)["ui"]["divider"]["format"] == "preset:powerline"


async def test_interrupted_preview_restores_layout_and_stops_animation(command_loop):
    class Interrupted(BarModal):
        async def show_modal(self, fragments_fn, key_fn, **kwargs):
            key_fn("left")
            key_fn("left")
            key_fn("j")
            await asyncio.sleep(0)
            raise asyncio.CancelledError

    layout = command_loop.presentation.status_bar.layout
    command_loop.presentation.tui = Interrupted([])
    before = set(asyncio.all_tasks())
    with pytest.raises(asyncio.CancelledError):
        await theme_command(command_loop, "")
    assert command_loop.presentation.status_bar.layout is layout
    assert set(asyncio.all_tasks()) == before


async def test_direct_forms_and_headless_listing_reject_unknown_values(command_loop):
    command_loop.interactive_input = False
    assert "powerline" in await theme_command(command_loop, "")
    before = dict(command_loop.presentation.status_bar.layout.sources)
    assert "unknown preset" in await theme_command(command_loop, "statusbar not-a-preset")
    assert "Unknown diff style" in await theme_command(command_loop, "diff not-a-style")
    assert command_loop.presentation.status_bar.layout.sources == before
    for args in ("statusbar minimal", "divider frame", "sweep none", "diff classic"):
        assert "saved" in await theme_command(command_loop, args)
    assert saved(command_loop)["ui"] == {
        "statusbar": {"format": "preset:minimal"},
        "divider": {"format": "preset:frame", "sweep": "preset:none"},
        "diff": {"style": "classic"},
    }


async def test_selection_recovers_from_a_malformed_ui_section(command_loop):
    command_loop.session.config.ui = {"statusbar": "broken"}
    Path(command_loop.session.config.path).write_text('[ui]\nstatusbar = "broken"\n')
    result = await theme_command(command_loop, "statusbar minimal")
    assert "Applied for this session; not saved" in result
    assert command_loop.session.config.ui["statusbar"] == {"format": "preset:minimal"}


def test_idle_preview_uses_idle_values_even_without_a_condition(command_loop):
    from wizolt.ui.cli.bars import preview

    layout = command_loop.presentation.status_bar.layout
    assert not layout.configure({"divider": "{label}|{rate}|{spinner}|{elapsed:duration}"}, Theme.bar_styles)
    rows = "".join(fragment[1] for fragment in preview(command_loop, "divider", 0)).splitlines()
    assert rows[rows.index("Idle (preview)") + 1] == "|||0s"
    assert rows[rows.index("Running (preview)") - 1] == "" and rows[rows.index("Queued (preview)") - 1] == ""


def test_appearance_command_completion():
    completer = CommandCompleter()

    def choices(text):
        return [item.text for item in completer.get_completions(Document(text), None)]

    assert "/statusbar" not in COMMAND_NAMES and "/divider" not in COMMAND_NAMES
    assert "statusbar " in choices("/theme ") and "kanagawa" in choices("/theme ")
    assert choices("/theme statusbar pow") == ["powerline"]
    assert choices("/theme divider fra") == ["frame"]
    assert choices("/theme sweep aur") == ["aurora"]
    assert choices("/theme diff cla") == ["classic"]
    assert choices("/theme input che") == ["chevron"]


async def test_switching_tabs_and_search_keeps_the_picker_height(command_loop, monkeypatch):
    import wizolt.ui.cli.appearance as appearance

    monkeypatch.setattr(appearance, "picker_height", lambda: 24)
    modal = command_loop.presentation.tui = BarModal(["l", "l", "l", "l", "e", "tab", "escape", "h", "/", "z", "z", "escape", "escape", "h", "h", "escape"])
    await theme_command(command_loop, "")
    heights = [1 + sum(text.count("\n") for _, text in frame) for frame in modal.frames]
    assert heights == [24] * len(heights)


async def test_small_pane_prioritizes_choices_over_the_sample(command_loop, monkeypatch):
    import wizolt.ui.cli.appearance as appearance

    monkeypatch.setattr(appearance, "picker_height", lambda: 14)
    modal = command_loop.presentation.tui = BarModal(["escape"])
    await theme_command(command_loop, "")
    text = "".join(text for _, text in modal.frames[0])
    assert "showing 1-6 of" in text
    assert "def " in text and "Enter save" in text
    assert "Color samples · 1/8 shown" in text and "enlarge to see all" in text
    assert text.splitlines()[1].strip().startswith("h/l")
    assert "customization" in text.splitlines()[2] and "config" in text.splitlines()[2]
    assert 1 + text.count("\n") == 14


@pytest.mark.parametrize("height", [4, 8, 14])
def test_appearance_tabs_fit_after_the_terminal_shrinks(command_loop, monkeypatch, height):
    import wizolt.ui.cli.appearance as appearance

    monkeypatch.setattr(appearance, "picker_height", lambda: 24)
    picker = appearance.AppearancePicker(command_loop, 0)
    try:
        picker.fragments()
        monkeypatch.setattr(appearance, "picker_height", lambda: height)
        for index, kind in enumerate(appearance.TAB_KINDS):
            picker.tabs.tab = index
            text = "".join(text for _, text in picker.fragments())
            assert 1 + text.count("\n") <= height, kind
            assert "Enter save" in text and "Esc cancel" in text
        picker.handle_key("e")
        assert 1 + sum(text.count("\n") for _, text in picker.fragments()) <= height
    finally:
        picker.restore()


async def test_choosing_sweep_preserves_pinned_layout_while_crossing_other_layouts(command_loop):
    modal = command_loop.presentation.tui = BarModal(["h", "h", "j", " ", "j", "j", "j", "j", "j", "j", " ", "enter"])
    await theme_command(command_loop, "")
    assert saved(command_loop)["ui"]["divider"] == {"format": "preset:capsule", "sweep": "preset:ripple"}
    assert any("* capsule" in text for frame in modal.frames for _, text in frame)


async def test_group_jump_keeps_unconfirmed_previews_out_of_saved_layout(command_loop):
    command_loop.presentation.tui = BarModal(["h", "h", "j", "tab", "j", " ", "enter"])
    await theme_command(command_loop, "")
    assert saved(command_loop)["ui"]["divider"] == {"sweep": "preset:ripple"}
    assert command_loop.presentation.status_bar.layout.sources["divider"] == "preset:comet"


async def test_space_marks_chosen_values_and_enter_does_not_save_a_hover(command_loop):
    modal = command_loop.presentation.tui = BarModal(["h", "h", "j", " ", "tab", "j", " ", "j", "enter"])
    await theme_command(command_loop, "")
    assert saved(command_loop)["ui"]["divider"] == {"format": "preset:capsule", "sweep": "preset:ripple"}
    chosen = "".join(text for _, text in modal.frames[-2])
    assert "Layout: capsule · Sweep: ripple" in chosen and "  aurora" in chosen
    assert any("* ripple" in text for frame in modal.frames for _, text in frame)


@pytest.mark.parametrize("name,prefix", list(InputStyle.PRESETS.items()))
async def test_input_presets_save_and_restore_from_config(command_loop, name, prefix):
    command_loop.session.config.ui["input"] = {"prompt": "custom ", "running": "follow-up "}
    Path(command_loop.session.config.path).write_text('[ui.input]\nprompt = "custom "\nrunning = "follow-up "\n')
    assert "saved as ui.input" in await theme_command(command_loop, "input " + name)
    assert saved(command_loop)["ui"]["input"] == {"prompt": "preset:" + name}
    command_loop.configure_theme()
    assert command_loop.presentation.input_style.prefix() == prefix
    assert command_loop.presentation.input_style.prefix(running=True) == "+" + prefix


async def test_input_picker_previews_a_preset_and_escape_restores_custom_prefixes(command_loop):
    original = command_loop.presentation.input_style = InputStyle("chat ", "queue ")
    modal = command_loop.presentation.tui = BarModal(["h", "g", "j", "escape"])
    assert await theme_command(command_loop, "") is None
    assert command_loop.presentation.input_style == original and modal.input_style == original
    assert any("❯ " in text for frame in modal.frames for _, text in frame)
    assert "ui" not in saved(command_loop)


async def test_input_custom_edit_returns_to_list_and_enter_saves_instead_of_editing_again(command_loop):
    modal = command_loop.presentation.tui = BarModal(["h", "e", "c-u", "λ", " ", "tab", "c-u", "→", " ", "enter", "enter"], consumed=True)
    result = await theme_command(command_loop, "")
    assert "saved as ui.input" in result
    assert modal.pos == 11
    assert saved(command_loop)["ui"]["input"] == {"prompt": "λ ", "running": "→ "}
    assert command_loop.presentation.input_style == InputStyle("λ ", "→ ")
    assert any("* custom" in text for _, text in modal.frames[-2])
    assert "e edit" in "".join(text for _, text in modal.frames[-2])
    # Opening the picker again on a custom row must also let Enter close it.
    modal = command_loop.presentation.tui = BarModal(["h", "enter"], consumed=True)
    assert await theme_command(command_loop, "") is None
    assert modal.pos == 2 and "Tab field" not in "".join(text for frame in modal.frames for _, text in frame)


@pytest.mark.parametrize("ending", ["escape", "c-c"])
async def test_cancel_or_interrupt_discards_custom_input_edits(command_loop, ending):
    original = command_loop.presentation.input_style = InputStyle("❯ ", "follow ")
    command_loop.presentation.tui = BarModal(["h", "e", "c-u", "x", ending, "escape"])
    if ending == "c-c":
        with pytest.raises(KeyboardInterrupt):
            await theme_command(command_loop, "")
    else:
        assert await theme_command(command_loop, "") is None
    assert command_loop.presentation.input_style == original
    assert "ui" not in saved(command_loop)


async def test_applied_custom_input_can_still_be_cancelled_before_saving(command_loop):
    command_loop.presentation.tui = BarModal(["h", "e", "c-u", "x", "enter", "escape"])
    assert await theme_command(command_loop, "") is None
    assert command_loop.presentation.input_style == InputStyle()
    assert "ui" not in saved(command_loop)


async def test_custom_input_editor_rejects_an_overwide_prefix_until_corrected(command_loop):
    modal = command_loop.presentation.tui = BarModal(["h", "e", "c-u", *(["界"] * 17), "enter", "backspace", "enter", "enter"])
    await theme_command(command_loop, "")
    assert saved(command_loop)["ui"]["input"]["prompt"] == "界" * 16
    assert any("at most 32 columns" in text for frame in modal.frames for _, text in frame)


async def test_invalid_input_preset_is_rejected_without_mutating_settings(command_loop):
    original = command_loop.presentation.input_style
    assert "Unknown input preset" in await theme_command(command_loop, "input nope")
    assert command_loop.presentation.input_style == original and "ui" not in saved(command_loop)
