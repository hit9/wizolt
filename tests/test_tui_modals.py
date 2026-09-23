"""tui modals (split from tests/test_tui_app.py)."""

import asyncio
import multiprocessing
import os
import shutil
import threading

import pytest
from prompt_toolkit.data_structures import Size
from test_tui_input import ctrl_c_queue_scenario
from tui_harness import ResizableOutput, loop, rendered_screen_text, request_input_from_driver, run_interactive_tui, show_modal_from_driver, wait_until

from wizolt.base import SELECTION_BACK
from wizolt.cli.commands import select_choice
from wizolt.cli.modals import choice_application
from wizolt.prompts import LIVE_FOLLOWUP_PREFIX
from wizolt.tui import TUI_MODAL_PENDING, TuiApp


def test_interactive_tui_modal_uses_real_j_and_enter_keys(monkeypatch):
    app = TuiApp()
    selected = {"index": 0}
    result = []

    def key(key, _data):
        if key == "j":
            selected["index"] = 1
            return TUI_MODAL_PENDING
        if key == "enter":
            return selected["index"]
        return TUI_MODAL_PENDING

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        modal = show_modal_from_driver(app, lambda: [("", "one\ntwo")], key)
        wait_until(lambda: app.modal is not None)
        pipe_input.send_text("j\r")
        result.append(modal.result(timeout=2))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)

    assert result == [1]


@pytest.mark.parametrize("exclusive", [False, True])
def test_interactive_tui_modal_survives_repeated_resize(monkeypatch, exclusive):
    app = TuiApp()
    output = ResizableOutput()
    result = []
    rendered = threading.Event()

    def fragments():
        return [("", "\n".join(f"choice {index}" for index in range(40)))]

    def key(key, _data):
        return None if key == "q" else TUI_MODAL_PENDING

    def after_render(_application):
        rendered.set()

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        modal = show_modal_from_driver(app, fragments, key, exclusive=exclusive)
        wait_until(lambda: app.modal is not None)
        for rows, columns in ((10, 40), (35, 120), (8, 24), (24, 80)):
            rendered.clear()
            output.size = Size(rows=rows, columns=columns)
            app.app.loop.call_soon_threadsafe(app.app._on_resize)
            assert rendered.wait(timeout=1)
        pipe_input.send_text("q")
        result.append(modal.result(timeout=2))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)

    assert result == [None]


def test_interactive_tui_renders_a_multi_line_question_as_rows_not_control_characters(monkeypatch):
    """The whole point of the split, on a real screen: every line of a multi-line Ask question is
    visible and no "^J" is drawn."""
    app = TuiApp()
    output = ResizableOutput(rows=12, columns=60)
    frames = []
    rendered = threading.Event()

    def after_render(application):
        text = rendered_screen_text(application, output)
        if "B) bar" in text:
            frames.append(text)
            rendered.set()

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        asking = request_input_from_driver(app, "\nWhich one?\nA) foo\nB) bar")
        wait_until(lambda: app.input_mode == "approval")
        assert rendered.wait(timeout=1)
        pipe_input.send_text("x\r")
        asking.result(timeout=2)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)

    assert "^J" not in frames[-1]
    for line in ("Which one?", "A) foo", "B) bar"):
        assert line in frames[-1]


@pytest.mark.parametrize("exclusive", [False, True])
def test_interactive_tui_modal_presentation_matches_legacy_scope(monkeypatch, exclusive):
    app = TuiApp(status_fragments_fn=lambda: [("", "status marker")])
    output = ResizableOutput(rows=12, columns=60)
    frames = []
    rendered = threading.Event()

    def after_render(application):
        text = rendered_screen_text(application, output)
        if "modal marker" in text:
            frames.append(text)
            rendered.set()

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        modal = show_modal_from_driver(app, lambda: [("", "modal marker")], lambda key, _data: None if key == "q" else TUI_MODAL_PENDING, exclusive=exclusive)
        wait_until(lambda: app.modal is not None)
        assert rendered.wait(timeout=1)
        pipe_input.send_text("q")
        modal.result(timeout=2)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)

    assert "status marker" in frames[-1]
    if exclusive:
        assert frames[-1].splitlines()[-1] == "status marker"
    else:
        lines = frames[-1].splitlines()
        modal = lines.index("modal marker")
        status = lines.index("status marker")
        assert lines[modal - 1] == ""
        assert lines[modal + 1 : status] == [""]


def test_interactive_command_loop_ctrl_c_stops_llm_and_returns_to_input(tmp_path):
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(target=ctrl_c_queue_scenario, args=(str(tmp_path), results))
    process.start()
    process.join(timeout=6)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
        pytest.fail("Ctrl-C TUI scenario did not exit within 6 seconds")

    assert process.exitcode == 0
    outcome = results.get(timeout=1)
    assert "fatal" not in outcome, outcome
    assert outcome["driver_errors"] == []
    assert outcome["return_code"] == 0
    assert outcome["elapsed"] and outcome["elapsed"][0] < 1.0
    assert outcome["cancel_calls"] == 1
    assert "long request" in outcome["requests"][0]
    queued_request = outcome["requests"][1]
    assert "queued one" in queued_request
    marked_followup = LIVE_FOLLOWUP_PREFIX + "queued two"
    assert marked_followup in queued_request
    assert queued_request.index("queued one") < queued_request.index(marked_followup)
    assert outcome["draft_after_ctrl_c"] == [""]
    # The interrupted first turn produced no output, so it is retracted: "long request" leaves no
    # trace in the persisted conversation, while the queued follow-ups become the next turn.
    # The follow-up keeps the marker it was sent with; only the transcript hides it.
    assert outcome["persisted_user_inputs"] == ["queued one", marked_followup]
    assert outcome["restored_queue"] == []


def test_interactive_tui_ctrl_c_closes_modal_and_restores_input_focus(monkeypatch):
    received = []
    app = None

    def submit(text):
        received.append(text)
        app.app.exit()

    app = TuiApp(on_chat_submit=submit)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        modal = show_modal_from_driver(app, lambda: [("", "selector")], lambda _key, _data: TUI_MODAL_PENDING)
        wait_until(lambda: app.modal is not None)
        pipe_input.send_text("\x03")
        modal.result(timeout=2)
        app.set_idle()
        wait_until(lambda: app.modal is None and app.app.layout.current_window is app.input_window)
        pipe_input.send_text("after cancel\r")

    run_interactive_tui(monkeypatch, app, drive=drive)

    assert received == ["after cancel"]


def test_interactive_tui_resolved_modal_allows_followup_approval(monkeypatch):
    app = TuiApp()
    selected = []
    approved = []

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        selector = show_modal_from_driver(app, lambda: [("", "selector")], lambda key, _data: "chosen" if key == "enter" else TUI_MODAL_PENDING)
        wait_until(lambda: app.modal is not None)
        pipe_input.send_text("\r")
        selected.append(selector.result(timeout=2))
        wait_until(lambda: app.modal is None and app.app.layout.current_window is app.input_window)

        approval = request_input_from_driver(app)
        wait_until(lambda: app.input_mode == "approval")
        pipe_input.send_text("y\r")
        approved.append(approval.result(timeout=2))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)

    assert selected == ["chosen"]
    assert approved == ["y"]


@pytest.mark.parametrize(
    ("rows", "preview"),
    # Fourteen rows leave the modal six: enough for a plain picker's fixed rows and one of list, not
    # for a three-line preview on top of them.
    [(14, False), (20, False), (24, False), (40, False), (20, True), (24, True), (40, True)],
)
def test_a_long_picker_keeps_its_key_legend_on_screen(monkeypatch, tmp_path, rows, preview):
    """A provider can discover dozens of models. Drawn whole, the /model list outgrew the modal
    region and its last rows -- the key legend among them -- fell off the bottom. The list scrolls
    inside the space instead, so the legend and the counter stay visible wherever the cursor is,
    and Tab walks the list like j. A preview (the /reason "why these levels" footer) takes its
    rows from the list, not from the legend."""
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, rows)))
    command_loop = loop(tmp_path)
    command_loop.interactive_input = True
    app = TuiApp()
    command_loop.tui = app
    output = ResizableOutput(rows=rows, columns=80)
    frames = []
    labels = ("--- Configured ---", "--- Discovered ---")
    choices = (labels[0], *(f"model-{index}" for index in range(4)), labels[1], *(f"remote-{index}" for index in range(40)))
    legend = "j/k/Tab move · Ctrl-D/U page · / search · Esc/q back/cancel"
    result = []

    def after_render(application):
        frames.append(rendered_screen_text(application, output))

    def screen_shows(*texts):
        return bool(frames) and all(text in frames[-1] for text in texts)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        preview_fn = (lambda _choice: "Why these levels\\nthe provider's documented scale\\nsource: compat policy") if preview else None
        tail = ("source: compat policy",) if preview else ()
        selector = asyncio.run_coroutine_threadsafe(select_choice(command_loop, "Model", choices, disabled=set(labels), preview_fn=preview_fn), app.app.loop)
        wait_until(lambda: screen_shows("model-0", legend, *tail))
        pipe_input.send_text("\t")  # Tab moves like j
        wait_until(lambda: screen_shows(legend, "showing", *tail))
        pipe_input.send_text("G")  # to the far end of the list
        wait_until(lambda: screen_shows("remote-39", legend, "of 46", *tail))
        pipe_input.send_text("\r")
        result.append(selector.result(timeout=2))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)

    assert result == ["remote-39"]


def test_ctrl_n_and_ctrl_p_reach_a_picker_and_never_its_search(monkeypatch, tmp_path):
    """The TUI hands Ctrl-N/P to an open modal by name. Before, they fell through to the catch-all
    key: they did not move, and while searching the raw control character was typed into the
    query."""
    command_loop = loop(tmp_path)
    command_loop.interactive_input = True
    app = TuiApp()
    command_loop.tui = app
    result = []

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        selector = asyncio.run_coroutine_threadsafe(select_choice(command_loop, "Pick", ("alpha", "beta", "gamma")), app.app.loop)
        wait_until(lambda: app.modal is not None)
        pipe_input.send_text("/")  # search: a control character here used to become query text
        pipe_input.send_text("\x0e")
        pipe_input.send_text("\r")  # leave the search with an empty query
        pipe_input.send_text("\x0e\x0e\x10\r")  # Ctrl-N twice, Ctrl-P once: the second row
        result.append(selector.result(timeout=2))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)

    assert result == ["beta"]


@pytest.mark.parametrize("rows", [24, 30, 40])
def test_an_exclusive_picker_with_a_tall_preview_keeps_its_legend(monkeypatch, tmp_path, rows):
    """The /sessions picker owns the screen and previews a session's recent messages, often a dozen
    lines. The list gives up rows to that preview rather than the preview's newest lines and the key
    legend falling off the bottom. (This fourteen-line preview and the fixed rows need twenty rows
    before the list gets one, so smaller screens are out of reach.)"""
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, rows)))
    command_loop = loop(tmp_path)
    command_loop.interactive_input = True
    app = TuiApp()
    command_loop.tui = app
    output = ResizableOutput(rows=rows, columns=80)
    frames = []
    choices = tuple(f"session-{index}" for index in range(30))
    preview = "\\n".join(f"message {index}" for index in range(12)) + "\\nnewest message"
    legend = "j/k/Tab move · Ctrl-D/U page · / search · Esc/q back/cancel"
    result = []

    def after_render(application):
        frames.append(rendered_screen_text(application, output))

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        selector = asyncio.run_coroutine_threadsafe(
            choice_application(command_loop, "Sessions", choices, {}, "", set(), preview_fn=lambda _uid: preview, exclusive=True, max_rows=max(5, min(20, rows - 12))),
            app.app.loop,
        )
        wait_until(lambda: bool(frames) and all(text in frames[-1] for text in ("session-0", "newest message", legend, "showing")))
        pipe_input.send_text("q")
        result.append(selector.result(timeout=2))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)

    assert result == [SELECTION_BACK]


def test_interactive_tui_choice_ctrl_c_reports_cancellation(monkeypatch, tmp_path):
    command_loop = loop(tmp_path)
    command_loop.interactive_input = True
    output = []
    command_loop.emit = lambda text="", indent=0: output.append(text)
    app = TuiApp()
    command_loop.tui = app
    result = []

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        selector = asyncio.run_coroutine_threadsafe(select_choice(command_loop, "Pick", ("a", "b")), app.app.loop)
        wait_until(lambda: app.modal is not None)
        pipe_input.send_text("\x03")
        result.append(selector.result(timeout=1))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)

    assert result == [None]
    assert output == ["Cancelled"]
