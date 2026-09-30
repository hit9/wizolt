"""Preset menus, cancellation, persistence at the command boundary."""

import asyncio
import tomllib
from pathlib import Path

import pytest
from test_command_ui import ModalHarness
from tui_harness import loop

from wizolt.ui.cli.bars import divider_command, statusbar_command
from wizolt.ui.render import Theme


class BarModal(ModalHarness):
    def invalidate(self):
        pass


@pytest.fixture
def command_loop(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(Theme, "_mode", "dark")
    monkeypatch.setattr(Theme, "_custom", {})
    command_loop = loop(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text('# keep comment\n[runtime]\ntheme = "auto"\n')
    command_loop.session.config.path = str(path)
    command_loop.interactive_input = True
    return command_loop


async def test_statusbar_hover_escape_restores_custom_template_without_saving(command_loop):
    layout = command_loop.presentation.status_bar.layout
    assert not layout.configure({"statusbar": "{model} custom"}, Theme.bar_styles)
    modal = command_loop.presentation.tui = BarModal(["up", "escape"])
    assert await statusbar_command(command_loop, "") is None
    assert layout.sources["statusbar"] == "{model} custom"
    assert any("" in fragment[1] for fragment in modal.frames[1])
    assert "ui" not in tomllib.loads(Path(command_loop.session.config.path).read_text())


async def test_statusbar_enter_saves_the_template(command_loop):
    command_loop.presentation.tui = BarModal(["j", "enter"])
    result = await statusbar_command(command_loop, "")
    assert "saved" in result
    saved = tomllib.loads(Path(command_loop.session.config.path).read_text())
    assert saved["ui"]["statusbar"]["format"] == "preset:minimal"


async def test_divider_layout_cancel_does_not_open_sweep(command_loop):
    modal = command_loop.presentation.tui = BarModal(["j", "escape"], consumed=True)
    before = dict(command_loop.presentation.status_bar.layout.sources)
    assert await divider_command(command_loop, "") is None
    assert command_loop.presentation.status_bar.layout.sources == before
    assert modal.pos == 2
    assert "ui" not in tomllib.loads(Path(command_loop.session.config.path).read_text())


async def test_divider_sweep_cancel_keeps_confirmed_layout(command_loop):
    modal = command_loop.presentation.tui = BarModal(["j", "enter", "j", "escape"], consumed=True)
    result = await divider_command(command_loop, "")
    assert "divider.format: preset:minimal" in result
    layout = command_loop.presentation.status_bar.layout
    assert layout.sources["divider"] == "preset:minimal"
    assert layout.sources["sweep"] == "preset:comet"
    frames = ["".join(fragment[1] for fragment in frame) for frame in modal.frames]
    assert "Divider › Layout" in frames[0]
    assert any("Divider › Sweep" in frame for frame in frames)
    assert any("Running (preview)" in frame and "Queued (preview)" in frame for frame in frames)
    assert modal.pos == 4
    saved = tomllib.loads(Path(command_loop.session.config.path).read_text())
    assert saved["ui"]["divider"] == {"format": "preset:minimal"}


async def test_divider_cascade_saves_layout_then_sweep(command_loop):
    command_loop.presentation.tui = BarModal(["enter", "j", "enter"], consumed=True)
    result = await divider_command(command_loop, "")
    assert "divider.format: preset:comet" in result
    assert "divider.sweep: preset:scan" in result
    saved = tomllib.loads(Path(command_loop.session.config.path).read_text())
    assert saved["ui"]["divider"] == {"format": "preset:comet", "sweep": "preset:scan"}


async def test_divider_cascade_can_keep_custom_layout_and_sweep(command_loop):
    layout = command_loop.presentation.status_bar.layout
    assert not layout.configure({"divider": "[divider_rule]{fill:─}[/]", "sweep": "0.5"}, Theme.bar_styles)
    command_loop.presentation.tui = BarModal(["enter", "enter"], consumed=True)
    result = await divider_command(command_loop, "")
    assert "saved" in result
    saved = tomllib.loads(Path(command_loop.session.config.path).read_text())
    assert saved["ui"]["divider"] == {"format": "[divider_rule]{fill:─}[/]", "sweep": "0.5"}


async def test_interrupted_preview_restores_layout_and_stops_animation(command_loop):
    class Interrupted(BarModal):
        async def show_modal(self, fragments_fn, key_fn, **kwargs):
            key_fn("j", "j")
            await asyncio.sleep(0)
            raise asyncio.CancelledError

    command_loop.presentation.tui = Interrupted([])
    from wizolt.ui.cli.bars import pick_layout

    before = dict(command_loop.presentation.status_bar.layout.sources)
    tasks = set(asyncio.all_tasks())
    with pytest.raises(asyncio.CancelledError):
        await pick_layout(command_loop, "divider")
    assert command_loop.presentation.status_bar.layout.sources == before
    assert set(asyncio.all_tasks()) == tasks


async def test_headless_lists_presets_and_direct_selection_handles_unknown_names(command_loop):
    command_loop.interactive_input = False
    assert "powerline" in await statusbar_command(command_loop, "")
    assert "breathe" in await divider_command(command_loop, "")
    before = dict(command_loop.presentation.status_bar.layout.sources)
    assert "unknown preset" in await statusbar_command(command_loop, "not-a-preset")
    assert command_loop.presentation.status_bar.layout.sources == before
    assert "saved" in await divider_command(command_loop, "minimal")


async def test_selection_recovers_from_a_malformed_ui_section(command_loop):
    command_loop.session.config.ui = {"statusbar": "broken"}
    Path(command_loop.session.config.path).write_text('[ui]\nstatusbar = "broken"\n')
    result = await statusbar_command(command_loop, "minimal")
    assert "Applied for this session; not saved" in result
    assert command_loop.session.config.ui["statusbar"] == {"format": "preset:minimal"}


async def test_cancel_restores_custom_template_even_after_its_highlight_disappears(command_loop, tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "custom.toml").write_text('base = "dark"\n[highlights.special]\nfg = "#abcdef"\n')
    assert not Theme.load_custom(str(themes))
    Theme.set_mode("custom")
    layout = command_loop.presentation.status_bar.layout
    assert not layout.configure({"statusbar": "[special]{model}[/]"}, Theme.bar_styles)
    # Switching away makes the custom group unavailable, but cancelling a picker must still
    # restore the exact previous configuration, not leave the last hovered preset active.
    Theme.set_mode("dark")
    command_loop.presentation.tui = BarModal(["up", "escape"])
    assert await statusbar_command(command_loop, "") is None
    assert command_loop.presentation.status_bar.layout is layout
    assert layout.sources["statusbar"] == "[special]{model}[/]"


def test_idle_preview_uses_idle_values_even_without_a_condition(command_loop):
    from wizolt.ui.cli.bars import preview

    layout = command_loop.presentation.status_bar.layout
    assert not layout.configure({"divider": "{label}|{rate}|{spinner}|{elapsed:duration}"}, Theme.bar_styles)
    rows = "".join(fragment[1] for fragment in preview(command_loop, "divider", 0)).splitlines()
    assert rows[rows.index("Idle (preview)") + 1] == "|||0s"
