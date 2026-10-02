"""Theme switching: named themes, theme files, `/theme`, and redrawing what is already printed."""

import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from test_command_ui import ModalHarness
from test_tui_resize import ReflowingTerminal
from tui_harness import loop, run_interactive_tui, wait_until

import wizolt.ui.render as render_module
from wizolt.config import ConfigFile
from wizolt.ui.cli import CommandCompleter
from wizolt.ui.cli.appearance import theme_command, theme_preview
from wizolt.ui.render import HorizontalRule, MessageBlock, Theme, UiPrinter
from wizolt.ui.themes import BUILTIN, DIFF_STYLES, HEX_ROLES, MENU_TEXT_CONTRAST, MUTED_CONTRAST, contrast
from wizolt.ui.tui.app import TuiApp
from wizolt.utils import terminal

# 24-bit SGR parameters for colors the tests switch between.
SAND_RED = "38;2;243;151;126"  # sand `error`, lifted for text contrast
SAND_GRAY = "38;2;178;160;135"  # sand `rule`
PLUM_RED = "38;2;243;148;176"  # plum `error`

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
    monkeypatch.setattr(Theme, "_bar_themes", {"statusbar": "inherit", "divider": "inherit"})
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

    def set_input_style(self, style):
        self.input_style = style


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
        # Startup scrollback resolves every role, including those used only by the statusbar.
        assert UiPrinter.render_to_ansi([FormattedText([(Theme.fg("user"), "ready")])], columns=80), name


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


def test_named_themes_footer_reads_on_its_band_and_off_it_and_the_divider_keeps_one_hue():
    for name, palette in BUILTIN.items():
        colors = palette.colors
        assert re.fullmatch(r"#[0-9a-f]{6}", colors["status_bg"]), name
        assert colors["divider_label"] == colors["accent"], name
        # A preset lays the footer on the band or leaves it on the background: both must read.
        for role in ("status_base", "status_provider", "status_reason", "status_mcp", "status_context", "status_yolo", "status_agent"):
            assert contrast(colors[role], colors["status_bg"]) >= MUTED_CONTRAST, (name, role)
            assert contrast(colors[role], palette.background) >= MUTED_CONTRAST, (name, role)
    # The terminal-following themes keep the label they always had.
    for name in ("dark", "light"):
        assert Theme.BUILTIN[name].colors["divider_label"] == Theme.BUILTIN[name].colors["accent_secondary"]


@pytest.mark.parametrize("theme", ["dark", "light", "forest", "sand"])
def test_a_status_band_belongs_to_the_preset_not_the_theme(theme):
    from wizolt.ui.bars import FIELDS, STATUS_PRESETS, Template

    Theme.set_mode(theme)
    values = dict.fromkeys(FIELDS, 0)
    values.update(model="m", provider="p", reasoning="high", **{"mcp.label": "mcp 0"})
    band = "bg:" + Theme.color("status_bg")
    for name, banded in (("default", False), ("powerline", False), ("vim", True), ("split", True), ("monitor", True)):
        template = Template("preset:" + name, STATUS_PRESETS)
        parts = template.render(values, 80, Theme.bar_styles(template.styles))
        # The fill across the row carries the band in a banded preset, and nothing in the others.
        fill = max(parts, key=lambda part: len(part[1]) if not part[1].strip() else 0)
        assert (band in fill[0]) == banded and ("bg:" in fill[0]) == banded, name
        if banded:
            assert all(band in style for style, _ in parts), name


def test_powerline_joins_fade_into_the_status_band(tmp_path):
    from tui_harness import loop

    Theme.set_mode("plum")
    bar = loop(tmp_path).presentation.status_bar
    assert not bar.layout.configure({"statusbar": "preset:powerline"}, Theme.bar_styles)
    joins = [style for style, text in bar.fragments() if text in ("", "")]
    # A join at a segment's end takes only the segment's color, so the band shows behind it.
    assert any(re.fullmatch(r"fg:#[0-9a-f]{6}", style) for style in joins)
    assert all("bg:default" not in style for style, _ in bar.fragments())


@pytest.mark.parametrize(("name", "appearance"), [(name, palette.appearance) for name, palette in BUILTIN.items()])
async def test_new_themes_switch_save_and_preview(tmp_path, name, appearance):
    command_loop = themed_loop(tmp_path)
    await theme_command(command_loop, name)

    assert Theme.name() == name
    assert Theme.palette()["pygments"] == BUILTIN[name].colors["pygments"]
    assert Theme.pygments_style() is not None
    assert f'theme = "{name}"' in (tmp_path / "config.toml").read_text()
    preview = theme_preview(name)
    text = "".join(text for _, text in preview)
    assert Theme.appearance() == appearance and "split_words" in text
    assert "Edit " in text and "return WORD.findall(text)" in text
    for label in ("Code", "Removed", "Added", "Your message", "Reply", "Tool call", "Results", "Menu selection"):
        assert label in text
    assert any(Theme.color("user") in style and "tighten" in text for style, text in preview)
    assert any(Theme.color("tool") in style and "Edit" in text for style, text in preview)


def test_menu_descriptions_take_the_menu_text_color(tmp_path):
    view = loop(tmp_path).view
    Theme.set_mode("slate")
    style = view.style()

    for name in ("completion-menu.meta.completion", "completion-menu.hint"):
        assert "#" + style.get_attrs_for_style_str("class:" + name).color == Theme.color("menu_muted"), name


def test_themes_resolve_by_name_and_fall_back_to_dark(monkeypatch):
    assert Theme.resolve("sand") == "sand"
    assert Theme.resolve(" Plum ") == "plum"
    assert Theme.resolve("auto") == "dark"
    assert Theme.resolve("gruvbx") == "dark"
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert Theme.resolve("gruvbx") == "dark"
    assert Theme.resolve("one-dark") == "dark"
    assert Theme.resolve("default") == "dark"
    assert Theme.resolve("auto") == "light"


def test_a_light_and_dark_pair_follows_the_terminal(monkeypatch, tmp_path):
    assert Theme.resolve("papercolor") == "papercolor-dark"
    assert "plum" not in Theme.pairs()
    write_theme(tmp_path, "mine-dark", 'base = "plum"\n')
    write_theme(tmp_path, "mine-light", 'base = "paper"\n')
    Theme.load_custom(str(tmp_path))
    assert Theme.resolve("mine") == "mine-dark"
    monkeypatch.setattr(terminal, "background", lambda: (253, 246, 227))
    assert Theme.resolve("mine") == "mine-light"
    assert Theme.resolve("papercolor") == "papercolor-light"
    # Pairs resolve by name without a row of their own in the menu.
    assert Theme.choices()[:5] == ("auto", "dark", "light", "slate", "forest")
    assert Theme.canonical("Mine") == "mine" and "mine" not in Theme.choices()


def test_configure_reports_an_unknown_theme_and_draws_the_default(tmp_path):
    problems = Theme.configure("gruvbx", str(tmp_path))

    assert Theme.name() == "dark"
    assert len(problems) == 1 and "unknown theme `gruvbx`" in problems[0] and "sand" in problems[0]
    assert Theme.configure("plum", str(tmp_path)) == []
    assert Theme.name() == "plum"


@pytest.mark.asyncio
async def test_inline_theme_starts_and_switches_without_losing_file_themes(tmp_path):
    from wizolt.config import Config

    command_loop = themed_loop(tmp_path, "mine")
    data = {
        "ui": {
            "themes": {
                "mine": {"base": "slate", "colors": {"user": "#abc"}, "diff": {"added": "#123456"}, "highlights": {"badge": {"fg": "#fff", "bold": True}}}
            }
        }
    }
    command_loop.session.config.ui = Config.from_dict(data).ui
    directory = command_loop.session.data_path("themes")
    write_theme(directory, "mine", 'base = "plum"\n[colors]\nuser = "#000"\n')
    write_theme(directory, "legacy", 'base = "plum"\n')

    command_loop.configure_theme()
    assert Theme.name() == "mine"
    assert Theme.color("user") == "#aabbcc"
    assert "bg:#123456" in Theme.themes()["mine"].diff.values()
    assert Theme.themes()["mine"].highlights["badge"] == "fg:#ffffff bold"
    assert "legacy" in Theme.choices()
    await theme_command(command_loop, "legacy")
    assert Theme.name() == "legacy"
    await theme_command(command_loop, "mine")
    assert Theme.color("user") == "#aabbcc"
    assert ConfigFile.load(command_loop.session.config.path)["runtime"]["theme"] == "mine"


def test_inline_themes_work_without_a_theme_directory_and_report_bad_entries(tmp_path):
    inline = {
        "mine": {"base": "slate", "colors": {"user": "#abc", "tool": "invalid"}},
        "bad-base": {"base": "missing"},
        "bad-shape": "wrong",
        "dark": {},
        "auto": {},
    }
    problems = Theme.configure("mine", str(tmp_path / "absent"), inline)
    assert Theme.name() == "mine"
    assert Theme.color("user") == "#aabbcc"
    assert Theme.color("tool") == Theme.BUILTIN["slate"].colors["tool"]
    for name in ("mine", "bad-base", "bad-shape", "dark", "auto"):
        assert any(f"ui.themes.{name}" in problem for problem in problems)
    assert set(Theme.themes()) - set(Theme.BUILTIN) == {"mine"}
    assert "[ui.themes] must be a table" in Theme.load_custom(str(tmp_path), "wrong")


def test_invalid_inline_theme_keeps_same_named_file(tmp_path):
    write_theme(tmp_path, "mine", 'base = "plum"\n')
    problems = Theme.configure("mine", str(tmp_path), {"mine": {"base": "missing"}})
    assert any("ui.themes.mine" in problem for problem in problems)
    assert Theme.color("user") == Theme.BUILTIN["plum"].colors["user"]


@pytest.mark.parametrize("role", ["status_base", "divider_label", "warning", "error", "status_bg", "menu_bg", "accent", "syntax_default"])
@pytest.mark.parametrize("color", ["ansicyan", "default"])
def test_custom_terminal_colors_render_bars_and_file_picker(tmp_path, role, color):
    write_theme(tmp_path, "mine", f'base = "slate"\n[colors]\n{role} = "{color}"\n')
    assert Theme.configure("mine", str(tmp_path)) == []
    assert Theme.color(role) == color
    # Both adapters adjust contrast for named bases, but terminal-owned colors have no
    # known RGB value. Valid custom colors must remain usable in either UI.
    assert Theme.bar_styles({"status.detail", "divider.badge", "status.warning", "status.error"})
    assert "fg:" in Theme.fzf_colors()


@pytest.mark.parametrize("value", [[], {}, ["classic"], {"name": "classic"}])
def test_invalid_diff_config_reports_a_problem_instead_of_crashing(value):
    Theme.set_diff_style("classic")
    problems = Theme.configure_diff_style({"diff": {"style": value}})
    assert any("unknown ui.diff.style" in problem for problem in problems)
    assert Theme.selected_diff_style() == "auto"


def test_a_theme_file_overrides_roles_on_its_base(tmp_path):
    write_theme(tmp_path, "mine", 'base = "plum"\npygments = "dracula"\n[colors]\naccent = "#AbC"\nerror = "ansired"\n')

    assert Theme.load_custom(str(tmp_path)) == []
    Theme.set_mode("mine")

    assert Theme.color("accent") == "#aabbcc"
    assert Theme.color("error") == "ansired"
    assert Theme.color("tool") == Theme.BUILTIN["plum"].colors["tool"]
    assert Theme.appearance() == "dark"
    assert Theme.palette()["pygments"] == "dracula"


def test_theme_file_mistakes_are_reported_and_the_rest_of_the_file_applies(tmp_path):
    write_theme(
        tmp_path,
        "rough",
        'base = "plum"\npygments = "no-such-style"\nextra = 1\n[colors]\naccent = "teal-ish"\ndivider_glow = "ansicyan"\nnope = "#fff"\nsuccess = "#00ff00"\n',
    )
    write_theme(tmp_path, "broken", "base = ")
    write_theme(tmp_path, "sand", 'base = "dark"\n')
    write_theme(tmp_path, "orphan", 'base = "no-such-base"\n')

    problems = Theme.load_custom(str(tmp_path))

    assert set(Theme.themes()) - set(Theme.BUILTIN) == {"rough"}
    for expected in (
        "broken.toml",
        "`sand` is a built-in",
        "base must be one of",
        "no-such-style",
        "extra",
        "teal-ish",
        "divider_glow must be a #rrggbb",
        "unknown role `nope`",
    ):
        assert any(expected in problem for problem in problems), expected
    Theme.set_mode("rough")
    base = Theme.BUILTIN["plum"].colors
    assert Theme.color("success") == "#00ff00"
    assert (Theme.color("accent"), Theme.color("divider_glow"), Theme.palette()["pygments"]) == (base["accent"], base["divider_glow"], "dracula")


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
    assert captured == str(line) and "boom" in captured and SAND_RED not in captured
    Theme.set_mode("sand")

    assert SAND_RED in line(80) and "boom" in line(80)
    assert str(line) == captured, "the capture itself is what text readers of the transcript see"
    assert SAND_GRAY in rule(80)


def test_transcript_rows_share_one_style_per_theme(monkeypatch):
    """A style merged per row starts with an empty lookup cache: it made every emitted line ten
    times as expensive to render. One style per theme keeps a row's cost what it was."""
    merges = []
    real_merge = render_module.merge_styles
    monkeypatch.setattr(render_module, "merge_styles", lambda styles: merges.append(len(styles)) or real_merge(styles))

    def render_rows(count):
        for _ in range(count):
            UiPrinter.render_to_ansi([FormattedText([(Theme.fg("error"), "boom\n")])], 80, color_depth=ColorDepth.DEPTH_8_BIT)

    Theme.set_mode("plum")
    render_rows(3)
    assert len(merges) == 1
    Theme.set_mode("forest")
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
    Theme.set_mode("sand")
    assert "38;2;255;196;109" in row(80)  # sand amber, not its nearest 256-color neighbour
    monkeypatch.setenv("COLORTERM", "")
    Theme.set_mode("sand")
    assert "38;2;" not in row(80)


def test_a_theme_file_draws_at_its_bases_depth(monkeypatch, tmp_path):
    monkeypatch.setenv("COLORTERM", "truecolor")
    write_theme(tmp_path, "warm", 'base = "sand"\n[colors]\naccent = "#abcdef"\n')
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
        ("plum", "auto", {}, "theme plum"),
        ("dark", "delta", {}, "diff style delta"),
        ("plum", "auto", {"COLORTERM": "truecolor"}, ""),
        ("plum", "auto", {"PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_8_BIT"}, ""),
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
    command_loop = themed_loop(tmp_path, "plum")
    command_loop.configure_theme()
    assert any("COLORTERM" in problem for problem in command_loop.theme_problems)

    command_loop.presentation.tui = ThemeModal([])
    result = await theme_command(command_loop, "forest")
    assert "The theme forest is drawn in exact colors" in result


def test_the_app_draws_at_the_depth_the_theme_asks_for(monkeypatch):
    monkeypatch.setenv("COLORTERM", "24bit")
    app = TuiApp()
    depths = []

    def drive(_pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        depths.append(app.app.color_depth)
        Theme.set_mode("plum")
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
    Theme.set_mode("plum")
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
        switch("plum", PLUM_RED)
        switch("sand", SAND_RED)
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

    result = await theme_command(command_loop, "Sand")

    assert Theme.name() == "sand" and command_loop.session.settings.theme == "sand"
    assert tui.recolored == 1
    assert "saved as runtime.theme" in result
    assert (tmp_path / "config.toml").read_text() == CONFIG.replace('"auto"', '"sand"')


async def test_a_pair_is_saved_as_the_pair_so_the_next_terminal_can_differ(tmp_path):
    command_loop = themed_loop(tmp_path)
    command_loop.presentation.tui = ThemeModal([])

    write_theme(command_loop.session.data_path("themes"), "mine-dark", 'base = "sand"\n')
    write_theme(command_loop.session.data_path("themes"), "mine-light", 'base = "paper"\n')
    await theme_command(command_loop, "mine")

    assert Theme.name() == "mine-dark"
    assert 'theme = "mine"' in (tmp_path / "config.toml").read_text()


async def test_an_unknown_theme_name_changes_nothing(tmp_path):
    command_loop = themed_loop(tmp_path)
    tui = command_loop.presentation.tui = ThemeModal([])

    result = await theme_command(command_loop, "gruvbx")

    assert result.startswith("Unknown theme: gruvbx.") and "sand" in result
    assert Theme.name() == "dark" and tui.recolored == 0
    assert (tmp_path / "config.toml").read_text() == CONFIG


async def test_without_a_picker_theme_lists_the_themes_and_marks_the_configured_one(tmp_path):
    command_loop = themed_loop(tmp_path, "plum")
    command_loop.interactive_input = False

    listing = (await theme_command(command_loop, "")).splitlines()

    assert listing[0] == "  auto"
    assert "* plum" in listing and "  sand" in listing


async def test_the_picker_applies_each_row_it_lands_on_and_escape_restores_the_theme(tmp_path):
    command_loop = themed_loop(tmp_path, "slate")
    Theme.set_mode("slate")
    command_loop.interactive_input = True
    tui = command_loop.presentation.tui = ThemeModal(["j", "j", "escape"])

    assert await theme_command(command_loop, "") is None

    # A pair row draws the variant the terminal gets.
    assert tui.painted == ["forest", "sand", "slate"]
    assert Theme.name() == "slate" and tui.recolored == 0
    assert (tmp_path / "config.toml").read_text() == CONFIG.replace('"auto"', '"slate"')
    # The preview is drawn in the row's theme: the user row in forest's orange on its frame.
    assert any("split_words" in text for _, text in tui.frames[1])


async def test_the_picker_restores_the_theme_when_interrupted(tmp_path):
    command_loop = themed_loop(tmp_path, "slate")
    Theme.set_mode("slate")
    command_loop.interactive_input = True
    command_loop.presentation.tui = ThemeModal(["j", "c-c"])

    with pytest.raises(KeyboardInterrupt):
        await theme_command(command_loop, "")

    assert Theme.name() == "slate"


async def test_enter_keeps_the_previewed_theme_and_saves_it(tmp_path):
    command_loop = themed_loop(tmp_path, "slate")
    Theme.set_mode("slate")
    command_loop.interactive_input = True
    tui = command_loop.presentation.tui = ThemeModal(["j", "enter"], consumed=True)

    result = await theme_command(command_loop, "")

    assert Theme.name() == "forest" and tui.recolored == 1 and Theme.selected_diff_style() == "auto"
    assert result.startswith("Theme: forest (saved")
    assert 'theme = "forest"' in (tmp_path / "config.toml").read_text()


async def test_the_theme_tabs_save_both_colorscheme_and_diff(tmp_path):
    command_loop = themed_loop(tmp_path, "slate")
    Theme.set_mode("slate")
    command_loop.interactive_input = True
    # Theme: slate -> forest. Diff colors: auto -> forest's own, the first style listed.
    tui = command_loop.presentation.tui = ThemeModal(["j", "right", "j", "enter"], consumed=True)

    result = await theme_command(command_loop, "")

    assert Theme.name() == "forest" and Theme.selected_diff_style() == "classic"
    assert "Theme: forest (saved" in result and "Diff colors: classic (saved as ui.diff.style" in result
    config = (tmp_path / "config.toml").read_text()
    assert 'theme = "forest"' in config and '[ui.diff]\nstyle = "classic"' in config
    assert command_loop.session.config.ui["diff"] == {"style": "classic"}
    # One redraw for both choices, after the second.
    assert tui.recolored == 1
    # The preview is drawn in the focused style's changed-word band.
    assert any(style.endswith(Theme.diff_style("diff.added.emph")) for style, _ in tui.frames[-1])


async def test_escape_on_the_diff_tab_restores_all_changes(tmp_path):
    command_loop = themed_loop(tmp_path, "slate")
    Theme.set_mode("slate")
    command_loop.interactive_input = True
    tui = command_loop.presentation.tui = ThemeModal(["j", "l", "j", "escape"], consumed=True)

    result = await theme_command(command_loop, "")

    assert result is None
    assert Theme.name() == "slate" and Theme.selected_diff_style() == "auto"
    assert (tmp_path / "config.toml").read_text() == CONFIG.replace('"auto"', '"slate"')
    assert tui.recolored == 0


async def test_theme_by_name_leaves_the_diff_colors_alone(tmp_path):
    command_loop = themed_loop(tmp_path)
    command_loop.presentation.tui = ThemeModal([])
    Theme.set_diff_style("classic")

    result = await theme_command(command_loop, "plum")

    assert "Diff colors" not in result and Theme.diff_style_name() == "classic"


def test_builtin_palettes_use_classic_by_default_and_all_diff_choices_support_both_appearances():
    assert set(BUILTIN) == {"slate", "forest", "sand", "plum", "paper", "gruvbox-dark", "solarized-dark", "dracula", "papercolor-light", "papercolor-dark"}
    assert set(DIFF_STYLES) == {"classic", "delta", "zebra"}
    for name in Theme.BUILTIN:
        Theme.set_mode(name)
        Theme.set_diff_style("auto")
        assert Theme.diff_style_name() == "classic"
        assert Theme.diff_styles() == ("auto", "classic", "delta", "zebra")
        for style in DIFF_STYLES:
            Theme.set_diff_style(style)
            assert Theme.diff_style_name() == style
            for side in ("added", "removed"):
                assert Theme.diff_style(f"diff.{side}.bg") != Theme.diff_style(f"diff.{side}.emph")
    assert Theme.configure_diff_style({"diff": {"style": "tokyonight"}})
    assert Theme.selected_diff_style() == "auto"


@pytest.mark.parametrize("name", ("slate", "forest", "sand", "plum"))
def test_markdown_code_blocks_on_dark_themes_do_not_fall_back_to_black(name):
    from wizolt.ui import markdown as markdown_module

    Theme.set_mode(name)
    console = markdown_module.markdown_console(60)
    with console.capture() as capture:
        console.print(markdown_module.WizoltMarkdown("A draft:\n\n    feat: plain words\n\n```python\nx = 1\n```\n"))
    assert not re.search(r"38;2;0;0;0[;m]", capture.get())
    assert Theme.pygments_style() is not None


def test_a_theme_file_inherits_its_bases_diff_pairing_and_its_bands_win(tmp_path):
    write_theme(tmp_path, "mine", 'base = "forest"\n[diff]\nremoved = "#112233"\n')
    assert not Theme.load_custom(str(tmp_path))
    Theme.set_mode("mine")
    assert Theme.diff_style_name() == "classic"
    # The file's base also lends it the code colors generated for it.
    assert Theme.pygments_style() is Theme._pygments_cache["zenburn"]
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
    command_loop = themed_loop(tmp_path, "plum")
    Theme.set_mode("plum")

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

    result = await theme_command(command_loop, "plum")

    assert Theme.name() == "plum"
    assert "Not saved to" in result and expected in result
    assert (tmp_path / "config.toml").read_text() == text


def test_a_theme_file_cannot_take_a_name_runtime_theme_already_means(tmp_path):
    """`auto` and built-in names are reserved, as are custom pairs: files by those names were listed twice
    and could never be picked."""
    for name in ("auto", "Slate", "mine", "mine-dark", "mine-light", "auto-dark", "auto-light"):
        write_theme(tmp_path, name, 'base = "plum"\n')

    problems = Theme.load_custom(str(tmp_path))

    assert set(Theme.themes()) - set(Theme.BUILTIN) == {"mine-dark", "mine-light", "auto-dark", "auto-light"}
    assert sum("already means something to runtime.theme" in problem for problem in problems) == 3
    assert "auto" not in Theme.pairs() and "mine" in Theme.pairs()
    assert len(Theme.choices()) == len(set(Theme.choices()))


async def test_the_theme_is_configured_before_either_frontend_draws(tmp_path, monkeypatch):
    """The TUI prints its banner and first frame before start_session: configuring the theme there
    drew them in the default palette."""
    command_loop = themed_loop(tmp_path, "plum")
    seen = []

    async def frontend(*_args, **_kwargs):
        seen.append(Theme.name())
        return 0

    monkeypatch.setattr(command_loop, "run_simple", frontend)
    command_loop.interactive_input = False
    await command_loop._run_frontend(show_banner=False)

    assert seen == ["plum"]


async def test_start_session_reports_what_the_theme_could_not_use(tmp_path, monkeypatch):
    command_loop = themed_loop(tmp_path, "mine")
    write_theme(command_loop.session.data_path("themes"), "mine", 'base = "plum"\n[colors]\nnope = "#fff"\n')
    emitted = []
    monkeypatch.setattr(command_loop.presentation, "emit", lambda text="", indent=0: emitted.append(text))

    command_loop.configure_theme()
    command_loop.start_session(show_banner=False)
    await command_loop.background.close_background()

    assert Theme.name() == "mine"
    assert any("unknown role `nope`" in line for line in emitted)


def test_theme_names_complete_after_the_command():
    completions = [completion.text for completion in CommandCompleter().get_completions(Document("/theme s"), None)]

    # A pair is accepted by name but not listed beside its two themes.
    assert completions == ["slate", "sand", "solarized-dark", "statusbar ", "sweep "]


@pytest.mark.parametrize("name", ("dark", "light", *BUILTIN))
def test_user_message_background_fills_wrapped_and_empty_rows(name):
    Theme.set_mode(name)
    block = MessageBlock(UiPrinter(), "A long message with 中文\n\nAnother line", "user", 0, False)
    for width in (12, 40):
        rows = list(split_lines(block.fragments(width)))[:-1]
        assert rows
        assert "A long" in "".join(text for _, text in rows[0])
        assert "Another line" in "".join(text for _, text in rows[-1])
        for row in rows:
            assert sum(get_cwidth(text) for _, text in row) == width
            assert all(f"bg:{Theme.color('user_bg')}" in style for style, text in row if text)
    if name in BUILTIN:
        assert contrast(Theme.color("user"), Theme.color("user_bg")) >= 4.5
    reply = MessageBlock(UiPrinter(), "Reply", "assistant", 0, False)
    assert not any("bg:" in style for style, text in reply.fragments(40) if text)


@pytest.mark.parametrize("background", ("#000000", "#123100", "#12313f"))
def test_custom_user_background_preserves_full_width_padding(tmp_path, background):
    Theme.configure("review", str(tmp_path), {"review": {"base": "dark", "colors": {"user_bg": background}}})
    block = MessageBlock(UiPrinter(), "hello", "user", 0, False)
    rows = list(split_lines(block.fragments(20)))[:-1]
    assert len(rows) == 1
    assert all(sum(get_cwidth(text) for _, text in row) == 20 for row in rows)


def test_the_app_style_follows_a_theme_switch(tmp_path):
    view = loop(tmp_path).view
    assert view.style().get_attrs_for_style_str("class:choice.selected").bgcolor == "0077a8"

    Theme.set_mode("sand")

    assert view.style().get_attrs_for_style_str("class:choice.selected").bgcolor == "83bec3"


@pytest.mark.parametrize("name", tuple(BUILTIN))
def test_every_builtin_bar_keeps_text_readable_on_its_actual_background(name):
    from prompt_toolkit.styles import Style

    from wizolt.ui.bars import FIELDS, PRESETS, BarLayout

    Theme.set_mode(name)
    palette = BUILTIN[name]
    style = Style([])
    layout = BarLayout()
    values = dict.fromkeys(FIELDS, 0)
    values.update(
        provider="test",
        model="model",
        reasoning="max",
        yolo=True,
        running=True,
        activity="working",
        label="working (12s)",
        elapsed=12,
        rate="42 tok/s",
        spinner="● ",
        **{
            "agent.name": "reader",
            "cache.percent": 88,
            "mcp.label": "mcp 2",
            "skills.count": 3,
            "queue.followup": 2,
            "queue.total": 2,
        },
    )
    for kind in ("statusbar", "divider"):
        for preset in PRESETS[kind]:
            assert not layout.configure({kind: "preset:" + preset}, Theme.bar_styles)
            for pressure in (37, 75, 95):
                values["context.percent"] = pressure
                for spec, text in layout.render(kind, values, 160, Theme.bar_styles):
                    if not any(char.isalnum() for char in text):
                        continue  # Decorative rules, joins and empty meter cells need no text contrast.
                    attrs = style.get_attrs_for_style_str(spec)
                    foreground = "#" + attrs.color if len(attrs.color) == 6 else palette.colors["syntax_default"]
                    background = "#" + attrs.bgcolor if len(attrs.bgcolor) == 6 else palette.background
                    assert contrast(foreground, background) >= 4.5, (name, preset, pressure, text)


async def test_file_picker_uses_the_current_colors_each_time_it_opens(tmp_path, monkeypatch):
    from wizolt.mentions import FilePick
    from wizolt.ui.cli.runtime import TuiRuntime

    command_loop = themed_loop(tmp_path)
    tui = TuiRuntime(command_loop).build_tui(TuiApp())
    calls = []

    async def pick(query, *, colors):
        calls.append((query, colors))
        return FilePick()

    monkeypatch.setattr(command_loop.session.mentions.picker, "pick", pick)
    command_loop.background.open_background()
    try:
        for name in ("sand", "plum", "paper"):
            Theme.set_mode(name)
            await tui.file_picker_fn("parser")
            query, colors = calls[-1]
            palette = Theme.palette()
            assert query == "parser"
            assert "bg:" + palette["menu_bg"] in colors
            assert "bg+:" + palette["selection_bg"] in colors
            assert "fg+:" + palette["selection_fg"] in colors
        monkeypatch.setenv("NO_COLOR", "1")
        await tui.file_picker_fn("")
        assert calls[-1] == ("", "bw")
    finally:
        await command_loop.background.close_background()


def test_file_picker_translates_terminal_colors_for_fzf():
    colors = Theme.fzf_colors()
    assert "ansi" not in colors and "header:8" in colors


@pytest.mark.parametrize("name", ("papercolor-light", "papercolor-dark"))
def test_papercolor_code_colors_are_inherited_by_custom_themes(name, tmp_path):
    from pygments.token import Keyword, Name, Operator, String

    write_theme(tmp_path, "mine", f'base = "{name}"\n')
    assert Theme.configure("mine", str(tmp_path)) == []
    style = Theme.pygments_style()
    assert style is not None
    expected = ("d70087", "0087af", "5f8700") if name.endswith("light") else ("afd700", "5fafd7", "d7af5f")
    assert tuple(style.style_for_token(token)["color"] for token in (Keyword, Name.Function, String)) == expected
    # Upstream Type is pink (like pythonStatement); pythonOperator is purple.
    assert style.style_for_token(Keyword.Type)["color"] == expected[0]
    assert style.style_for_token(Name.Class)["color"] == expected[0]
    assert style.style_for_token(Operator)["color"] == ("8700af" if name.endswith("light") else "af87d7")


def test_component_auto_and_pairs_follow_terminal_independently(monkeypatch, tmp_path):
    command_loop = themed_loop(tmp_path, "plum")
    command_loop.session.config.ui = {"statusbar": {"theme": "papercolor"}, "divider": {"theme": "auto"}}
    command_loop.configure_theme()
    assert Theme.bar_palette("statusbar") is Theme.BUILTIN["papercolor-dark"]
    assert Theme.bar_palette("divider") is Theme.BUILTIN["dark"]
    before = command_loop.view.style()
    monkeypatch.setattr(terminal, "background", lambda: (255, 255, 255))
    assert Theme.bar_palette("statusbar") is Theme.BUILTIN["papercolor-light"]
    assert Theme.bar_palette("divider") is Theme.BUILTIN["light"]
    assert command_loop.view.style() is not before
    assert command_loop.view.style().get_attrs_for_style_str("class:queue.rule").color == "9ca3af"
    assert Theme.name() == "plum"


def test_component_themes_keep_transcript_colors_and_respect_no_color(monkeypatch):
    Theme.set_mode("dark")
    before = Theme.transcript_style().get_attrs_for_style_str("class:role.user")
    Theme.set_bar_theme("statusbar", "sand")
    Theme.set_bar_theme("divider", "forest")
    assert Theme.transcript_style().get_attrs_for_style_str("class:role.user") == before
    assert Theme.wants_true_color()
    assert "statusbar theme sand" in Theme.true_color_warning()
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert Theme.color_depth(ColorDepth.DEPTH_8_BIT) == ColorDepth.DEPTH_24_BIT
    monkeypatch.setenv("NO_COLOR", "1")
    assert Theme.color_depth(ColorDepth.DEPTH_24_BIT) == ColorDepth.DEPTH_1_BIT


def test_reloading_or_removing_custom_component_theme(tmp_path):
    write_theme(tmp_path, "mine", 'base = "sand"\n[colors]\nstatus_base = "#123456"\n')
    Theme.load_custom(str(tmp_path))
    Theme.set_bar_theme("statusbar", "mine")
    assert "#123456" in Theme.bar_styles({"status_base"})["status_base"]
    write_theme(tmp_path, "mine", 'base = "forest"\n[colors]\nstatus_base = "#abcdef"\n')
    Theme.load_custom(str(tmp_path))
    assert "#abcdef" in Theme.bar_styles({"status_base"})["status_base"]
    Path(tmp_path, "mine.toml").unlink()
    Theme.load_custom(str(tmp_path))
    assert Theme.bar_palette("statusbar") is Theme.active()
