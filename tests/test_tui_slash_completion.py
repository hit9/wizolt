"""TUI slash-command completion: a leading "/" opens the command list as it is typed."""

import pytest
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document
from tui_harness import ResizableOutput, rendered_screen_text, run_interactive_tui, wait_until

from wizolt.cli import CommandCompleter, CommandLoop
from wizolt.tui import TuiApp
from wizolt.tui.app import InputMode


def _completions(app):
    state = app.input_buffer.complete_state
    return None if state is None else [c.text for c in state.completions]


def test_completion_menu_anchors_at_the_replaced_word():
    app = TuiApp(completer=CommandCompleter())

    for text, start, expected in (("/", -1, 0), ("/provider ope", -3, len("/provider ")), ("use @ski", -4, len("use "))):
        document = Document(text, cursor_position=len(text))
        app.input_buffer.complete_state = CompletionState(document, [Completion("candidate", start_position=start)])
        assert app._completion_menu_position() == expected


@pytest.mark.parametrize("mode", [InputMode.CHAT, InputMode.RUNNING])
def test_ctrl_n_and_ctrl_p_move_through_an_open_menu(monkeypatch, mode):
    """Readline's Ctrl-N/Ctrl-P walk the menu in every mode. While a turn runs, Ctrl-P otherwise
    recalls the queued follow-up; an open menu takes it first, so the draft is never replaced."""
    app = TuiApp(completer=CommandCompleter())
    app.on_recall = lambda: "queued follow-up"

    def index():
        state = app.input_buffer.complete_state
        return None if state is None else state.complete_index

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        app.input_mode = mode
        pipe_input.send_text("/")
        wait_until(lambda: app.input_buffer.complete_state is not None)
        pipe_input.send_text("\x0e\x0e")  # Ctrl-N twice
        wait_until(lambda: index() == 1)
        pipe_input.send_text("\x10")  # Ctrl-P
        wait_until(lambda: index() == 0)
        assert app.input_buffer.text == "/help"
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_completion_menu_closes_on_a_key_hint_row(monkeypatch):
    """Every dropdown -- mentions as much as commands -- names its keys under the last row, the
    way the @file: picker's header does."""
    app = TuiApp(completer=CommandCompleter(mcp_servers=lambda: ("github", "gitlab")))
    output = ResizableOutput(rows=20, columns=60)
    frames = []

    def after_render(application):
        frames.append(rendered_screen_text(application, output))

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("@mcp:")
        wait_until(lambda: any("@mcp:gitlab" in frame for frame in frames))
        lines = next(frame for frame in reversed(frames) if "@mcp:gitlab" in frame).splitlines()
        row = next(index for index, line in enumerate(lines) if "@mcp:gitlab" in line)
        assert lines[row + 1].strip() == "Ctrl-N/P or ↑/↓ move · Enter select"
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)


def test_leading_slash_and_command_rows_render_in_the_same_column(monkeypatch):
    app = TuiApp(completer=CommandCompleter())
    output = ResizableOutput(rows=20, columns=60)
    frames = []

    def after_render(application):
        frames.append(rendered_screen_text(application, output))

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("/")
        wait_until(lambda: any("/help" in frame for frame in frames))
        frame = next(frame for frame in reversed(frames) if "/help" in frame)
        lines = frame.splitlines()
        input_line = next(line for line in lines if line.startswith("> "))
        help_line = next(line for line in lines if "/help" in line)
        assert help_line.index("/help") == input_line.index("/")
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)


def test_leading_slash_opens_command_completions_and_narrows_while_typing(monkeypatch):
    """A "/" at the start of the line names a command, so the list opens on the first character,
    without Tab, and narrows as more characters arrive -- matching how @/$ mentions behave."""
    app = TuiApp(completer=CommandCompleter())

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)

        pipe_input.send_text("/")
        wait_until(lambda: _completions(app) == list(CommandLoop.COMMANDS))

        # Narrowing happens at a partial match; completing the full word adds nothing, so
        # prompt-toolkit then closes the menu on its own.
        pipe_input.send_text("reas")
        wait_until(lambda: _completions(app) == ["/reason"])

        # A word after the command stops completion: the line is no longer a bare command name.
        pipe_input.send_text(" on")
        wait_until(lambda: app.input_buffer.text == "/reas on")
        wait_until(lambda: _completions(app) is None)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_tab_still_browses_an_auto_opened_command_menu(monkeypatch):
    """Tab keeps its role on a menu the first "/" opened: it highlights a row to pick or commit."""
    app = TuiApp(completer=CommandCompleter())

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)

        pipe_input.send_text("/")
        wait_until(lambda: _completions(app) is not None)
        pipe_input.send_text("\t")
        wait_until(lambda: app.input_buffer.complete_state is not None and app.input_buffer.complete_state.complete_index == 0)

        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_fast_slash_typing_keeps_a_current_menu_ready_for_tab():
    """Every keystroke replaces the menu in the same edit, so `/`, `p`, Tab cannot hit a delayed
    refresh that briefly closes or resets it."""
    app = TuiApp(completer=CommandCompleter())

    app.input_buffer.insert_text("/")
    assert _completions(app) == list(CommandLoop.COMMANDS)

    app.input_buffer.insert_text("p")
    assert _completions(app) == ["/ps", "/provider"]
    assert app._mention_transition_timer is None

    app.tab_or_complete(app.input_buffer, reverse=False)
    assert app.input_buffer.complete_state is not None
    assert app.input_buffer.complete_state.complete_index == 0


def test_running_tab_completes_when_the_menu_is_open():
    """An open menu owns Tab: a working prompt must not swallow the completion into the
    next-turn queue."""
    held: list[str] = []
    app = TuiApp(completer=CommandCompleter(), on_queue_next_turn=held.append)
    app.set_running("working")
    app.input_buffer.insert_text("/")
    assert app.input_buffer.complete_state is not None

    app.tab_or_complete(app.input_buffer, reverse=False)

    assert held == []
    assert app.input_buffer.text.startswith("/")


def test_running_tab_opens_the_argument_menu_for_a_slash_command(monkeypatch):
    """`/mcp ` closes the menu as it is typed, so the next Tab has to reopen it rather than hold
    the half-typed command for the next turn. Driven through the app because opening the menu
    schedules the completer on the running loop."""
    held: list[str] = []
    app = TuiApp(completer=CommandCompleter(), on_queue_next_turn=held.append)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        assert app.app is not None
        app.app.loop.call_soon_threadsafe(app.set_running, "working")
        pipe_input.send_text("/mcp ")
        wait_until(lambda: app.input_buffer.text == "/mcp ")
        pipe_input.send_text("\t")
        wait_until(lambda: app.input_buffer.complete_state is not None)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)

    assert held == []


def test_running_tab_holds_a_draft_whose_first_line_looks_like_a_path():
    """The slash guard reads the current line, so a multi-line draft that merely starts with a
    path still holds for the next turn."""
    held: list[str] = []
    app = TuiApp(completer=CommandCompleter(), on_queue_next_turn=held.append)
    app.set_running("working")
    app.input_buffer.insert_text("/usr/local/lib\n这个目录看下")

    app.tab_or_complete(app.input_buffer, reverse=False)

    assert held == ["/usr/local/lib\n这个目录看下"]


def test_complete_slash_command_submits_on_the_first_enter(monkeypatch):
    received = []
    app = TuiApp(completer=CommandCompleter(), on_chat_submit=received.append)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("/effort")
        wait_until(lambda: app.input_buffer.text == "/effort")
        assert app.input_buffer.complete_state is None

        pipe_input.send_text("\r")
        wait_until(lambda: received == ["/effort"])
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)
