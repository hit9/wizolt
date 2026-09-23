"""tui hints (split from tests/test_tui_app.py)."""
import threading
from types import SimpleNamespace

from test_tui_app import _StubJob, quick_hint_app
from tui_harness import ResizableOutput, loop, rendered_screen_text, run_interactive_tui, wait_until

from wizolt.base import oneline
from wizolt.cli import hints
from wizolt.cli.hints import HintPicker
from wizolt.tools import NextHintsTool
from wizolt.tui import TuiApp


def test_tui_chat_input_shows_random_idle_placeholder(tmp_path):
    command_loop = loop(tmp_path)
    command_loop.tui = TuiApp()

    hint = command_loop.view.tui_input_hint()
    assert hint in {entry.text for entry in hints.HINTS}
    assert command_loop.view.tui_input_hint() == hint  # stable within a situation (no flicker)

def test_tui_chat_input_says_starting_until_startup_settles(tmp_path):
    command_loop = loop(tmp_path)
    command_loop.tui = TuiApp()
    command_loop.view._hint_picker = HintPicker(choice=lambda pool: pool[-1])
    command_loop.starting = True

    assert command_loop.view.tui_input_hint() == "starting…"
    command_loop.tui.set_running("working")
    assert command_loop.view.tui_input_hint() != "starting…"  # a running turn keeps its queue hint

    command_loop.tui.set_idle()
    command_loop.starting = False
    assert command_loop.view.tui_input_hint() == "/sessions resumes a past session"


def test_tui_idle_hint_sessions_only_before_work(tmp_path):
    command_loop = loop(tmp_path)
    command_loop.tui = TuiApp()
    command_loop.view._hint_picker = HintPicker(choice=lambda pool: pool[-1])

    # Early session: the pool ends with the /sessions hint (the only early-only entry).
    assert command_loop.view.tui_input_hint() == "/sessions resumes a past session"

    # Once work exists the session is no longer early and /sessions leaves the pool.
    command_loop.session.store_tool_result("Bash", ["ls"], "ok")
    assert command_loop.view.tui_input_hint() == "Type / for commands"  # last technique hint

def test_tui_idle_hint_favors_diff_right_after_editing(tmp_path):
    command_loop = loop(tmp_path)
    command_loop.tui = TuiApp()
    command_loop.view._hint_picker = HintPicker(choice=lambda pool: pool[-1])
    command_loop.session.store_tool_result("Bash", ["ls"], "ok")  # mature phase
    command_loop.session.state.round_count = 1
    command_loop.session.store_turn_diff("tr.1", 1, "a.py", "diff", round=1)

    # The post-edit pool ends with the weighted /diff copies.
    assert command_loop.view.tui_input_hint() == "/diff reviews recent edits"

    # A later round without edits drops /diff back out of the pool.
    command_loop.session.state.round_count = 2
    assert command_loop.view.tui_input_hint() == "Type / for commands"

def test_tui_idle_hint_ps_while_jobs_running(tmp_path):
    command_loop = loop(tmp_path)
    command_loop.tui = TuiApp()
    command_loop.view._hint_picker = HintPicker(choice=lambda pool: pool[-1])
    command_loop.session.store_tool_result("Bash", ["ls"], "ok")  # mature

    assert command_loop.view.tui_input_hint() == "Type / for commands"  # no jobs yet

    command_loop.session.jobs["j1"] = _StubJob("running")
    assert command_loop.view.tui_input_hint() == "/ps lists background jobs"

    command_loop.session.jobs["j1"] = _StubJob("done")  # finished -> hint clears
    assert command_loop.view.tui_input_hint() == "Type / for commands"

def test_tui_hint_context_projects_availability(tmp_path):
    command_loop = loop(tmp_path)
    session = command_loop.session

    session.skills = None
    session.mcp = None
    session.jobs.clear()
    ctx = command_loop.view._hint_context()
    assert not ctx.skills_available and not ctx.mcp_connected and not ctx.jobs_running

    session.skills = SimpleNamespace(skills={"demo": object()})
    session.mcp = SimpleNamespace(tools={"srv": []})
    session.jobs["j1"] = _StubJob("running")
    ctx = command_loop.view._hint_context()
    assert ctx.skills_available and ctx.mcp_connected and ctx.jobs_running

def test_tui_idle_hint_rerolls_each_turn(tmp_path):
    command_loop = loop(tmp_path)
    command_loop.tui = TuiApp()
    picks = iter(["first", "second"])
    command_loop.view._hint_picker = HintPicker(choice=lambda pool: next(picks))
    command_loop.session.state.round_count = 1
    assert command_loop.view.tui_input_hint() == "first"
    assert command_loop.view.tui_input_hint() == "first"  # stable within the round
    command_loop.session.state.round_count = 2
    assert command_loop.view.tui_input_hint() == "second"  # a new turn re-rolls

def test_quick_hint_tab_cycles_focus_and_wraps():
    app, _ = quick_hint_app()
    assert app.quick_hint_focus == -1
    for expected in (0, 1, 2, -1):
        app.tab_or_complete(app.input_buffer, reverse=False)
        assert app.quick_hint_focus == expected

def test_quick_hint_shift_tab_cycles_focus_backwards_and_wraps():
    app, _ = quick_hint_app()
    assert app.quick_hint_focus == -1
    for expected in (2, 1, 0, -1):
        app.tab_or_complete(app.input_buffer, reverse=True)
        assert app.quick_hint_focus == expected

def test_quick_hint_tab_falls_through_to_completion_with_text():
    app, _ = quick_hint_app()
    app.input_buffer.insert_text("/mod")
    app.tab_or_complete(app.input_buffer, reverse=False)
    assert app.quick_hint_focus == -1

def test_quick_hint_tab_ignored_without_hints():
    app, _ = quick_hint_app(())
    app.tab_or_complete(app.input_buffer, reverse=False)
    assert app.quick_hint_focus == -1

def test_quick_hint_enter_picks_chip_and_returns_focus():
    app, submitted = quick_hint_app()
    app.quick_hint_focus = 1
    assert app._pick_quick_hint(app.input_buffer)
    assert submitted == []
    assert app.input_buffer.text == "show the diff"
    assert app.quick_hint_focus == -1  # Enter returns focus to the input line
    app._accept(app.input_buffer)  # a second Enter sends
    assert [str(value) for value in submitted] == ["show the diff"]

def test_quick_hint_pick_ignored_with_completion_menu_open():
    app, submitted = quick_hint_app()
    app.quick_hint_focus = 1
    app.input_buffer.complete_state = object()  # a completion menu is open
    assert app._pick_quick_hint(app.input_buffer) is False
    assert submitted == []
    assert app.input_buffer.text == ""

def test_quick_hint_pick_ignored_while_running():
    app, submitted = quick_hint_app()
    app.quick_hint_focus = 0
    app.set_running("working")
    assert app._pick_quick_hint(app.input_buffer) is False
    assert submitted == []

def test_quick_hint_enter_on_empty_unfocused_input_does_nothing():
    app, submitted = quick_hint_app()
    app._accept(app.input_buffer)
    assert submitted == []

def test_quick_hint_enter_picks_chips_one_per_tab_and_sends():
    """Enter picks the focused chip and returns to the prompt; Tab to the next chip and Enter
    again combines, and a final Enter sends the whole text."""
    app, submitted = quick_hint_app()
    app.quick_hint_focus = 0
    assert app._pick_quick_hint(app.input_buffer)  # Enter picks "run the tests"
    assert app.input_buffer.text == "run the tests"
    assert app.quick_hint_focus == -1  # focus is back on the input line
    app.tab_or_complete(app.input_buffer, reverse=False)  # resumes after picked chip 0 -> 1
    assert app._pick_quick_hint(app.input_buffer)  # Enter picks "show the diff"
    assert app.input_buffer.text == "run the tests\nshow the diff"
    assert app.quick_hint_picked == ["run the tests", "show the diff"]
    assert submitted == []
    assert app._accept(app.input_buffer)  # a final Enter sends
    assert [str(value) for value in submitted] == ["run the tests\nshow the diff"]

def test_quick_hint_enter_keys_pick_and_send_through_real_bindings(monkeypatch):
    """The wiring, not just the methods: Tab focuses, Enter picks and returns to the prompt, and
    only the Enter with no chip focused sends."""
    received = []
    app = None

    def submit(text):
        received.append(str(text))
        app.set_idle()

    app = TuiApp(on_chat_submit=submit, quick_hints_fn=lambda: ("run the tests", "show the diff", "commit"))
    app.set_idle()

    # Tab focuses chip 0, Enter picks it; one Tab reaches chip 1, Enter picks it; the final
    # Enter, with focus back on the input line, sends the combined text.
    run_interactive_tui(monkeypatch, app, text="\t\r\t\r\r\x04")

    assert received == ["run the tests\nshow the diff"]

def test_quick_hint_space_is_plain_through_real_bindings(monkeypatch):
    """Space no longer picks a focused chip: it reaches the buffer as an ordinary character."""
    received = []
    app = None

    def submit(text):
        received.append(str(text))
        app.set_idle()

    app = TuiApp(on_chat_submit=submit, quick_hints_fn=lambda: ("run the tests", "show the diff", "commit"))
    app.set_idle()

    # Tab focuses chip 0, but the space lands in the buffer instead of picking it.
    run_interactive_tui(monkeypatch, app, text="\t ok\r\x04")

    assert received == [" ok"]

def test_quick_hint_enter_again_unpicks_chip():
    app, _ = quick_hint_app()
    app.quick_hint_focus = 0
    app._pick_quick_hint(app.input_buffer)  # pick "run the tests", focus back on the input
    assert app.input_buffer.text == "run the tests"
    app.quick_hint_focus = 0  # cycling back to a picked chip makes Enter a toggle
    assert app._pick_quick_hint(app.input_buffer)  # Enter toggles it off
    assert app.quick_hint_picked == []
    assert app.input_buffer.text == ""

def test_quick_hint_pick_uses_one_hint_snapshot():
    states = iter((("old hint",), ()))
    app = TuiApp(quick_hints_fn=lambda: next(states))
    app.set_idle()
    app.quick_hint_focus = 0

    app._pick_quick_hint(app.input_buffer)
    assert app.input_buffer.text == "old hint"

def test_quick_hint_tab_cycles_while_picked():
    app, _ = quick_hint_app()
    app.quick_hint_focus = 0
    app._pick_quick_hint(app.input_buffer)  # pick -> focus back on the input
    for expected in (1, 2, -1, 0):
        app.tab_or_complete(app.input_buffer, reverse=False)
        assert app.quick_hint_focus == expected

def test_quick_hint_manual_edit_drops_picked_and_sends_edited_text():
    app, submitted = quick_hint_app()
    app.quick_hint_focus = 0
    app._pick_quick_hint(app.input_buffer)  # pick -> buffer = "run the tests"
    app.input_buffer.insert_text("!")
    assert app.quick_hint_picked == []
    assert app.quick_hint_focus == -1
    assert app.input_buffer.text == "run the tests!"
    app._accept(app.input_buffer)
    assert [str(value) for value in submitted] == ["run the tests!"]

def test_quick_hint_hints_change_resets_picked_keeps_text():
    hints = ["run the tests", "show the diff"]
    submitted = []
    app = TuiApp(on_chat_submit=submitted.append, quick_hints_fn=lambda: tuple(hints))
    app.set_idle()
    app.quick_hint_focus = 0
    app._pick_quick_hint(app.input_buffer)  # pick -> buffer = "run the tests"
    assert app.quick_hint_picked == ["run the tests"]
    hints[:] = ["commit", "push"]
    app.quick_hints()  # lazy comparison resets the picked state
    assert app.quick_hint_picked == []
    assert app.quick_hint_focus == -1
    assert app.input_buffer.text == "run the tests"

def test_quick_hint_tab_refreshes_hints_before_deciding_to_cycle():
    hints = ["old hint"]
    app, _ = quick_hint_app(tuple(hints))
    app.quick_hint_focus = 0
    app._pick_quick_hint(app.input_buffer)
    app.quick_hints_fn = lambda: tuple(hints)
    hints[:] = ["new hint"]

    app.tab_or_complete(app.input_buffer, reverse=False)

    assert app.quick_hint_picked == []
    assert app.quick_hint_focus == -1
    assert app.input_buffer.text == "old hint"

async def test_quick_hint_external_edit_drops_picked_state(monkeypatch):
    app, _ = quick_hint_app()
    app.quick_hint_focus = 0
    app._pick_quick_hint(app.input_buffer)

    async def edited(_text):
        return "edited text"

    monkeypatch.setattr(app, "_edit_text_in_editor", edited)
    await app._run_input_editor()

    assert app.input_buffer.text == "edited text"
    assert app.quick_hint_picked == []
    assert app.quick_hint_focus == -1

def test_quick_hint_fragments_mark_picked_chips():
    app, _ = quick_hint_app(("a", "b"))
    app.quick_hint_focus = 0
    app._pick_quick_hint(app.input_buffer)  # pick "a"; the ✓ mark survives the pick
    fragments = app.quick_hint_fragments()
    assert ("class:quickhint", " \u2713 a ") in fragments
    assert ("class:quickhint", " b ") in fragments

def test_quick_hint_fragments_highlight_focused_chip():
    app, _ = quick_hint_app(("a", "b"))
    # With no terminal width (the app is not running) chips stay on one horizontal row,
    # separated by a bar, exactly as before the wrap-aware layout.
    assert app.quick_hint_fragments() == [("class:quickhint", " a "), ("class:quickhint.sep", " \u2502 "), ("class:quickhint", " b ")]
    app.quick_hint_focus = 0
    assert ("class:quickhint.focused", " a ") in app.quick_hint_fragments()

def test_quick_hints_all_visible_at_narrow_width(monkeypatch):
    """Chips flow left to right and wrap only between chips once the row is full, so all three
    hints stay fully visible — never truncated or split mid-text — at a narrow terminal.
    Exercises the real prompt_toolkit layout/render boundary, not just quick_hint_fragments()."""
    hints = ("run the tests and check the coverage", "构建文档并同步中文 locale 目录", "commit the work with a clear message")
    app = TuiApp(quick_hints_fn=lambda: hints)
    output = ResizableOutput(rows=14, columns=30)
    frames = []
    rendered = threading.Event()

    def after_render(application):
        frames.append(rendered_screen_text(application, output))
        rendered.set()

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        assert rendered.wait(timeout=1)
        pipe_input.send_text("\x04")
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)

    assert frames, "the app rendered at least one frame"
    # A chip wider than the terminal wraps onto extra visual lines and eats the whitespace at
    # the break, so compare compact forms: one frame must show every hint's full text.
    compact_frames = ["".join(frame.split()) for frame in frames]
    assert any(all("".join(hint.split()) in screen for hint in hints) for screen in compact_frames), "no single rendered frame showed every hint"

def test_quick_hint_flow_lays_chips_horizontally_and_wraps_between_chips():
    """The wrap-aware layout keeps as many chips as fit on one row and only breaks between
    chips, never inside one; a narrow column pushes later chips to their own lines."""
    flow = TuiApp._flow_quick_hints

    # Wide enough: all three chips on one horizontal row, bar-separated, no newlines.
    wide = flow(("run", "show diff", "commit"), columns=100, focus=-1, picked=())
    assert wide == [
        ("class:quickhint", " run "),
        ("class:quickhint.sep", " \u2502 "),
        ("class:quickhint", " show diff "),
        ("class:quickhint.sep", " \u2502 "),
        ("class:quickhint", " commit "),
    ]

    # Unknown width (0) keeps the single horizontal row too, letting the window wrap as fallback.
    unknown = flow(("a", "b"), columns=0, focus=-1, picked=())
    assert "\n" not in [text for _, text in unknown]

    # Narrow: " run the tests " (15) fits alone, but " b " would not fit the remaining row,
    # so it wraps to its own line; no chip is ever split and no bar appears mid-row.
    narrow = flow(("run the tests", "b"), columns=16, focus=-1, picked=())
    lines = "".join(text for _, text in narrow)
    assert lines == " run the tests \n b "
    assert "\u2502" not in lines

    # A chip wider than the whole row still stays whole on its own line.
    lone = flow(("构建文档并同步中文 locale 目录", "x"), columns=12, focus=-1, picked=())
    lines = "".join(text for _, text in lone)
    assert lines.startswith(" 构建文档并同步中文 locale 目录 ")
    assert " \u2502 " not in lines

    # Focus and picked marks keep working through the flow layout.
    picked = flow(("a", "b"), columns=100, focus=1, picked=("a",))
    assert ("class:quickhint", " \u2713 a ") in picked
    assert ("class:quickhint.focused", " b ") in picked

    # At most MAX_QUICK_HINTS_PER_ROW chips per row even when the terminal is wide: the fourth
    # short hint starts its own line instead of crowding one row.
    four = flow(("a", "b", "c", "d"), columns=100, focus=-1, picked=())
    lines = "".join(text for _, text in four)
    assert lines == " a  \u2502  b  \u2502  c \n d "

    # Width and the per-row cap both end a row; the width check still wins on a narrow column.
    cap_and_width = flow(("a", "b", "c", "d"), columns=5, focus=-1, picked=())
    assert "".join(text for _, text in cap_and_width) == " a \n b \n c \n d "

def test_quick_hint_pick_and_send_still_work_after_wrapping(monkeypatch):
    """Chips that wrap onto multiple visual lines keep Tab focus, Enter pick/unpick, and the
    final Enter submission."""
    received = []
    app = None

    def submit(text):
        received.append(str(text))
        app.set_idle()

    app = TuiApp(
        on_chat_submit=submit,
        quick_hints_fn=lambda: ("run the tests and check the coverage", "构建文档并同步中文 locale 目录", "commit the work"),
    )
    app.set_idle()

    # Tab focuses chip 0, Enter picks it; one Tab reaches chip 1, Enter picks it; the final
    # Enter, with focus back on the input line, sends the combined text.
    run_interactive_tui(monkeypatch, app, text="\t\r\t\r\r\x04")

    assert received == ["run the tests and check the coverage\n构建文档并同步中文 locale 目录"]

LONG_HINT = "run the full test suite, then check the coverage report, and commit only what passed"
SECOND_LONG = "review the diff hunk by hunk and write down anything you are still unsure about"


def test_quick_hint_row_shortens_a_long_chip_but_the_pick_keeps_it_whole():
    """The row below the input stays a row; what `Enter` puts in the input is the suggestion the
    user chose, never the display form of it."""
    assert len(LONG_HINT) > TuiApp.QUICK_HINT_MAX_CHARS
    app, submitted = quick_hint_app((LONG_HINT, "show the diff"))
    label = oneline(LONG_HINT, TuiApp.QUICK_HINT_MAX_CHARS)
    assert label.endswith("...") and len(label) <= TuiApp.QUICK_HINT_MAX_CHARS
    assert ("class:quickhint", f" {label} ") in app.quick_hint_fragments()

    app.quick_hint_focus = 0
    assert app._pick_quick_hint(app.input_buffer)
    assert app.input_buffer.text == LONG_HINT
    assert app.quick_hints() == (LONG_HINT, "show the diff")  # the session keeps it whole
    assert ("class:quickhint", f" \u2713 {label} ") in app.quick_hint_fragments()

    # The input still agrees with its picks, which is what makes Tab and Enter decide from one
    # snapshot; a truncated pick would have broken this the moment it was inserted.
    assert app._live_quick_hints(app.input_buffer) == (LONG_HINT, "show the diff")
    app.tab_or_complete(app.input_buffer, reverse=False)  # Tab resumes after the picked chip
    assert app.quick_hint_focus == 1
    assert submitted == []


def test_quick_hint_chip_shortens_only_past_the_cap():
    """Boundary: a suggestion exactly as long as the cap is drawn whole; one more character is not."""
    cap = TuiApp.QUICK_HINT_MAX_CHARS
    exact = "x" * cap
    over = "y" * (cap + 1)

    lines = "".join(text for _, text in TuiApp._flow_quick_hints((exact, over), columns=0, focus=-1, picked=()))

    assert f" {exact} " in lines
    assert f" {'y' * (cap - 3)}... " in lines
    assert "y" * (cap - 2) not in lines  # the tail is dropped from the row only


def test_quick_hint_flow_shortens_only_the_long_chip():
    parts = TuiApp._flow_quick_hints(("show the diff", LONG_HINT), columns=0, focus=-1, picked=())
    assert parts == [
        ("class:quickhint", " show the diff "),
        ("class:quickhint.sep", " \u2502 "),
        ("class:quickhint", f" {oneline(LONG_HINT, TuiApp.QUICK_HINT_MAX_CHARS)} "),
    ]


def test_quick_hint_long_suggestions_combine_and_send_whole():
    app, submitted = quick_hint_app((LONG_HINT, SECOND_LONG))
    app.quick_hint_focus = 0
    assert app._pick_quick_hint(app.input_buffer)
    app.tab_or_complete(app.input_buffer, reverse=False)
    assert app._pick_quick_hint(app.input_buffer)

    assert app.input_buffer.text == f"{LONG_HINT}\n{SECOND_LONG}"
    assert app.quick_hint_picked == [LONG_HINT, SECOND_LONG]
    assert app._accept(app.input_buffer)
    assert [str(value) for value in submitted] == [f"{LONG_HINT}\n{SECOND_LONG}"]


def test_quick_hint_unpicking_a_long_chip_matches_the_whole_text():
    app, _ = quick_hint_app((LONG_HINT,))
    app.quick_hint_focus = 0
    assert app._pick_quick_hint(app.input_buffer)
    assert app.input_buffer.text == LONG_HINT

    app.quick_hint_focus = 0
    assert app._pick_quick_hint(app.input_buffer)  # the picker finds it by the whole suggestion
    assert app.input_buffer.text == ""
    assert app.quick_hint_picked == []


def test_quick_hint_long_suggestions_send_whole_through_real_bindings(monkeypatch):
    received = []
    app = None

    def submit(text):
        received.append(str(text))
        app.set_idle()

    app = TuiApp(on_chat_submit=submit, quick_hints_fn=lambda: (LONG_HINT, SECOND_LONG))
    app.set_idle()

    run_interactive_tui(monkeypatch, app, text="\t\r\t\r\r\x04")

    assert received == [f"{LONG_HINT}\n{SECOND_LONG}"]


def test_quick_hint_long_suggestions_stay_pickable_at_narrow_width(monkeypatch):
    """At a narrow terminal the row wraps, and a shortened chip may even spill across lines; every
    suggestion still reaches the picker and the submission is still the whole text."""
    received = []
    app = None
    frames = []

    def submit(text):
        received.append(str(text))
        app.set_idle()

    app = TuiApp(on_chat_submit=submit, quick_hints_fn=lambda: (LONG_HINT, SECOND_LONG))
    app.set_idle()
    output = ResizableOutput(rows=14, columns=30)

    run_interactive_tui(
        monkeypatch,
        app,
        text="\t\r\t\r\r\x04",
        output=output,
        after_render=lambda application: frames.append(rendered_screen_text(application, output)),
    )

    assert received == [f"{LONG_HINT}\n{SECOND_LONG}"]
    # Wrapping eats the whitespace at a break, so compare compact forms: the second chip's drawn
    # label reached the screen, shortened rather than grown.
    drawn = "".join("".join(frame.split()) for frame in frames)
    assert "".join(oneline(SECOND_LONG, TuiApp.QUICK_HINT_MAX_CHARS).split()) in drawn


def test_quick_hint_two_suggestions_may_share_one_drawn_label():
    """The one visible consequence of shortening on screen only: suggestions that agree for their
    first forty-five characters look alike on the row, and each still picks its own whole text."""
    prefix = "check the whole diff and the test output before deciding what to do next about it"
    first = prefix + " today"
    second = prefix + " tomorrow"
    assert oneline(first, TuiApp.QUICK_HINT_MAX_CHARS) == oneline(second, TuiApp.QUICK_HINT_MAX_CHARS)

    app, _ = quick_hint_app((first, second))
    drawn = [text for _, text in app.quick_hint_fragments()]
    assert len(drawn) == 3 and drawn[0] == drawn[2]
    app.quick_hint_focus = 1
    assert app._pick_quick_hint(app.input_buffer)
    assert app.input_buffer.text == second


def test_quick_hint_cap_counts_characters_not_cells():
    """Wide text is cut by characters, exactly as the tool used to cut it; the pick is whole."""
    wide = (
        "\u8fd0\u884c\u5b8c\u6574\u7684\u6d4b\u8bd5\u5957\u4ef6\u5e76\u68c0\u67e5\u8986\u76d6\u7387\u62a5\u544a\u7136\u540e\u518d\u63d0\u4ea4\u5df2\u7ecf\u901a\u8fc7\u7684\u5168\u90e8\u6539\u52a8"
        "\u8fd0\u884c\u5b8c\u6574\u7684\u6d4b\u8bd5\u5957\u4ef6\u5e76\u68c0\u67e5\u8986\u76d6\u7387\u62a5\u544a\u7136\u540e\u518d\u63d0\u4ea4\u5df2\u7ecf\u901a\u8fc7\u7684\u5168\u90e8\u6539\u52a8"
    )
    label = oneline(wide, TuiApp.QUICK_HINT_MAX_CHARS)
    assert len(label) <= TuiApp.QUICK_HINT_MAX_CHARS and label.endswith("...")
    assert "".join(text for _, text in TuiApp._flow_quick_hints((wide,), columns=0, focus=-1, picked=())) == f" {label} "

    app, _ = quick_hint_app((wide,))
    app.quick_hint_focus = 0
    assert app._pick_quick_hint(app.input_buffer)
    assert app.input_buffer.text == wide


def test_quick_hint_suggestion_rides_the_tool_and_the_row_into_the_input(tmp_path):
    """The whole chain in one test: the tool stores the suggestion whole, the row draws it
    shortened, and `Enter` puts the suggestion itself in the input."""
    command_loop = loop(tmp_path)
    long = "run the full test suite, then check the coverage report, and commit only what passed"
    NextHintsTool(command_loop.session, [{"inputs": [long]}]).call()
    app = TuiApp(quick_hints_fn=lambda: command_loop.session.quick_hints)
    app.set_idle()

    assert app.quick_hints() == (long,)
    assert ("class:quickhint", f" {oneline(long, TuiApp.QUICK_HINT_MAX_CHARS)} ") in app.quick_hint_fragments()
    app.quick_hint_focus = 0
    assert app._pick_quick_hint(app.input_buffer)
    assert app.input_buffer.text == long


def test_quick_hint_placeholder_hints_keys_until_focused():
    app, _ = quick_hint_app()
    assert app.placeholder_text() == "Tab cycles suggestions \u00b7 Enter picks \u00b7 Enter sends"
    app.quick_hint_focus = 0
    assert app.placeholder_text() == ""

def test_quick_hint_pick_ignored_when_input_was_edited():
    app, _ = quick_hint_app()
    app.quick_hint_focus = 0
    app.input_buffer.insert_text("hello")
    assert app._pick_quick_hint(app.input_buffer) is False
    assert app.quick_hint_picked == []
    assert app.quick_hint_focus == 0  # unchanged, so Enter falls through to sending

def test_quick_hint_enter_without_focus_sends():
    """Enter with no focused chip sends; it never unpicks picked text."""
    app, submitted = quick_hint_app()
    app.quick_hint_focus = 0
    app._pick_quick_hint(app.input_buffer)  # pick -> buffer = "run the tests"
    assert app.quick_hint_focus == -1
    assert app._accept(app.input_buffer) is True  # Enter sends, it never unpicks
    assert [str(value) for value in submitted] == ["run the tests"]
    assert app.quick_hint_focus == -1  # sending clears the quick-hint state

def test_quick_hint_placeholder_falls_back_without_hints():
    app, _ = quick_hint_app(())
    assert app.placeholder_text() == app.input_hint_fn()

def test_quick_hint_mode_change_resets_focus_and_picked():
    app, _ = quick_hint_app()
    app.quick_hint_focus = 2
    app.quick_hint_picked = ["run the tests"]
    app.set_running("working")
    assert app.quick_hint_focus == -1
    assert app.quick_hint_picked == []
