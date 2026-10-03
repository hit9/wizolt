"""Tabbed appearance previews, cancellation and persistence at the command boundary."""

import asyncio
import os
import tomllib
from pathlib import Path

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.utils import get_cwidth
from test_command_ui import ModalHarness
from tui_harness import loop

from wizolt.ui.bars import DIVIDER_PRESETS, STATUS_PRESETS, SWEEPS, status_template
from wizolt.ui.cli.appearance import AppearancePicker, theme_command
from wizolt.ui.cli.commands import COMMAND_NAMES
from wizolt.ui.cli.formats import FormatPanel
from wizolt.ui.cli.view import CommandCompleter
from wizolt.ui.render import InputStyle, Theme
from wizolt.utils.clipboard import Clipboard


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
    monkeypatch.setattr(Theme, "_bar_themes", {"statusbar": "inherit", "divider": "inherit"})
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
    modal = command_loop.presentation.tui = BarModal(["right", "right", "j", " ", "l", "j", " ", "tab", "j", " ", "enter"], consumed=True)
    result = await theme_command(command_loop, "")
    assert "statusbar.format: preset:minimal" in result
    assert "divider.format: preset:capsule" in result
    assert "divider.sweep: preset:ripple" in result
    assert saved(command_loop)["ui"] == {"statusbar": {"format": "preset:minimal"}, "divider": {"format": "preset:capsule", "sweep": "preset:ripple"}}
    frames = ["".join(fragment[1] for fragment in frame) for frame in modal.frames]
    assert all("Colorscheme" in frame and "StatusBar" in frame for frame in frames)
    assert any("Running (preview)" in frame and "Queued (preview)" in frame for frame in frames)
    assert modal.pos == 11


async def test_escape_after_switching_tabs_restores_everything(command_loop):
    layout = command_loop.presentation.status_bar.layout
    before = dict(layout.sources)
    command_loop.presentation.tui = BarModal(["j", "l", "j", "l", "j", "l", "j", "escape"])
    assert await theme_command(command_loop, "") is None
    assert command_loop.presentation.status_bar.layout is layout
    assert layout.sources == before and Theme.name() == "dark" and Theme.selected_diff_style() == "auto"
    assert "ui" not in saved(command_loop)


async def test_search_letters_do_not_switch_tabs_and_tab_focus_is_retained(command_loop):
    modal = command_loop.presentation.tui = BarModal(["l", "l", "/", "l", "u", "a", "escape", " ", "right", "left", "enter"])
    await theme_command(command_loop, "")
    assert saved(command_loop)["ui"]["statusbar"]["format"] == "preset:lualine"
    assert "/lua (filtered)" in "".join(text for frame in modal.frames for _, text in frame)


async def test_older_presets_and_custom_sweep_can_be_kept(command_loop):
    layout = command_loop.presentation.status_bar.layout
    assert not layout.configure({"divider": "preset:plain", "sweep": "0.5"}, Theme.bar_styles)
    modal = command_loop.presentation.tui = BarModal(["h", "h", "g", "tab", "enter"])
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


@pytest.mark.parametrize("preset", DIVIDER_PRESETS)
def test_every_divider_previews_the_same_breathing_dot_as_live_activity(command_loop, monkeypatch, preset):
    from prompt_toolkit.styles import Style

    from wizolt.ui.cli.bars import preview
    from wizolt.ui.render import ActivityPulse

    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    layout = command_loop.presentation.status_bar.layout
    assert not layout.configure({"divider": "preset:" + preset, "sweep": "preset:none"}, Theme.bar_styles)
    command_loop.session.state.current_model_call_started_at = 1
    colors = []
    for now in (0.0, ActivityPulse.PERIOD / 2, ActivityPulse.PERIOD):
        clock[0] = now
        sample = preview(command_loop, "divider", 0)
        live = command_loop.view.queue_divider_fragments()
        sample_dots = [Style([]).get_attrs_for_style_str(style).color for style, text in sample if "●" in text]
        live_dots = [Style([]).get_attrs_for_style_str(style).color for style, text in live if "●" in text]
        assert len(sample_dots) == 2  # Running and Queued; Idle never claims activity.
        assert len(live_dots) == 1 and sample_dots == live_dots * 2
        red, green, blue = (int(live_dots[0][offset : offset + 2], 16) for offset in (0, 2, 4))
        assert green > red and green > blue
        colors.append(live_dots[0])
    assert colors[0] == colors[2] != colors[1]
    assert "●" not in "".join(text for _, text in command_loop.view.idle_divider_fragments())
    command_loop.session.state.current_model_call_started_at = 0
    assert "●" not in "".join(text for _, text in command_loop.view.queue_divider_fragments())


def test_appearance_command_completion():
    completer = CommandCompleter()

    def choices(text):
        return [item.text for item in completer.get_completions(Document(text), None)]

    assert "/statusbar" not in COMMAND_NAMES and "/divider" not in COMMAND_NAMES
    assert "statusbar " in choices("/theme ") and "plum" in choices("/theme ")
    assert choices("/theme statusbar pow") == ["powerline"]
    assert choices("/theme divider fra") == ["frame"]
    assert choices("/theme sweep aur") == ["aurora"]
    assert choices("/theme diff cla") == ["classic"]
    assert choices("/theme input che") == ["chevron"]


async def test_switching_tabs_and_search_keeps_the_picker_height(command_loop, monkeypatch):
    from wizolt.ui.cli import appearance

    monkeypatch.setattr(appearance, "picker_height", lambda: 24)
    modal = command_loop.presentation.tui = BarModal(["l", "l", "l", "l", "e", "tab", "escape", "h", "/", "z", "z", "escape", "escape", "h", "h", "escape"])
    await theme_command(command_loop, "")
    heights = [1 + sum(text.count("\n") for _, text in frame) for frame in modal.frames]
    assert heights == [24] * len(heights)
    for frame in modal.frames:
        text = "".join(text for _, text in frame)
        if "no matches" in text:
            assert text.splitlines()[1] == ""
            assert text.splitlines()[2].strip().endswith("Enter save · Esc cancel")


async def test_small_pane_prioritizes_choices_over_the_sample(command_loop, monkeypatch):
    from wizolt.ui.cli import appearance

    monkeypatch.setattr(appearance, "picker_height", lambda: 14)
    modal = command_loop.presentation.tui = BarModal(["escape"])
    await theme_command(command_loop, "")
    text = "".join(text for _, text in modal.frames[0])
    assert "showing 1-6 of" in text
    assert "def " in text and "Enter save" in text
    assert "Color samples · 1/8 shown" in text and "enlarge to see all" in text
    assert text.splitlines()[1].strip().endswith("Enter save · Esc cancel")
    assert "customization" in text.splitlines()[2] and "config" in text.splitlines()[2]
    assert 1 + text.count("\n") == 14


@pytest.mark.parametrize("height", [4, 8, 14, 20, 24, 40])
def test_appearance_tabs_fit_after_the_terminal_shrinks(command_loop, monkeypatch, height):
    from wizolt.ui.cli import appearance

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


@pytest.mark.parametrize("width", [52, 80, 140])
def test_roomy_appearance_previews_have_bounded_frames(command_loop, monkeypatch, width):
    from os import terminal_size

    from prompt_toolkit.utils import get_cwidth

    from wizolt.ui.cli import appearance

    monkeypatch.setattr(appearance, "picker_height", lambda: 32)
    monkeypatch.setattr(appearance.shutil, "get_terminal_size", lambda _fallback: terminal_size((width, 32)))
    picker = appearance.AppearancePicker(command_loop, 0)
    try:
        for index, kind in enumerate(appearance.TAB_KINDS):
            picker.tabs.tab = index
            rows = "".join(text for _, text in picker.fragments()).split("\n")
            assert len(rows) == 32, kind
            assert rows[1] == rows[4] == ""
            top = next(i for i, row in enumerate(rows) if row.startswith("  ╭"))
            bottom = next(i for i, row in enumerate(rows) if row.startswith("  ╰"))
            assert not rows[top - 1].strip()
            assert bottom > top + 3
            assert {get_cwidth(row) for row in rows[top : bottom + 1]} == {min(width - 4, 100) + 2}
            assert not rows[top + 1].strip(" │") and not rows[bottom - 1].strip(" │")
            if kind == "theme":
                assert "8/8 shown" in rows[top]
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


async def test_component_themes_persist_independently_and_survive_global_switch(command_loop):
    for args in ("statusbar lualine", "divider frame", "statusbar theme gruvbox-dark", "divider theme forest"):
        assert "saved" in await theme_command(command_loop, args)
    assert saved(command_loop)["ui"] == {
        "statusbar": {"format": "preset:lualine", "theme": "gruvbox-dark"},
        "divider": {"format": "preset:frame", "theme": "forest"},
    }
    await theme_command(command_loop, "plum")
    assert Theme.name() == "plum"
    assert Theme.bar_palette("statusbar") is Theme.BUILTIN["gruvbox-dark"]
    assert Theme.bar_palette("divider") is Theme.BUILTIN["forest"]
    command_loop.configure_theme()
    assert not command_loop.presentation.status_bar.layout.errors
    assert Theme.selected_bar_theme("divider") == "forest"
    assert "saved" in await theme_command(command_loop, "statusbar theme inherit")
    assert Theme.bar_palette("statusbar") is Theme.active()
    assert Theme.bar_palette("divider") is Theme.BUILTIN["forest"]


@pytest.mark.parametrize("kind", ["statusbar", "divider"])
async def test_unknown_component_theme_does_not_change_or_save_settings(command_loop, kind):
    assert f"unknown ui.{kind}.theme" in await theme_command(command_loop, f"{kind} theme missing")
    assert Theme.selected_bar_theme(kind) == "inherit"
    assert "ui" not in saved(command_loop)


@pytest.mark.parametrize("kind,keys", [("statusbar", ["l", "l", "tab"]), ("divider", ["h", "h", "tab", "tab"])])
async def test_picker_component_color_confirmation_and_cancel(command_loop, kind, keys):
    # inherit -> auto -> dark -> light -> slate; Space pins slate, forest stays a hover.
    command_loop.presentation.tui = BarModal([*keys, "j", "j", "j", "j", " ", "j", "enter"])
    assert f"{kind}.theme: slate" in await theme_command(command_loop, "")
    assert saved(command_loop)["ui"] == {kind: {"theme": "slate"}}
    command_loop.presentation.tui = BarModal([*keys, "j", " ", "escape"])
    assert await theme_command(command_loop, "") is None
    assert Theme.selected_bar_theme(kind) == "slate"
    assert saved(command_loop)["ui"] == {kind: {"theme": "slate"}}


def test_component_theme_completion():
    completer = CommandCompleter()
    for text, expected in (("/theme statusbar th", "theme "), ("/theme divider theme inh", "inherit"), ("/theme statusbar theme gru", "gruvbox-dark")):
        assert [item.text for item in completer.get_completions(Document(text), None)] == [expected]


@pytest.mark.parametrize("value", ["missing", 123, False])
def test_invalid_component_config_falls_back_without_rejecting_layout(command_loop, value):
    command_loop.session.config.ui = {"statusbar": {"format": "preset:minimal", "theme": value}, "divider": {"theme": "forest"}}
    command_loop.configure_theme()
    assert any("ui.statusbar.theme" in error for error in command_loop.theme_problems)
    assert Theme.selected_bar_theme("statusbar") == "inherit"
    assert Theme.selected_bar_theme("divider") == "forest"
    assert command_loop.presentation.status_bar.layout.sources["statusbar"] == "preset:minimal"


async def test_custom_component_highlights_and_sweep_use_the_selected_palette(command_loop):
    command_loop.session.config.ui = {
        "themes": {
            "mine": {
                "base": "forest",
                "colors": {"divider_rule": "#112233", "divider_glow": "#aabbcc"},
                "highlights": {"badge": {"fg": "#123456"}, "divider.label": {"fg": "#fedcba"}},
            }
        },
        "divider": {"theme": "mine", "format": "[badge]{activity}[/] [divider.label]{label}[/][divider_rule]{fill:─}[/]"},
        "statusbar": {"theme": "sand"},
    }
    command_loop.configure_theme()
    assert not command_loop.presentation.status_bar.layout.errors
    assert not any("unknown highlight" in problem for problem in command_loop.theme_problems)
    parts = command_loop.view.queue_divider_fragments()
    assert any("#123456" in style for style, _ in parts)
    assert any("#fedcba" in style for style, _ in parts)
    styles = command_loop.view.style()
    assert styles.get_attrs_for_style_str("class:queue.rule").color == "112233"
    assert styles.get_attrs_for_style_str("class:divider.glow0").color == "aabbcc"
    await theme_command(command_loop, "plum")
    assert command_loop.view.style().get_attrs_for_style_str("class:queue.rule").color == "112233"


async def test_component_picker_accepts_a_theme_named_custom(command_loop):
    command_loop.session.config.ui = {"themes": {"custom": {"base": "forest"}}}
    command_loop.configure_theme()
    command_loop.presentation.tui = BarModal(["l", "l", "tab", "G", " ", "enter"])
    assert "statusbar.theme: custom" in await theme_command(command_loop, "")
    assert saved(command_loop)["ui"]["statusbar"]["theme"] == "custom"


async def test_statusbar_powerline_and_warning_colors_are_scoped(command_loop):
    command_loop.session.config.ui = {
        "themes": {"mine": {"base": "sand", "highlights": {"status.model": {"fg": "#112233", "bg": "#aabbcc"}}}},
        "statusbar": {"format": "preset:lualine", "theme": "mine"},
        "divider": {"theme": "forest"},
    }
    command_loop.configure_theme()
    parts = command_loop.presentation.status_bar.fragments()
    assert any("fg:#112233 bg:#aabbcc" in style for style, _ in parts)
    colors = Theme.bar_styles({"status.warning", "status.error"})
    await theme_command(command_loop, "plum")
    assert Theme.bar_styles({"status.warning", "status.error"}) == colors
    assert command_loop.view.style().get_attrs_for_style_str("class:divider.working").color == Theme.BUILTIN["forest"].colors["divider_label"].lstrip("#")


async def test_leaving_color_preview_restores_custom_highlights_and_keeps_focus(command_loop):
    command_loop.session.config.ui = {
        "themes": {"mine": {"base": "forest", "highlights": {"badge": {"fg": "#123456"}}}},
        "statusbar": {"theme": "mine", "format": "[badge]{model}[/]"},
    }
    command_loop.configure_theme()
    picker = AppearancePicker(command_loop, 0)
    try:
        for key in ("l", "l", "tab", *(["k"] * (len(Theme.choices()) - 1))):
            picker.handle_key(key)
        assert Theme.selected_bar_theme("statusbar") == "auto"
        focused = picker.current_list().selected_choice()
        picker.handle_key("l")
        assert Theme.selected_bar_theme("statusbar") == "mine"
        assert not picker.layout.errors
        assert any("#123456" in style for style, _ in command_loop.presentation.status_bar.fragments())
        picker.handle_key("h")
        assert picker.current_list().selected_choice() == focused
    finally:
        picker.restore()


def frames_text(modal):
    return ["".join(fragment[1] for fragment in frame) for frame in modal.frames]


def configure_statusbar(command_loop, source):
    command_loop.session.config.ui = {"statusbar": {"format": source}}
    command_loop.configure_theme()


async def test_statusbar_placement_is_saved_as_the_format_alone(command_loop):
    modal = command_loop.presentation.tui = BarModal(["l", "l", "p", "enter"])
    assert "statusbar.format:" in await theme_command(command_loop, "")
    assert saved(command_loop)["ui"] == {"statusbar": {"format": status_template("default", True)}}
    assert any("Left / Right" in frame and "All left" in frame and "p placement" in frame for frame in frames_text(modal))

    # Read back from the saved format; moving the groups together restores the preset reference.
    configure_statusbar(command_loop, saved(command_loop)["ui"]["statusbar"]["format"])
    command_loop.presentation.tui = BarModal(["l", "l", "p", "enter"])
    await theme_command(command_loop, "")
    assert saved(command_loop)["ui"] == {"statusbar": {"format": "preset:default"}}


async def test_a_newly_chosen_preset_keeps_the_placement(command_loop):
    configure_statusbar(command_loop, status_template("powerline", True))
    # Recognized, so nothing is rewritten while it is left alone.
    command_loop.presentation.tui = BarModal(["l", "l", "enter"])
    assert await theme_command(command_loop, "") is None
    assert "ui" not in saved(command_loop)

    command_loop.presentation.tui = BarModal(["l", "l", "k", " ", "enter"])
    await theme_command(command_loop, "")
    assert saved(command_loop)["ui"] == {"statusbar": {"format": status_template("lualine", True)}}


async def test_a_custom_format_offers_no_placement_and_is_never_rewritten(command_loop):
    configure_statusbar(command_loop, "{model}{>}ctx {context.percent}%")
    modal = command_loop.presentation.tui = BarModal(["l", "l", "p", "enter"])
    assert await theme_command(command_loop, "") is None
    assert "ui" not in saved(command_loop)
    assert command_loop.presentation.status_bar.layout.sources["statusbar"] == "{model}{>}ctx {context.percent}%"
    assert any("custom format, f to edit" in frame and "Left / Right" not in frame for frame in frames_text(modal))


async def test_format_panel_copies_the_format_and_a_toml_snippet(command_loop, monkeypatch):
    copied = []
    monkeypatch.setattr(Clipboard, "copy", staticmethod(copied.append))
    modal = command_loop.presentation.tui = BarModal(["l", "l", "f", "c", "t", "escape", "escape"])
    assert await theme_command(command_loop, "") is None
    assert copied[0] == "preset:default"
    assert tomllib.loads(copied[1]) == {"ui": {"statusbar": {"format": "preset:default"}}}
    frames = frames_text(modal)
    # Which value, and what the preset stands for, are both on screen.
    assert any("ui.statusbar.format · selected, saved" in frame and "expands to" in frame for frame in frames)
    assert any("Copied a TOML snippet for ui.statusbar.format." in frame for frame in frames)


async def test_format_panel_reports_a_failed_copy_and_leaves_the_text_to_select(command_loop, monkeypatch):
    monkeypatch.setattr(Clipboard, "copy", staticmethod(lambda _text: "no clipboard command found (pbcopy)"))
    modal = command_loop.presentation.tui = BarModal(["l", "l", "f", "t", "escape", "escape"])
    await theme_command(command_loop, "")
    last = frames_text(modal)[-3]
    assert "Not copied: no clipboard command found (pbcopy). Select the text above" in last
    assert 'format = "preset:default"' in last and "Copied" not in last


async def test_an_edited_format_previews_and_saves_through_the_config(command_loop):
    edit = ["l", "l", "f", "e", "home", "X", " ", "c-s", "escape", "enter"]
    command_loop.presentation.tui = BarModal(edit)
    assert "statusbar.format:" in await theme_command(command_loop, "")
    assert saved(command_loop)["ui"] == {"statusbar": {"format": "X " + STATUS_PRESETS["default"]}}
    assert "".join(text for _, text in command_loop.presentation.status_bar.fragments()).startswith("X ")


async def test_an_unchanged_edit_keeps_the_preset_reference(command_loop):
    command_loop.presentation.tui = BarModal(["l", "l", "f", "e", "c-s", "escape", "enter"])
    assert await theme_command(command_loop, "") is None
    assert "ui" not in saved(command_loop)


async def test_an_invalid_draft_reports_where_and_never_reaches_the_config(command_loop):
    layout = command_loop.presentation.status_bar.layout
    keys = ["l", "l", "f", "e", "home", "]", "c-s", "escape", "escape", "escape"]
    modal = command_loop.presentation.tui = BarModal(keys)
    assert await theme_command(command_loop, "") is None
    frames = frames_text(modal)
    assert any("ui.statusbar.format: line 1, column 1: unmatched delimiter" in frame for frame in frames)
    assert any("Fix the errors above before applying." in frame for frame in frames)
    assert "Draft discarded." in frames[-3]
    assert command_loop.presentation.status_bar.layout is layout and layout.sources["statusbar"] == "preset:default"
    assert "ui" not in saved(command_loop)


async def test_format_editor_keeps_its_draft_keys_and_preview_through_resizes(command_loop, monkeypatch):
    picker = AppearancePicker(command_loop, 0)
    try:
        for key in ("l", "l", "l", "f", "e", "end", "Z"):  # the divider's format
            picker.handle_key(key, key if len(key) == 1 else "")
        assert picker.format is not None and picker.format.draft is not None
        draft = picker.format.draft.text
        for height in (30, 12, 6, 3, 2, 30):
            monkeypatch.setattr("wizolt.ui.cli.appearance.picker_height", lambda height=height: height)
            text = "".join(fragment[1] for fragment in picker.fragments())
            assert text.count("\n") + 1 <= height + 1
            assert "Ctrl-S apply · Esc discard" in text
            if height >= 12:
                assert "Running (preview)" in text and "Z" in text  # the real renderer, beside the draft
        assert picker.format.draft.text == draft and draft.endswith("Z")
        picker.handle_key("c-s")
        assert picker.source("divider") == DIVIDER_PRESETS["comet"] + "Z"
    finally:
        picker.restore()


async def test_sweep_rows_open_the_sweep_formula_in_the_format_panel(command_loop, monkeypatch):
    copied = []
    monkeypatch.setattr(Clipboard, "copy", staticmethod(copied.append))
    # The Divider tab, then its Sweep group; f opens the formula, not the divider's format.
    keys = ["h", "h", "tab", "f", "t", "e", "end", ")", "backspace", *" * 0.5", "c-s", "escape", "enter"]
    modal = command_loop.presentation.tui = BarModal(keys)
    assert "divider.sweep:" in await theme_command(command_loop, "")
    frames = frames_text(modal)
    assert any("ui.divider.sweep · selected, saved" in frame and "expands to" in frame for frame in frames)
    # An unbalanced formula is reported against the sweep setting and never applied.
    assert any("ui.divider.sweep: invalid expression" in frame for frame in frames)
    assert tomllib.loads(copied[0]) == {"ui": {"divider": {"sweep": "preset:comet"}}}
    assert saved(command_loop)["ui"] == {"divider": {"sweep": SWEEPS["comet"] + " * 0.5"}}


async def test_placement_applies_to_the_highlighted_preset_and_stays_when_choosing(command_loop):
    """Regression: with `blocks` chosen and `default` only highlighted, `p` changed nothing on
    screen; the preview of a highlighted preset was forced to All left."""
    configure_statusbar(command_loop, "preset:blocks")
    picker = AppearancePicker(command_loop, 0)
    try:
        for key in ("l", "l", "g"):  # the StatusBar tab, cursor on `default`
            picker.handle_key(key, key)
        assert picker.layout.sources["statusbar"] == "preset:default"
        picker.handle_key("p", "p")
        assert picker.layout.sources["statusbar"] == status_template("default", True)
        picker.handle_key("j", "j")  # browsing keeps the placement
        assert picker.layout.sources["statusbar"] == status_template("minimal", True)
        picker.handle_key(" ", " ")  # and so does choosing
        assert picker.source("statusbar") == status_template("minimal", True)
        picker.handle_key("p", "p")
        assert picker.layout.sources["statusbar"] == picker.source("statusbar") == "preset:minimal"
    finally:
        picker.restore()


@pytest.mark.parametrize("width", [60, 80, 120])
async def test_every_tab_legend_fits_and_keeps_save_and_cancel(command_loop, monkeypatch, width):
    """Regression: at 80 columns the StatusBar legend ran to 99 and the modal cut it before
    `Enter save · Esc cancel`; a narrow pane now drops the most familiar keys first."""
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback=None: os.terminal_size((width, 30)))
    monkeypatch.setattr("wizolt.ui.cli.appearance.picker_height", lambda: 24)
    picker = AppearancePicker(command_loop, 0)
    try:
        for tab in range(len(picker.tabs.titles)):
            legend = next(line for line in "".join(fragment[1] for fragment in picker.fragments()).splitlines() if "Esc cancel" in line)
            assert get_cwidth(legend) <= width and legend.rstrip().endswith("Enter save · Esc cancel"), (tab, legend)
            if width >= 120:
                assert "j/k move" in legend  # nothing is dropped where everything fits
            picker.handle_key("l", "l")
    finally:
        picker.restore()


def test_an_empty_paste_or_key_types_nothing_into_a_draft():
    """Regression: a bare bracketed paste arrives with empty data, and its key name was typed."""
    panel = FormatPanel("statusbar", lambda: "preset:default", "preset:default", apply=lambda _source: [], accept=lambda _source: None)
    panel.handle_key("e")
    assert panel.draft is not None
    before = panel.draft.text
    for key in ("paste", "any"):
        panel.handle_key(key, "")
        assert panel.draft.text == before, key
    panel.handle_key("paste", "{model}")
    assert panel.draft.text == before + "{model}"
