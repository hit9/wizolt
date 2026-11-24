"""The idle-input sparkle field: a sparse, transparent overlay that never touches the input row."""

from types import SimpleNamespace

from wizolt.ui.tui import starfield
from wizolt.ui.tui.app import InputMode, TuiApp
from wizolt.ui.tui.starfield import StarfieldControl


def _row_text(fragments):
    return "".join(text for _style, text in fragments)


def test_control_keeps_the_input_row_clear_and_stars_sparse(monkeypatch):
    monkeypatch.setattr(starfield.time, "monotonic", lambda: 1000.0)
    content = StarfieldControl().create_content(width=60, height=3)
    assert content.line_count == 3
    rows = [content.get_line(i) for i in range(3)]
    # The middle row aligns with the input line: fully transparent, no glyphs, no style.
    assert rows[1] == [("", " " * 60)]
    for y in (0, 2):
        assert len(_row_text(rows[y])) == 60
        stars = [text for style, text in rows[y] if style]
        assert 0 < len(stars) < 60  # sparse, not a solid block
        assert all(ch in starfield.GLYPHS for ch in stars)
        assert all(style == "class:input.sparkle" for style, _ in rows[y] if style)


def test_control_frame_advances_with_time(monkeypatch):
    monkeypatch.setattr(starfield.time, "monotonic", lambda: 1000.0)
    first = StarfieldControl().create_content(60, 3).get_line(0)
    monkeypatch.setattr(starfield.time, "monotonic", lambda: 1000.0 + starfield.INTERVAL)
    second = StarfieldControl().create_content(60, 3).get_line(0)
    assert first != second


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
