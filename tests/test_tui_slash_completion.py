"""TUI slash-command completion: a leading "/" opens the command list as it is typed."""

import time

import pytest
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Char, Screen, WritePosition
from tui_harness import ResizableOutput, loop, rendered_screen_text, request_input_from_driver, run_interactive_tui, wait_until

from wizolt.cli import CommandCompleter, CommandLoop
from wizolt.cli.commands import SET_KEYS, set_value
from wizolt.cli.worker import WORKER_SUBCOMMANDS
from wizolt.config import PROVIDER_API_CHOICES
from wizolt.tui import TuiApp
from wizolt.tui.app import InputMode, _AlignedCompletionsMenu, default_completion


def _command_rows():
    """Every command as the menu lists it: one that does nothing bare carries a trailing space."""
    return [name + " " if name in CommandLoop.NEEDS_ARGUMENT else name for name in CommandLoop.COMMANDS]


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
        state = app.input_buffer.complete_state
        pipe_input.send_text("\x0e\x0e")  # Ctrl-N twice: past the default first row, then on
        wait_until(lambda: index() == 2)
        pipe_input.send_text("\x10")  # Ctrl-P
        wait_until(lambda: index() == 1)
        assert app.input_buffer.text == state.completions[1].text
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
        assert lines[row + 1].strip() == "Ctrl-N/P or ↑/↓ move · Enter select · Esc close"
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output, after_render=after_render)


def test_typing_a_command_highlights_its_first_match_for_enter(monkeypatch):
    """`/st` and Enter used to send `/st` for "Unknown command". While typing, the menu's first
    row is drawn in the selection band, and Enter runs that command at once -- a command name is
    the whole input, so there is nothing left to type after it."""
    submitted = []
    app = TuiApp(completer=CommandCompleter(), on_chat_submit=submitted.append)
    output = ResizableOutput(rows=20, columns=60)
    band = []

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("/st")
        wait_until(lambda: default_completion(app.input_buffer.complete_state) is not None)
        first = app.input_buffer.complete_state.completions[0].text
        wait_until(lambda: first in rendered_screen_text(app.app, output) and app.input_buffer.text == "/st")
        # The first row is drawn exactly as a selected row is, and it is the only such row.
        screen = app.app.renderer.last_rendered_screen
        rows = rendered_screen_text(app.app, output).splitlines()
        band.extend(
            index
            for index, line in enumerate(rows)
            if line.strip() and all("completion-menu.completion.current" in screen.data_buffer[index][column].style for column in range(line.index(line.strip()[0]), len(line)))
        )
        assert [rows[index].strip() for index in band] == [first]
        pipe_input.send_text("\r")
        wait_until(lambda: submitted == [first])
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output)


@pytest.mark.parametrize("name", CommandLoop.COMMANDS)
def test_every_command_row_says_whether_enter_runs_it(name):
    """Each command's menu row decides what Enter does: a plain name runs, a name with a trailing
    space fills in and opens its arguments. Only a command that does nothing bare may carry the
    space -- anything else would stop Enter from running a command that works on its own."""
    (row,) = [c.text for c in CommandCompleter().get_completions(Document(name, len(name)), CompleteEvent()) if c.text.rstrip() == name]
    assert row == (name + " " if name in CommandLoop.NEEDS_ARGUMENT else name)


def test_only_set_needs_an_argument_and_bare_it_prints_its_usage(tmp_path):
    """The one command marked as needing an argument really does nothing without one. Every other
    command works bare (a picker, a status, a toggle), so its row runs on Enter."""
    assert CommandLoop.NEEDS_ARGUMENT == {"/set"}
    assert set_value(loop(tmp_path), "") == "Usage: /set KEY VALUE"


@pytest.mark.parametrize(
    ("text", "marked", "plain"),
    [
        # Every `/set` key takes a value; the values end the command.
        ("/set ", set(SET_KEYS), set()),
        ("/set provider.temperature ", set(), {"off"}),
        # `/mcp connect` and `disconnect` take a server; `tools` works bare.
        ("/mcp ", {"connect", "disconnect"}, {"tools"}),
        ("/mcp connect ", set(), {"github"}),
        ("/mcp disconnect ", set(), {"github"}),
        ("/catalog ", set(), {"status", "sync"}),
        ("/strict ", set(), {"on", "off"}),
        ("/compact ", set(), {"log"}),
        ("/api ", set(), set(PROVIDER_API_CHOICES)),
        ("/model ", set(), {"model-a"}),
        ("/provider ", set(), {"prov-a"}),
        # Every `/worker` subcommand opens its own picker bare, so none is held back.
        ("/worker ", set(), set(WORKER_SUBCOMMANDS)),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_argument_row_says_whether_enter_runs_it(text, marked, plain):
    """Arguments follow the same rule as command names: a row that needs something after it ends
    in a space (Enter opens the next level), and a final one does not (Enter runs the command)."""
    completer = CommandCompleter(mcp_servers=lambda: ("github",), models=lambda: ("model-a",), providers=lambda: ("prov-a",))
    rows = [c.text for c in completer.get_completions(Document(text, len(text)), CompleteEvent())]
    assert {row[:-1] for row in rows if row.endswith(" ")} == marked
    assert {row for row in rows if not row.endswith(" ")} == plain


@pytest.mark.parametrize("keys", [["\r"], ["\x1b[B", "\r"], ["\t", "\r"]], ids=["default", "down", "tab"])
def test_enter_runs_a_highlighted_command(monkeypatch, keys):
    """However a command row is highlighted -- the default, Down, or Tab -- Enter runs it in one
    press: a command is the whole input, not a chip inside a sentence."""
    submitted = []
    app = TuiApp(completer=CommandCompleter(), on_chat_submit=submitted.append)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("/st")
        wait_until(lambda: default_completion(app.input_buffer.complete_state) is not None)
        completions = [c.text for c in app.input_buffer.complete_state.completions]
        for key in keys:
            pipe_input.send_text(key)
            time.sleep(0.05)
        expected = completions[1] if keys[0] == "\x1b[B" else completions[0]
        wait_until(lambda: submitted == [expected])
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize(
    ("keys", "sent", "text", "menu"),
    [
        # A command that does nothing bare fills in and opens its arguments instead of running.
        (["/set", "\r"], None, "/set ", "provider.temperature "),
        # ...and so does each step after it, until the last one runs the whole command.
        (["/set", "\r", "\r", "\r"], "/set provider.temperature off", "", None),
        # A value with nothing to list leaves the cursor waiting after the space.
        (["/set provider.max_t", "\r"], None, "/set provider.max_tokens ", None),
        # Tab onto the only candidate of a step opens the next level too.
        (["/mcp co", "\t", "\r"], "/mcp connect github", "", None),
        # A final argument runs the command.
        (["/catalog s", "\r"], "/catalog status", "", None),
        # An optional argument does not hold a command back: bare, it opens its own picker.
        (["/mo", "\r"], "/model", "", None),
    ],
    ids=["set", "set-to-the-end", "free-form-value", "tab-single-step", "final-argument", "optional-argument"],
)
def test_a_command_that_needs_more_opens_its_next_level(monkeypatch, keys, sent, text, menu):
    """Running `/set` or `/set provider.max_tokens` half-typed would only print the usage. A row
    that needs something after it completes with a trailing space, and Enter on it opens the next
    level (or waits for typing when there is nothing to list) instead of running the command."""
    submitted = []
    app = TuiApp(completer=CommandCompleter(mcp_servers=lambda: ("github",)), on_chat_submit=submitted.append)

    def first_row():
        state = app.input_buffer.complete_state
        return state.completions[0].text if state is not None and state.completions else None

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        for key in keys:
            pipe_input.send_text(key)
            time.sleep(0.2)
        if sent is None:
            wait_until(lambda: app.input_buffer.text == text and first_row() == menu)
            assert submitted == []
        else:
            wait_until(lambda: submitted == [sent])
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize(
    ("typed", "deletes", "expected"),
    [
        # A fully typed command has no menu; Backspace brings back the commands it narrows to.
        ("/ps", 1, ["/ps", "/provider"]),
        ("/ps", 2, None),  # `/` alone: every command, checked below by count
        ("use @skill:rele", 3, ["@skill:release", "@skill:review"]),
        ("use $rele", 2, ["@skill:release", "@skill:review"]),
    ],
    ids=["command", "slash-alone", "mention", "dollar"],
)
def test_backspace_reopens_the_menu_for_what_is_left(monkeypatch, typed, deletes, expected):
    """prompt_toolkit drops the menu on any edit, and it used to reopen only on typed characters:
    `/ps` then Backspace left `/p` with no menu at all."""
    app = TuiApp(completer=CommandCompleter(skills=lambda: ("release", "review")))

    def listed():
        state = app.input_buffer.complete_state
        return None if state is None else [c.text for c in state.completions]

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text(typed)
        wait_until(lambda: app.input_buffer.text == typed)
        time.sleep(0.1)
        pipe_input.send_text("\x7f" * deletes)
        if expected is None:
            wait_until(lambda: (listed() or []).count("/ps") == 1 and len(listed() or []) > 5)
        else:
            wait_until(lambda: listed() == expected)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_backspace_reopen_leaves_a_menu_that_navigated_back_alone(monkeypatch):
    """Shift-Tab past the first row returns the input to what was typed -- the text shrinks just
    as it does on Backspace, but the menu is mid-browse and keeps its own state, so Tab carries on
    from the top instead of the menu being rebuilt under it."""
    app = TuiApp(completer=CommandCompleter())

    def index():
        state = app.input_buffer.complete_state
        return "closed" if state is None else state.complete_index

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("/st")
        wait_until(lambda: index() is None)
        pipe_input.send_text("\t")  # onto the first row: its text previews into the input
        wait_until(lambda: index() == 0)
        pipe_input.send_text("\x1b[Z")  # Shift-Tab back past it: the input shrinks back to `/st`
        wait_until(lambda: index() is None and app.input_buffer.text == "/st")
        state = app.input_buffer.complete_state
        time.sleep(0.1)  # past the deferred reopen
        assert app.input_buffer.complete_state is state  # the same menu, not a rebuilt one
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize(("key", "index"), [("\x1b[B", 1), ("\x0e", 1), ("\t", 0)], ids=["down", "ctrl-n", "tab"])
def test_moving_from_the_default_row(monkeypatch, key, index):
    """Down and Ctrl-N only move, and the default row is already highlighted, so they step to the
    second row. Tab completes, so it fills in the highlighted row itself."""
    app = TuiApp(completer=CommandCompleter())

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("/")
        wait_until(lambda: default_completion(app.input_buffer.complete_state) is not None)
        completions = app.input_buffer.complete_state.completions
        pipe_input.send_text(key)
        wait_until(lambda: app.input_buffer.complete_state is not None and app.input_buffer.complete_state.complete_index == index)
        assert app.input_buffer.text == completions[index].text
        assert default_completion(app.input_buffer.complete_state) is None  # a real selection now
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize(
    ("typed", "keys"),
    [
        ("/st", []),  # the default row only: nothing to undo
        ("/st", ["\t"]),  # Tab previewed `/status` into the input: Esc takes it back
        ("use @skill:re", ["\x1b[B"]),  # a mention, a row moved to with Down
    ],
    ids=["default", "tab-preview", "mention"],
)
def test_esc_closes_the_menu_and_restores_what_was_typed(monkeypatch, typed, keys):
    """Esc closes a completion menu, as it does the @file: picker, and puts back the text as it
    was typed. Restoring it is not typing, so the menu stays closed: neither the Backspace reopen
    nor a pending transition brings it back."""
    submitted = []
    app = TuiApp(completer=CommandCompleter(skills=lambda: ("release", "review")), on_chat_submit=submitted.append)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text(typed)
        wait_until(lambda: app.input_buffer.complete_state is not None)
        for key in keys:
            pipe_input.send_text(key)
            time.sleep(0.05)
        if keys:
            wait_until(lambda: app.input_buffer.text != typed)
        pipe_input.send_text("\x1b")
        wait_until(lambda: app.input_buffer.complete_state is None, timeout=3)
        assert app.input_buffer.text == typed
        time.sleep(0.3)  # past any deferred reopen or namespace transition
        assert app.input_buffer.complete_state is None and submitted == []
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_esc_closes_the_menu_without_waiting(monkeypatch):
    """Esc used to wait a full `timeoutlen` (a second) to see whether it began Esc+Enter before
    the menu closed. It closes as soon as the key arrives."""
    app = TuiApp(completer=CommandCompleter())

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("/st")
        wait_until(lambda: app.input_buffer.complete_state is not None)
        sent = time.monotonic()
        pipe_input.send_text("\x1b")
        wait_until(lambda: app.input_buffer.complete_state is None)
        assert time.monotonic() - sent < 0.3
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize(
    ("keys", "text", "sent"),
    [
        # The chord as a terminal sends Alt+Enter: one burst. A newline, not a send.
        (["\x1b\r"], "/st\n", []),
        # The chord as a person types it, Esc and then Enter: the same newline.
        (["\x1b", "\r"], "/st\n", []),
        # Anything typed between them breaks the chord: Enter sends as usual.
        (["\x1b", "x", "\r"], "", ["/stx"]),
    ],
    ids=["alt-enter", "esc-then-enter", "esc-type-enter"],
)
def test_esc_enter_still_inserts_a_newline_with_the_menu_open(monkeypatch, keys, text, sent):
    """Esc is the first half of Esc+Enter, the newline chord. The menu's Esc closes at once, and
    Enter straight after it completes the chord, so it still types a newline -- but only straight
    after, so Esc, more typing, then Enter sends."""
    submitted = []
    app = TuiApp(completer=CommandCompleter(), on_chat_submit=submitted.append)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("/st")
        wait_until(lambda: app.input_buffer.complete_state is not None)
        for key in keys:
            pipe_input.send_text(key)
            time.sleep(0.2)
        wait_until(lambda: app.input_buffer.text == text and [str(value) for value in submitted] == sent)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


APPROVAL_ACTIONS = [("Approve", ""), ("Refuse", "n")]


@pytest.mark.parametrize(
    ("keys", "text", "answer"),
    [
        # Esc alone clears the typed reason back to the action row, at once; nothing answered yet.
        (["\x1b"], "", "pending"),
        # The newline chord in one burst (Alt+Enter): the reason stays, with a new line.
        (["\x1b\r"], "because\n", "pending"),
        # The chord as typed by hand: the Esc clears at once, the Enter puts the reason back.
        (["\x1b", "\r"], "because\n", "pending"),
    ],
    ids=["esc", "alt-enter", "esc-then-enter"],
)
def test_approval_esc_clears_the_reason_at_once_and_keeps_the_newline_chord(monkeypatch, keys, text, answer):
    """The approval prompt's Esc used to wait a full `timeoutlen` before clearing a typed reason,
    to see whether it began Esc+Enter. It clears at once now, and an Enter straight after restores
    the reason with a new line, as the chord always did."""
    app = TuiApp()
    results = []

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        app.app.loop.call_soon_threadsafe(lambda: app.set_approval_form(APPROVAL_ACTIONS))
        pending = request_input_from_driver(app)
        wait_until(lambda: app.input_mode == "approval")
        pipe_input.send_text("because")
        wait_until(lambda: app.input_buffer.text == "because")
        sent = time.monotonic()
        for key in keys:
            pipe_input.send_text(key)
            if len(keys) > 1:
                time.sleep(0.2)
        wait_until(lambda: app.input_buffer.text == text)
        if keys == ["\x1b"]:
            assert time.monotonic() - sent < 0.3
        time.sleep(0.1)
        results.append(pending.result() if pending.done() else "pending")
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)
    assert results == [answer]


def test_approval_esc_on_an_empty_line_refuses_at_once(monkeypatch):
    """With nothing typed, Esc cancels the approval -- `confirm` reads that as a refusal -- and it
    does so as soon as the key arrives."""
    app = TuiApp()

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        app.app.loop.call_soon_threadsafe(lambda: app.set_approval_form(APPROVAL_ACTIONS))
        pending = request_input_from_driver(app)
        wait_until(lambda: app.input_mode == "approval")
        sent = time.monotonic()
        pipe_input.send_text("\x1b")
        assert pending.result(timeout=2) is None
        assert time.monotonic() - sent < 0.3
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_approval_enter_long_after_esc_answers_normally(monkeypatch):
    """Past the chord's time, Enter on the cleared line is an ordinary Enter: it runs the focused
    action (Approve, answered as "")."""
    app = TuiApp()
    results = []

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        app.app.loop.call_soon_threadsafe(lambda: app.set_approval_form(APPROVAL_ACTIONS))
        pending = request_input_from_driver(app)
        wait_until(lambda: app.input_mode == "approval")
        pipe_input.send_text("because")
        wait_until(lambda: app.input_buffer.text == "because")
        pipe_input.send_text("\x1b")
        wait_until(lambda: app.input_buffer.text == "")
        time.sleep(app.app.timeoutlen + 0.2)
        pipe_input.send_text("\r")
        results.append(pending.result(timeout=2))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)
    assert results == [""]


def test_enter_long_after_esc_sends(monkeypatch):
    """The chord has the same time limit it always had (`timeoutlen`): an Enter pressed well after
    the Esc that closed the menu is an ordinary Enter and sends."""
    submitted = []
    app = TuiApp(completer=CommandCompleter(), on_chat_submit=submitted.append)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("/st")
        wait_until(lambda: app.input_buffer.complete_state is not None)
        pipe_input.send_text("\x1b")
        wait_until(lambda: app.input_buffer.complete_state is None)
        time.sleep(app.app.timeoutlen + 0.2)
        pipe_input.send_text("\r")
        wait_until(lambda: [str(value) for value in submitted] == ["/st"])
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_completion_hint_row_spans_the_whole_menu_width(monkeypatch):
    """The menu floats transparently over the transcript, so a hint row narrower than the menu
    would let the text beneath show through its right-hand gap. Every cell of the hint row under
    the menu is blanked and on the menu's surface, as far as the widest item row reaches."""
    server = "a-server-name-long-enough-to-widen-the-menu-past-the-hint"
    app = TuiApp(completer=CommandCompleter(mcp_servers=lambda: (server,)))
    output = ResizableOutput(rows=20, columns=90)
    checked = []

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("@mcp:")
        wait_until(lambda: rendered_screen_text(app.app, output).count(server) == 1)
        screen = app.app.renderer.last_rendered_screen
        text = rendered_screen_text(app.app, output).splitlines()
        item_row = next(index for index, line in enumerate(text) if server in line)
        hint_row = item_row + 1
        menu_columns = [column for column in range(output.size.columns) if "completion-menu" in screen.data_buffer[item_row][column].style]
        assert len(menu_columns) > len(_AlignedCompletionsMenu.HINT)
        for column in menu_columns:
            cell = screen.data_buffer[hint_row][column]
            assert "completion-menu.hint" in cell.style, column
        tail = "".join(screen.data_buffer[hint_row][column].char for column in menu_columns[len(_AlignedCompletionsMenu.HINT) :])
        assert tail.strip() == ""
        checked.append(len(menu_columns))
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive, output=output)
    assert checked


def test_completion_hint_row_overwrites_the_text_beneath_it():
    """A style alone only recolors a cell, it does not clear it: drawn the way a transparent float
    draws (no background erase) over a line of transcript, the hint row must blank every cell it
    spans, not only the ones its text covers. On a real screen the prompt sits at the bottom, so
    the menu flips up over the transcript and a gap there shows old text through."""
    menu = _AlignedCompletionsMenu()
    assert isinstance(menu.content, HSplit)
    hint = menu.content.children[1]
    width = len(_AlignedCompletionsMenu.HINT) + 12
    screen = Screen()
    for column in range(width):
        screen.data_buffer[0][column] = Char("x", "")

    hint.write_to_screen(screen, MouseHandlers(), WritePosition(0, 0, width, 1), "", erase_bg=False, z_index=None)
    screen.draw_all_floats()

    row = "".join(screen.data_buffer[0][column].char for column in range(width))
    assert row == _AlignedCompletionsMenu.HINT + " " * 12
    assert all("completion-menu.hint" in screen.data_buffer[0][column].style for column in range(width))


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
        wait_until(lambda: _completions(app) == _command_rows())

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
    assert _completions(app) == _command_rows()

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
