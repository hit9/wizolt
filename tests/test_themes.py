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
from wizolt.ui.cli.commands import theme_command, theme_preview
from wizolt.ui.render import HorizontalRule, Theme, UiPrinter
from wizolt.ui.themes import BUILTIN, DIFF_STYLES, HEX_ROLES, MENU_TEXT_CONTRAST, MUTED_CONTRAST, contrast
from wizolt.ui.tui.app import TuiApp
from wizolt.utils import terminal

# 24-bit SGR parameters for colors the tests switch between.
GRUVBOX_RED = "38;2;249;88;65"  # gruvbox-dark `error`, lifted for text contrast
GRUVBOX_GRAY = "38;2;146;131;116"  # gruvbox-dark `rule`
NORD_RED = "38;2;201;147;157"  # nord `error`, lifted for text contrast

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
    monkeypatch.setattr(Theme, "_diff_style", "auto")
    monkeypatch.setattr(Theme, "_custom", {})
    monkeypatch.delenv("COLORFGBG", raising=False)
    # The color environment decides the depth; each test sets the part it is about.
    for name in ("COLORTERM", "PROMPT_TOOLKIT_COLOR_DEPTH", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(terminal, "background", lambda: None)  # never ask the machine's terminal


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

    def __init__(self, keys, *, consumed=False):
        super().__init__(keys, consumed=consumed)
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
    """Readable hints and menu text, including Solarized's low-contrast foreground."""
    for name, palette in BUILTIN.items():
        colors = palette.colors
        assert contrast(colors["muted"], palette.background) >= MUTED_CONTRAST, name
        menu = contrast(colors["menu_muted"], colors["menu_bg"])
        assert menu >= MENU_TEXT_CONTRAST, (name, menu)
        assert contrast(colors["muted"], palette.background) >= contrast(colors["subtle"], palette.background), name


def test_builtin_selections_and_named_ui_labels_have_readable_contrast():
    for name, palette in Theme.BUILTIN.items():
        colors = palette.colors
        assert contrast(colors["selection_fg"], colors["selection_bg"]) >= 4.5, name
        if not palette.background:  # ANSI roles follow the terminal's user-defined palette.
            continue
        for role in ("user", "tool", "success", "warning", "error", "accent", "info", "status_base"):
            assert contrast(colors[role], palette.background) >= 4.5, (name, role)


def test_named_themes_lay_the_footer_on_their_panel_and_keep_the_divider_one_hue():
    for name, palette in BUILTIN.items():
        colors = palette.colors
        assert colors["status_bg"] == colors["menu_bg"], name
        assert colors["divider_label"] == colors["accent"], name
        # Footer text is readable on the panel it lies on, not only on the background.
        for role in ("status_base", "status_provider", "status_reason", "status_mcp", "status_context", "status_yolo", "status_worker"):
            assert contrast(colors[role], colors["status_bg"]) >= MUTED_CONTRAST, (name, role)
    # The terminal-following themes keep a transparent footer and the label they always had.
    for name in ("dark", "light"):
        assert Theme.BUILTIN[name].colors["status_bg"] == "default"
        assert Theme.BUILTIN[name].colors["divider_label"] == Theme.BUILTIN[name].colors["accent_secondary"]


def test_powerline_joins_fade_into_the_status_band(tmp_path):
    from tui_harness import loop

    Theme.set_mode("nord")
    bar = loop(tmp_path).presentation.status_bar
    assert not bar.layout.configure({"statusbar": "preset:powerline"}, Theme.bar_styles)
    band = Theme.color("status_bg")
    joins = [style for style, text in bar.fragments() if text in ("", "")]
    # The joins at the ends of the segments are drawn in the band, not the terminal's background.
    assert any(style == f"fg:{band}" for style in joins)
    assert all("bg:default" not in style for style, _ in bar.fragments())


@pytest.mark.parametrize(("name", "appearance"), [("monokai", "dark"), ("github-dark", "dark"), ("gruvbox-light", "light")])
async def test_new_themes_switch_save_and_preview(tmp_path, name, appearance):
    command_loop = themed_loop(tmp_path)
    await theme_command(command_loop, name)

    assert Theme.name() == name
    assert Theme.palette()["pygments"] == name
    assert f'theme = "{name}"' in (tmp_path / "config.toml").read_text()
    preview = theme_preview(name)
    text = "".join(text for _, text in preview)
    assert f"{appearance} terminal background" in text
    assert "Choose a color theme" in text and "ctx 25%" in text
    assert any(Theme.color("selection_bg") in style and "/theme" in text for style, text in preview)
    assert any(Theme.color("menu_muted") in style and "Choose" in text for style, text in preview)


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
    assert Theme.resolve("gruvbx") == "dark"
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert Theme.resolve("gruvbx") == "light"


def test_a_light_and_dark_pair_follows_the_terminal(monkeypatch, tmp_path):
    assert Theme.resolve("Gruvbox") == "gruvbox-dark"
    monkeypatch.setattr(terminal, "background", lambda: (253, 246, 227))
    assert Theme.resolve("gruvbox") == "gruvbox-light"
    assert Theme.resolve("solarized") == "solarized-light"
    assert "nord" not in Theme.pairs()  # no light variant to follow into
    write_theme(tmp_path, "mine-dark", 'base = "nord"\n')
    write_theme(tmp_path, "mine-light", 'base = "gruvbox-light"\n')
    Theme.load_custom(str(tmp_path))
    assert Theme.resolve("mine") == "mine-light"
    assert Theme.choices()[:6] == ("auto", "dark", "light", "gruvbox", "gruvbox-dark", "gruvbox-light")


def test_configure_reports_an_unknown_theme_and_draws_the_default(tmp_path):
    problems = Theme.configure("gruvbx", str(tmp_path))

    assert Theme.name() == "dark"
    assert len(problems) == 1 and "unknown theme `gruvbx`" in problems[0] and "gruvbox-dark" in problems[0]
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


def test_a_theme_file_can_recolor_the_diff_bands(tmp_path):
    """Red against green is the pair colorblind readers lose; a theme file can pick another."""
    write_theme(tmp_path, "cb", 'base = "dark"\n[diff]\nadded = "#003A66"\nremoved_word = "ansiyellow"\nremoved = "default"\nmoved = "#fff"\n')

    problems = Theme.load_custom(str(tmp_path))
    Theme.set_mode("cb")

    assert Theme.diff_style("diff.added.bg") == "bg:#003a66"
    assert Theme.diff_style("diff.removed.emph") == "bg:ansiyellow"
    assert Theme.diff_style("diff.removed.bg") == Theme.DIFF_DARK["diff.removed.bg"]
    assert Theme.diff_style("diff.added.emph") == Theme.DIFF_DARK["diff.added.emph"]
    assert any("diff removed must be" in problem for problem in problems)
    assert any("unknown [diff] key `moved`" in problem for problem in problems)
    Theme.set_mode("dark")
    assert Theme.diff_style("diff.added.bg") == Theme.DIFF_DARK["diff.added.bg"]


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


def test_a_theme_file_draws_at_its_bases_depth(monkeypatch, tmp_path):
    monkeypatch.setenv("COLORTERM", "truecolor")
    write_theme(tmp_path, "warm", 'base = "gruvbox-dark"\n[colors]\naccent = "#abcdef"\n')
    write_theme(tmp_path, "plain", 'base = "dark"\n[colors]\naccent = "#abcdef"\n')
    Theme.load_custom(str(tmp_path))

    Theme.set_mode("warm")
    assert Theme.color_depth(ColorDepth.DEPTH_8_BIT) is ColorDepth.DEPTH_24_BIT
    Theme.set_mode("plain")
    assert Theme.color_depth(ColorDepth.DEPTH_8_BIT) is ColorDepth.DEPTH_8_BIT


def test_a_diff_style_other_than_classic_draws_exact_colors_under_the_default_theme(monkeypatch):
    """Rounded to 256 colors, delta's dark added band became black, even on a true-color terminal
    running the terminal-following `dark` theme."""
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert Theme.color_depth(ColorDepth.DEPTH_8_BIT) is ColorDepth.DEPTH_8_BIT
    Theme.set_diff_style("delta")
    assert Theme.color_depth(ColorDepth.DEPTH_8_BIT) is ColorDepth.DEPTH_24_BIT


@pytest.mark.parametrize(
    ("theme", "diff_style", "env", "warned"),
    [
        ("nord", "auto", {}, "theme nord"),
        ("dark", "delta", {}, "diff style delta"),
        ("nord", "auto", {"COLORTERM": "truecolor"}, ""),
        ("nord", "auto", {"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_8_BIT"}, ""),
        # `classic` survives rounding, so the terminal-following themes never need true color.
        ("dark", "auto", {}, ""),
    ],
)
def test_exact_colors_on_a_terminal_without_true_color_are_warned_about(monkeypatch, theme, diff_style, env, warned):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    Theme.set_mode(theme)
    Theme.set_diff_style(diff_style)
    warning = Theme.true_color_warning()
    if warned:
        assert warning and f"The {warned} is drawn in exact colors" in warning and "export COLORTERM=truecolor" in warning
    else:
        assert warning is None


async def test_the_true_color_warning_shows_at_startup_and_after_theme(tmp_path):
    command_loop = themed_loop(tmp_path, "nord")
    command_loop.configure_theme()
    assert any("COLORTERM" in problem for problem in command_loop.theme_problems)

    command_loop.presentation.tui = ThemeModal([])
    result = await theme_command(command_loop, "dracula")
    assert "The theme dracula is drawn in exact colors" in result


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


def test_transcript_rows_resolve_each_style_string_once(monkeypatch):
    """A `class:` name makes prompt-toolkit scan every rule, and a row renders outside any live
    render's cache: resolving the same `Theme.fg` strings afresh on every line doubled the cost
    of emitting one. The benchmark's emit_500_plain_rows went from 12.4 ms to 25.2 ms."""
    lookups = []
    real_merge = render_module.merge_styles

    def counting_merge(styles):
        merged = real_merge(styles)
        resolve = merged.get_attrs_for_style_str
        merged.get_attrs_for_style_str = lambda style, *default: lookups.append(style) or resolve(style, *default)
        return merged

    monkeypatch.setattr(render_module, "merge_styles", counting_merge)
    Theme.set_mode("nord")
    for _ in range(3):
        UiPrinter.render_to_ansi([FormattedText([(Theme.fg("tool"), "Read "), (Theme.fg("muted"), "a.py\n")])], 80, color_depth=ColorDepth.DEPTH_8_BIT)

    assert sorted(set(lookups)) == sorted(lookups), "a style string was resolved more than once"


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


async def test_a_pair_is_saved_as_the_pair_so_the_next_terminal_can_differ(tmp_path):
    command_loop = themed_loop(tmp_path)
    command_loop.presentation.tui = ThemeModal([])

    await theme_command(command_loop, "gruvbox")

    assert Theme.name() == "gruvbox-dark"
    assert 'theme = "gruvbox"' in (tmp_path / "config.toml").read_text()


async def test_an_unknown_theme_name_changes_nothing(tmp_path):
    command_loop = themed_loop(tmp_path)
    tui = command_loop.presentation.tui = ThemeModal([])

    result = await theme_command(command_loop, "gruvbx")

    assert result.startswith("Unknown theme: gruvbx.") and "gruvbox-dark" in result
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
    tui = command_loop.presentation.tui = ThemeModal(["j", "enter"], consumed=True)

    result = await theme_command(command_loop, "")

    assert Theme.name() == "dracula" and tui.recolored == 1 and Theme.selected_diff_style() == "auto"
    assert result.startswith("Theme: dracula (saved")
    assert 'theme = "dracula"' in (tmp_path / "config.toml").read_text()


async def test_the_theme_menu_goes_on_to_the_diff_colors_and_saves_both(tmp_path):
    command_loop = themed_loop(tmp_path, "nord")
    Theme.set_mode("nord")
    command_loop.interactive_input = True
    # Theme: nord -> dracula. Diff colors: auto -> classic.
    tui = command_loop.presentation.tui = ThemeModal(["j", "enter", "j", "enter"], consumed=True)

    result = await theme_command(command_loop, "")

    assert Theme.name() == "dracula" and Theme.diff_style_name() == "classic"
    assert "Theme: dracula (saved" in result and "Diff colors: classic (saved as ui.diff.style" in result
    config = (tmp_path / "config.toml").read_text()
    assert 'theme = "dracula"' in config and '[ui.diff]\nstyle = "classic"' in config
    assert command_loop.session.config.ui["diff"] == {"style": "classic"}
    # One redraw for both choices, after the second.
    assert tui.recolored == 1
    # The preview is drawn in the focused style: classic's changed-word green, not dracula's delta.
    assert any(style.endswith("bg:#1c7a1c") for style, _ in tui.frames[-1])


async def test_escape_on_the_diff_colors_keeps_the_theme_just_picked(tmp_path):
    command_loop = themed_loop(tmp_path, "nord")
    Theme.set_mode("nord")
    command_loop.interactive_input = True
    tui = command_loop.presentation.tui = ThemeModal(["j", "enter", "j", "escape"], consumed=True)

    result = await theme_command(command_loop, "")

    assert Theme.name() == "dracula" and Theme.selected_diff_style() == "auto"
    assert result.startswith("Theme: dracula") and "Diff colors" not in result
    assert "[ui.diff]" not in (tmp_path / "config.toml").read_text()
    assert tui.recolored == 1


async def test_theme_by_name_leaves_the_diff_colors_alone(tmp_path):
    command_loop = themed_loop(tmp_path)
    command_loop.presentation.tui = ThemeModal([])
    Theme.set_diff_style("classic")

    result = await theme_command(command_loop, "nord")

    assert "Diff colors" not in result and Theme.diff_style_name() == "classic"


def test_auto_diff_colors_follow_each_themes_pairing():
    # The terminal-following themes keep the colors they always had.
    assert {name: palette.diff_style for name, palette in Theme.BUILTIN.items()} == {
        "dark": "classic",
        "light": "classic",
        "gruvbox-dark": "calochortus-lyallii",
        "gruvbox-light": "zebra",
        "solarized-dark": "calochortus-lyallii",
        "solarized-light": "zebra",
        "nord": "calochortus-lyallii",
        "dracula": "delta",
        "one-dark": "colibri",
        "monokai": "mantis-shrimp",
        "github-dark": "zebra",
    }
    for name, palette in Theme.BUILTIN.items():
        Theme.set_mode(name)
        # Every pairing was made for its theme's appearance, and draws all four bands.
        assert Theme.diff_style_name() == palette.diff_style, name
        assert set(Theme.diff_bands(palette.diff_style) or {}) == {"diff.added.bg", "diff.added.emph", "diff.removed.bg", "diff.removed.emph"}, name
    Theme.set_mode("dracula")
    assert Theme.diff_style("diff.added.bg") == "bg:#002800"  # delta's, as published
    Theme.set_diff_style("classic")
    assert Theme.diff_style("diff.added.bg") == Theme.DIFF_DARK["diff.added.bg"]


def test_the_menu_offers_the_styles_made_for_the_background_and_others_fall_back():
    Theme.set_mode("nord")
    assert Theme.diff_styles() == ("auto", *DIFF_STYLES)
    Theme.set_mode("gruvbox-light")
    assert Theme.diff_styles() == ("auto", "classic", "delta", "zebra")
    # A style made only for dark backgrounds draws the light theme's own pairing instead.
    Theme.set_diff_style("gruvmax-fang")
    assert Theme.diff_style_name() == "zebra" and Theme.diff_style("diff.added.bg") == "bg:#d6ffd6"
    Theme.set_mode("gruvbox-dark")
    assert Theme.diff_style_name() == "gruvmax-fang" and Theme.diff_style("diff.removed.emph") == "bg:#80002a"


def test_a_theme_file_inherits_its_bases_diff_pairing_and_its_bands_win(tmp_path):
    write_theme(tmp_path, "mine", 'base = "github-dark"\n[diff]\nremoved = "#112233"\n')
    assert not Theme.load_custom(str(tmp_path))
    Theme.set_mode("mine")
    assert Theme.diff_style_name() == "zebra"
    Theme.set_diff_style("classic")
    assert Theme.diff_style("diff.removed.bg") == "bg:#112233"
    assert Theme.diff_style("diff.added.bg") == "bg:#003b00"
    # Foregrounds keep the appearance's pinned ones under every style.
    assert Theme.diff_style("diff.added.fg") == Theme.DIFF_DARK["diff.added.fg"]


@pytest.mark.parametrize(
    ("ui", "selected", "problem"),
    [
        ({}, "auto", ""),
        ({"diff": {"style": "delta"}}, "delta", ""),
        ({"diff": {"style": "rainbow"}}, "auto", "unknown ui.diff.style 'rainbow'"),
        ({"diff": "delta"}, "auto", "ui.diff must be a table"),
    ],
)
def test_the_configured_diff_style_is_selected_or_reported(ui, selected, problem):
    problems = Theme.configure_diff_style(ui)
    assert Theme.selected_diff_style() == selected
    assert (problem in problems[0]) if problem else not problems


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


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_saving_keeps_the_configs_permissions(tmp_path, mode):
    """The config holds provider keys: a 0600 file came back 0644, the umask's default."""
    config = tmp_path / "config.toml"
    config.write_text('[provider]\nkey = "sk-secret"\n')
    config.chmod(mode)

    ConfigFile.set_runtime(str(config), "theme", "nord")

    assert config.stat().st_mode & 0o777 == mode
    assert not (tmp_path / "config.toml.tmp").exists()


@pytest.mark.parametrize(("text", "expected"), [('runtime = "auto"\n', "runtime in"), ("[runtime\n", "")])
async def test_a_config_that_cannot_be_saved_into_is_reported_not_raised(tmp_path, text, expected):
    """A non-table `runtime` raised TypeError out of /theme, ending the run."""
    command_loop = themed_loop(tmp_path)
    (tmp_path / "config.toml").write_text(text)
    command_loop.presentation.tui = ThemeModal([])

    result = await theme_command(command_loop, "nord")

    assert Theme.name() == "nord"
    assert "Not saved to" in result and expected in result
    assert (tmp_path / "config.toml").read_text() == text


def test_a_theme_file_cannot_take_a_name_runtime_theme_already_means(tmp_path):
    """`auto` is a choice of its own and `gruvbox` a pair: files by those names were listed twice
    and could never be picked."""
    for name in ("auto", "Gruvbox", "mine", "mine-dark", "mine-light", "auto-dark", "auto-light"):
        write_theme(tmp_path, name, 'base = "nord"\n')

    problems = Theme.load_custom(str(tmp_path))

    assert set(Theme.themes()) - set(Theme.BUILTIN) == {"mine-dark", "mine-light", "auto-dark", "auto-light"}
    assert sum("already means something to runtime.theme" in problem for problem in problems) == 3
    assert "auto" not in Theme.pairs() and "mine" in Theme.pairs()
    assert len(Theme.choices()) == len(set(Theme.choices()))


async def test_the_theme_is_configured_before_either_frontend_draws(tmp_path, monkeypatch):
    """The TUI prints its banner and first frame before start_session: configuring the theme there
    drew them in the default palette."""
    command_loop = themed_loop(tmp_path, "nord")
    seen = []

    async def frontend(*_args, **_kwargs):
        seen.append(Theme.name())
        return 0

    monkeypatch.setattr(command_loop, "run_simple", frontend)
    command_loop.interactive_input = False
    await command_loop._run_frontend(show_banner=False)

    assert seen == ["nord"]


async def test_start_session_reports_what_the_theme_could_not_use(tmp_path, monkeypatch):
    command_loop = themed_loop(tmp_path, "mine")
    write_theme(command_loop.session.data_path("themes"), "mine", 'base = "nord"\n[colors]\nnope = "#fff"\n')
    emitted = []
    monkeypatch.setattr(command_loop.presentation, "emit", lambda text="", indent=0: emitted.append(text))

    command_loop.configure_theme()
    command_loop.start_session(show_banner=False)
    await command_loop.background.close_background()

    assert Theme.name() == "mine"
    assert any("unknown role `nope`" in line for line in emitted)


def test_theme_names_complete_after_the_command():
    completions = [completion.text for completion in CommandCompleter().get_completions(Document("/theme gru"), None)]

    assert completions == ["gruvbox", "gruvbox-dark", "gruvbox-light"]


def test_the_app_style_follows_a_theme_switch(tmp_path):
    view = loop(tmp_path).view
    assert view.style().get_attrs_for_style_str("class:choice.selected").bgcolor == "0077a8"

    Theme.set_mode("gruvbox-dark")

    assert view.style().get_attrs_for_style_str("class:choice.selected").bgcolor == "83a598"
