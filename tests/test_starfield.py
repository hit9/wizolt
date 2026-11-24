"""The idle-input sparkle field: a sparse, transparent overlay that never touches the input row."""

from types import SimpleNamespace

import pytest
from prompt_toolkit.styles import Style

from wizolt.ui.tui import starfield
from wizolt.ui.tui.app import InputMode, TuiApp
from wizolt.ui.tui.starfield import StarfieldControl


def _row_text(fragments):
    return "".join(text for _style, text in fragments)


def _ctl(**kw):
    return StarfieldControl(lambda: "#ffffff", lambda: "#000000", **kw)


def test_control_paints_fixed_braille_dots_and_masks_reserved(monkeypatch):
    monkeypatch.setattr(starfield.time, "monotonic", lambda: 1000.0)
    row = _ctl(reserved_fn=lambda: 10).create_content(60, 1).get_line(0)
    assert len(_row_text(row)) == 60
    assert _row_text(row)[:10] == " " * 10
    assert all(not style for style, _ in row[:10])
    dots = [text for style, text in row if style]
    assert dots and all(ch in starfield.DOTS or ch == "✦" for ch in dots)
    assert all(style.startswith("fg:#") for style, _ in row if style)
    # Deterministic: the same instant paints the same cells.
    assert _ctl(reserved_fn=lambda: 10).create_content(60, 1).get_line(0) == row


@pytest.mark.parametrize(("fg", "bg"), [("ansicyan", "#222222"), ("#abcdee", "default"), ("default", "ansiblack")])
def test_terminal_palette_twinkles_in_the_theme_foreground(monkeypatch, fg, bg):
    now, visibility = 1000.0, 1.0
    monkeypatch.setattr(starfield, "time", SimpleNamespace(monotonic=lambda: now))
    field = StarfieldControl(lambda: fg, lambda: bg, visibility_fn=lambda: visibility)
    style = Style.from_dict({})
    first = field.create_content(100, 1).get_line(0)
    dots = [style.get_attrs_for_style_str(s) for s, glyph in first if glyph.strip()]
    assert dots and all(attrs.color == fg.removeprefix("#") for attrs in dots)
    now += 2
    assert _row_text(field.create_content(100, 1).get_line(0)) != _row_text(first)
    visibility = 0
    assert not _row_text(field.create_content(100, 1).get_line(0)).strip()


def test_control_blank_when_inactive():
    assert _ctl(active_fn=lambda: False).create_content(60, 1).get_line(0) == [("", " " * 60)]


def test_control_twinkles_in_place_over_time(monkeypatch):
    monkeypatch.setattr(starfield.time, "monotonic", lambda: 1000.0)
    first = _ctl().create_content(60, 1).get_line(0)
    monkeypatch.setattr(starfield.time, "monotonic", lambda: 1002.0)
    second = _ctl().create_content(60, 1).get_line(0)
    assert first != second  # brightness shifts
    # A cell never swaps between two different dots (no movement); it may only flare braille ↔ ✦.
    for (_s1, t1), (_s2, t2) in zip(first, second):
        if t1 != " " and t2 != " ":
            assert t1 == t2 or "✦" in (t1, t2)


def _idle_tui(rows=30):
    tui = TuiApp()
    tui.starfield_enabled_fn = lambda: True
    tui.app = SimpleNamespace(output=SimpleNamespace(get_size=lambda: SimpleNamespace(rows=rows, columns=80)), is_running=False)
    tui.input_mode = InputMode.CHAT
    tui._starfield_idle_since = starfield.time.monotonic()
    return tui


def test_active_only_when_enabled_idle_empty_chat_and_tall(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(starfield.time, "monotonic", lambda: now)
    monkeypatch.setattr("wizolt.ui.tui.app.time.monotonic", lambda: now)
    tui = _idle_tui()
    assert tui.starfield_active()
    tui.starfield_enabled_fn = lambda: False
    assert not tui.starfield_active()
    tui.starfield_enabled_fn = lambda: True
    tui.input_mode = InputMode.RUNNING
    assert not tui.starfield_active()
    tui.input_mode = InputMode.CHAT
    tui._starfield_idle_since = None
    assert not tui.starfield_active()
    tui._starfield_idle_since = now - starfield.TIMEOUT
    assert not tui.starfield_active()
    tui._starfield_idle_since = now
    assert not _idle_tui(rows=10).starfield_active()


def test_typing_hides_the_field_and_clearing_restarts_the_clock(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(starfield.time, "monotonic", lambda: now)
    monkeypatch.setattr("wizolt.ui.tui.app.time.monotonic", lambda: now)
    tui = _idle_tui()
    assert tui.starfield_active()
    tui.input_buffer.text = "hello"
    assert tui._starfield_idle_since is None
    assert not tui.starfield_active()
    tui.input_buffer.text = ""
    assert tui._starfield_idle_since == now
    assert tui.starfield_active()
