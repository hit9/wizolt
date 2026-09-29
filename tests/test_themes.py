"""Theme switching: named themes, theme files, `/theme`, and redrawing what is already printed."""

import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.output import ColorDepth
from rich.console import Console
from test_command_ui import ModalHarness
from test_tui_resize import ReflowingTerminal
from tui_harness import loop, run_interactive_tui, wait_until

import wizolt.ui.render as render_module
from wizolt.config import ConfigFile
from wizolt.ui.cli import CommandCompleter
from wizolt.ui.cli.commands import theme_command
from wizolt.ui.render import HorizontalRule, Theme, UiPrinter
from wizolt.ui.themes import BUILTIN, HEX_ROLES, MENU_TEXT_CONTRAST, MUTED_CONTRAST, contrast
from wizolt.ui.tui.app import TuiApp

# 24-bit SGR parameters for colors the tests switch between.
GRUVBOX_RED = "38;2;251;73;52"  # gruvbox-dark `error`
GRUVBOX_GRAY = "38;2;146;131;116"  # gruvbox-dark `rule`
NORD_RED = "38;2;191;97;106"  # nord `error`

CONFIG = """# my own notes
[runtime]
# the look
theme = "auto"
shell_timeout = 30
"""


@pytest.fixture(autouse=True)
def default_theme(monkeypatch):
    """Every test starts on the default dark theme with no theme files, and leaves it that way."""
    monkeypatch.setattr(Theme, "_mode", "dark")
    monkeypatch.setattr(Theme, "_custom", {})
    monkeypatch.delenv("COLORFGBG", raising=False)


@pytest.fixture
def truecolor(monkeypatch):
    """Record transcript rows in 24-bit color, so a row's colors can be read back exactly."""
    output = SimpleNamespace(get_default_color_depth=lambda: ColorDepth.DEPTH_24_BIT)
    monkeypatch.setattr(render_module, "get_app_session", lambda: SimpleNamespace(output=output))


def write_theme(directory, name, text):
    os.makedirs(directory, exist_ok=True)
    Path(directory, name + ".toml").write_text(text)


def themed_loop(tmp_path, theme="auto"):
    command_loop = loop(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.replace('"auto"', f'"{theme}"'))
    command_loop.session.config.path = str(config)
    command_loop.session.settings.theme = theme
    return command_loop


class ThemeModal(ModalHarness):
    """The picker's host: records which theme each repaint was asked for and each recolor."""

    def __init__(self, keys):
        super().__init__(keys)
        self.painted = []
        self.recolored = 0

    def invalidate(self):
        self.painted.append(Theme.name())

    def recolor(self):
        self.recolored += 1


def test_every_builtin_theme_defines_every_role_in_a_shape_the_adapters_accept():
    for name, palette in Theme.BUILTIN.items():
        assert palette.colors.keys() == Theme.DARK.keys(), name
        assert palette.appearance in ("dark", "light"), name
        for role in Theme.ROLES:
            value = palette.colors[role]
            terminal = value == "default" or value in Theme.RICH_COLOR_NAMES
            assert terminal or re.fullmatch(r"#[0-9a-f]{6}", value), (name, role, value)
            if role in HEX_ROLES:
                assert value.startswith("#"), (name, role)
        Theme.set_mode(name)
        assert Theme.pygments_style() is not None, name
        Console(theme=Theme.rich_theme()).get_style("wizolt.user")  # raises for a color Rich cannot read


def test_named_themes_keep_grey_text_readable():
    """A scheme's comment grey is dim by design; as hint and menu text it must still be readable.
    The menu floor gives way only where the scheme's own foreground cannot reach it."""
    for name, palette in BUILTIN.items():
        colors = palette.colors
        assert contrast(colors["muted"], palette.background) >= MUTED_CONTRAST, name
        menu = contrast(colors["menu_muted"], colors["menu_bg"])
        assert menu >= MENU_TEXT_CONTRAST or colors["menu_muted"] == colors["status_base"], (name, menu)


def test_menu_descriptions_take_the_menu_text_color(tmp_path):
    view = loop(tmp_path).view
    Theme.set_mode("one-dark")
    style = view.style()

    for name in ("completion-menu.meta.completion", "completion-menu.hint"):
        assert "#" + style.get_attrs_for_style_str("class:" + name).color == Theme.color("menu_muted"), name


def test_themes_resolve_by_name_and_fall_back_to_the_terminal_default(monkeypatch):
    assert Theme.resolve("gruvbox-dark") == "gruvbox-dark"
    assert Theme.resolve(" Nord ") == "nord"
    assert Theme.resolve("auto") == "dark"
    assert Theme.resolve("gruvbox") == "dark"
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert Theme.resolve("gruvbox") == "light"


def test_configure_reports_an_unknown_theme_and_draws_the_default(tmp_path):
    problems = Theme.configure("gruvbox", str(tmp_path))

    assert Theme.name() == "dark"
    assert len(problems) == 1 and "unknown theme `gruvbox`" in problems[0] and "gruvbox-dark" in problems[0]
    assert Theme.configure("nord", str(tmp_path)) == []
    assert Theme.name() == "nord"


def test_a_theme_file_overrides_roles_on_its_base(tmp_path):
    write_theme(tmp_path, "mine", 'base = "nord"\npygments = "dracula"\n[colors]\naccent = "#AbC"\nerror = "ansired"\n')

    assert Theme.load_custom(str(tmp_path)) == []
    Theme.set_mode("mine")

    assert Theme.color("accent") == "#aabbcc"
    assert Theme.color("error") == "ansired"
    assert Theme.color("tool") == Theme.BUILTIN["nord"].colors["tool"]
    assert Theme.appearance() == "dark"
    assert Theme.palette()["pygments"] == "dracula"


def test_theme_file_mistakes_are_reported_and_the_rest_of_the_file_applies(tmp_path):
    write_theme(
        tmp_path,
        "rough",
        'base = "nord"\npygments = "no-such-style"\nextra = 1\n[colors]\naccent = "teal-ish"\ndivider_glow = "ansicyan"\nnope = "#fff"\nsuccess = "#00ff00"\n',
    )
    write_theme(tmp_path, "broken", "base = ")
    write_theme(tmp_path, "gruvbox-dark", 'base = "dark"\n')
    write_theme(tmp_path, "orphan", 'base = "no-such-base"\n')

    problems = Theme.load_custom(str(tmp_path))

    assert set(Theme.themes()) - set(Theme.BUILTIN) == {"rough"}
    for expected in (
        "broken.toml",
        "`gruvbox-dark` is a built-in",
        "base must be one of",
        "no-such-style",
        "extra",
        "teal-ish",
        "divider_glow must be a #rrggbb",
        "unknown role `nope`",
    ):
        assert any(expected in problem for problem in problems), expected
    Theme.set_mode("rough")
    nord = Theme.BUILTIN["nord"].colors
    assert Theme.color("success") == "#00ff00"
    assert (Theme.color("accent"), Theme.color("divider_glow"), Theme.palette()["pygments"]) == (nord["accent"], nord["divider_glow"], "nord")


def test_recorded_rows_replay_as_captured_until_the_theme_changes_then_redraw_in_it(truecolor):
    recorded = []
    printer = UiPrinter(output_fn=lambda text: None)
    printer.transcript_sink = recorded.append
    printer.print_parts([FormattedText([(Theme.fg("error"), "boom\n")])])
    printer.print_parts([HorizontalRule(Theme.fg("rule"))])
    line, rule = recorded

    captured = line(80)
    assert captured == str(line) and "boom" in captured and GRUVBOX_RED not in captured
    Theme.set_mode("gruvbox-dark")

    assert GRUVBOX_RED in line(80) and "boom" in line(80)
    assert str(line) == captured, "the capture itself is what text readers of the transcript see"
    assert GRUVBOX_GRAY in rule(80)


def test_transcript_rows_share_one_style_per_theme(monkeypatch):
    """A style merged per row starts with an empty lookup cache: it made every emitted line ten
    times as expensive to render. One style per theme keeps a row's cost what it was."""
    merges = []
    real_merge = render_module.merge_styles
    monkeypatch.setattr(render_module, "merge_styles", lambda styles: merges.append(len(styles)) or real_merge(styles))

    def render_rows(count):
        for _ in range(count):
            UiPrinter.render_to_ansi([FormattedText([(Theme.fg("error"), "boom\n")])], 80, color_depth=ColorDepth.DEPTH_8_BIT)

    Theme.set_mode("nord")
    render_rows(3)
    assert len(merges) == 1
    Theme.set_mode("dracula")
    render_rows(3)
    assert len(merges) == 2


def recorded_row(style, text="boom"):
    recorded = []
    printer = UiPrinter(output_fn=lambda text: None)
    printer.transcript_sink = recorded.append
    printer.print_parts([FormattedText([(style, text + "\n")])])
    return recorded[0]


def sgr_parameters(text):
    return [params for params in re.findall(r"\x1b\[([0-9;]*)m", text) if params]


def test_no_color_turns_off_the_transcript_colors_but_keeps_emphasis(monkeypatch):
    """`NO_COLOR` already took the live app to monochrome; recorded rows kept their colors."""
    output = SimpleNamespace(get_default_color_depth=lambda: ColorDepth.DEPTH_8_BIT)
    monkeypatch.setattr(render_module, "get_app_session", lambda: SimpleNamespace(output=output))
    monkeypatch.setenv("NO_COLOR", "1")
    row = recorded_row(Theme.fg("error", "bold"))(80)

    assert "boom" in row
    assert {code for params in sgr_parameters(row) for code in params.split(";")} == {"0", "1"}  # reset and bold only


def test_without_color_the_selection_band_is_reverse(monkeypatch, tmp_path):
    """A background draws nothing on a monochrome terminal, so the cursor row would vanish."""
    monkeypatch.setenv("NO_COLOR", "1")
    band = loop(tmp_path).view.style().get_attrs_for_style_str("class:choice.selected")

    assert band.reverse and band.bgcolor == ""


def test_named_themes_draw_exact_colors_on_a_true_color_terminal(monkeypatch):
    """prompt-toolkit never reads COLORTERM, so every hex was rounded to 256 colors. The defaults
    keep that rounding, which is how their colors were tuned; named schemes draw exactly."""
    output = SimpleNamespace(get_default_color_depth=lambda: ColorDepth.DEPTH_8_BIT)
    monkeypatch.setattr(render_module, "get_app_session", lambda: SimpleNamespace(output=output))
    monkeypatch.setenv("COLORTERM", "truecolor")
    row = recorded_row(Theme.fg("user"))

    assert "38;2;" not in row(80) and "boom" in row(80)
    Theme.set_mode("gruvbox-dark")
    assert "38;2;254;128;25" in row(80)  # gruvbox orange, not its nearest 256-color neighbour
    monkeypatch.setenv("COLORTERM", "")
    Theme.set_mode("gruvbox-dark")
    assert "38;2;" not in row(80)


def test_the_app_draws_at_the_depth_the_theme_asks_for(monkeypatch):
    monkeypatch.setenv("COLORTERM", "24bit")
    app = TuiApp()
    depths = []

    def drive(_pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        depths.append(app.app.color_depth)
        Theme.set_mode("nord")
        depths.append(app.app.color_depth)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)

    assert depths == [ColorDepth.DEPTH_1_BIT, ColorDepth.DEPTH_24_BIT]  # DummyOutput selects 1 bit


class RecordingTerminal(ReflowingTerminal):
    def __init__(self, rows, columns):
        super().__init__(rows, columns)
        self.raw = []

    def write_raw(self, data):
        self.raw.append(data)
        super().write_raw(data)


def test_recolor_redraws_the_transcript_in_the_new_theme_without_a_width_change(monkeypatch, truecolor):
    """Two switches, so the second has to redraw rather than replay the rows the first cached."""
    output = RecordingTerminal(30, 80)
    app = TuiApp()
    printer = UiPrinter()
    printer.color = True
    printer.transcript_sink = app.record_scrollback

    def switch(name, color):
        del output.raw[:]
        app.app.loop.call_soon_threadsafe(lambda: (Theme.set_mode(name), app.recolor()))
        wait_until(lambda: color in "".join(output.raw))
        redrawn = "".join(output.raw)
        assert "\x1b[3J" in redrawn and "boom" in redrawn

    def drive(_pipe_input):
        wait_until(lambda: any(line.startswith(UiPrinter.PROMPT_PREFIX) for line in output.lines))
        app.app.loop.call_soon_threadsafe(printer.print_parts, [FormattedText([(Theme.fg("error"), "boom\n")])])
        wait_until(lambda: "boom" in "".join(app.scrollback.transcript))
        switch("nord", NORD_RED)
        switch("gruvbox-dark", GRUVBOX_RED)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(
        monkeypatch,
        app,
        drive=drive,
        output=output,
        on_application=lambda application: setattr(output, "report_cursor_row", application.renderer.report_absolute_cursor_row),
    )


async def test_theme_by_name_switches_redraws_and_saves_keeping_the_config_comments(tmp_path):
    command_loop = themed_loop(tmp_path)
    tui = command_loop.presentation.tui = ThemeModal([])

    result = await theme_command(command_loop, "Gruvbox-Dark")

    assert Theme.name() == "gruvbox-dark" and command_loop.session.settings.theme == "gruvbox-dark"
    assert tui.recolored == 1
    assert "saved as runtime.theme" in result
    assert (tmp_path / "config.toml").read_text() == CONFIG.replace('"auto"', '"gruvbox-dark"')


async def test_an_unknown_theme_name_changes_nothing(tmp_path):
    command_loop = themed_loop(tmp_path)
    tui = command_loop.presentation.tui = ThemeModal([])

    result = await theme_command(command_loop, "gruvbox")

    assert result.startswith("Unknown theme: gruvbox.") and "gruvbox-dark" in result
    assert Theme.name() == "dark" and tui.recolored == 0
    assert (tmp_path / "config.toml").read_text() == CONFIG


async def test_without_a_picker_theme_lists_the_themes_and_marks_the_configured_one(tmp_path):
    command_loop = themed_loop(tmp_path, "nord")
    command_loop.interactive_input = False

    listing = (await theme_command(command_loop, "")).splitlines()

    assert listing[0] == "  auto"
    assert "* nord" in listing and "  gruvbox-dark" in listing


async def test_the_picker_applies_each_row_it_lands_on_and_escape_restores_the_theme(tmp_path):
    command_loop = themed_loop(tmp_path, "nord")
    Theme.set_mode("nord")
    command_loop.interactive_input = True
    tui = command_loop.presentation.tui = ThemeModal(["j", "j", "escape"])

    assert await theme_command(command_loop, "") is None

    assert tui.painted == ["dracula", "one-dark", "nord"]
    assert Theme.name() == "nord" and tui.recolored == 0
    assert (tmp_path / "config.toml").read_text() == CONFIG.replace('"auto"', '"nord"')
    # The preview is drawn in the row's theme: the user row in dracula's orange on its frame.
    assert any((Theme.BUILTIN["dracula"].colors["user"] in style and "tighten" in text) for style, text in tui.frames[1])


async def test_the_picker_restores_the_theme_when_interrupted(tmp_path):
    command_loop = themed_loop(tmp_path, "nord")
    Theme.set_mode("nord")
    command_loop.interactive_input = True
    command_loop.presentation.tui = ThemeModal(["j", "c-c"])

    with pytest.raises(KeyboardInterrupt):
        await theme_command(command_loop, "")

    assert Theme.name() == "nord"


async def test_enter_keeps_the_previewed_theme_and_saves_it(tmp_path):
    command_loop = themed_loop(tmp_path, "nord")
    Theme.set_mode("nord")
    command_loop.interactive_input = True
    tui = command_loop.presentation.tui = ThemeModal(["j", "enter"])

    result = await theme_command(command_loop, "")

    assert Theme.name() == "dracula" and tui.recolored == 1
    assert result.startswith("Theme: dracula (saved")
    assert 'theme = "dracula"' in (tmp_path / "config.toml").read_text()


async def test_picking_auto_saves_auto_and_draws_the_terminal_default(tmp_path, monkeypatch):
    monkeypatch.setenv("COLORFGBG", "0;15")
    command_loop = themed_loop(tmp_path, "nord")
    Theme.set_mode("nord")

    await theme_command(command_loop, "auto")

    assert Theme.name() == "light"
    assert 'theme = "auto"' in (tmp_path / "config.toml").read_text()


def test_saving_adds_a_runtime_table_and_writes_through_a_symlinked_config(tmp_path):
    target = tmp_path / "dotfiles" / "config.toml"
    target.parent.mkdir()
    target.write_text('# providers only\n[provider]\nmodel = "m"\n')
    link = tmp_path / "config.toml"
    link.symlink_to(target)

    ConfigFile.set_runtime(str(link), "theme", "nord")

    assert link.is_symlink()
    assert target.read_text() == '# providers only\n[provider]\nmodel = "m"\n\n[runtime]\ntheme = "nord"\n'


async def test_start_session_draws_the_configured_theme_file_and_reports_its_problems(tmp_path, monkeypatch):
    command_loop = themed_loop(tmp_path, "mine")
    write_theme(command_loop.session.data_path("themes"), "mine", 'base = "nord"\n[colors]\nnope = "#fff"\n')
    emitted = []
    monkeypatch.setattr(command_loop.presentation, "emit", lambda text="", indent=0: emitted.append(text))

    command_loop.start_session(show_banner=False)
    await command_loop.background.close_background()

    assert Theme.name() == "mine"
    assert any("unknown role `nope`" in line for line in emitted)


def test_theme_names_complete_after_the_command():
    completions = [completion.text for completion in CommandCompleter().get_completions(Document("/theme gru"), None)]

    assert completions == ["gruvbox-dark", "gruvbox-light"]


def test_the_app_style_follows_a_theme_switch(tmp_path):
    view = loop(tmp_path).view
    assert view.style().get_attrs_for_style_str("class:choice.selected").bgcolor == "008ec4"

    Theme.set_mode("gruvbox-dark")

    assert view.style().get_attrs_for_style_str("class:choice.selected").bgcolor == "83a598"
