"""tui mentions (split from tests/test_tui_app.py)."""

import time

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from tui_harness import run_interactive_tui, wait_until

from wizolt.agentsmd import MenuRow
from wizolt.cli import CommandCompleter
from wizolt.mentions import FilePick, active_mention
from wizolt.tui import InputMode, TuiApp


def _recording_picker(queries):
    """A picker that records the query it was opened with and selects nothing."""

    async def pick(query):
        queries.append(query)
        return FilePick()

    return pick


def test_mention_opens_completions_while_typing(monkeypatch):
    """`@`, `@kind:`, and `$` name something the completer knows, so the list opens as they are
    typed and narrows as more characters arrive - everything else in this prompt is prose and
    waits for Tab."""
    app = TuiApp(
        completer=CommandCompleter(
            mcp_servers=lambda: ("github", "gitlab", "playwright"),
            skills=lambda: ("release",),
            files=lambda: (("wizolt/tui.py", "wizolt/tui.py"), ("wizolt/hints.py", "wizolt/hints.py")),
        )
    )

    def completions():
        state = app.input_buffer.complete_state
        return None if state is None else [c.text for c in state.completions]

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)

        pipe_input.send_text("use @gi")
        wait_until(lambda: completions() == ["@mcp:github", "@mcp:gitlab"])

        pipe_input.send_text("th")
        wait_until(lambda: completions() == ["@mcp:github"])  # the list narrows as typing continues

        pipe_input.send_text(" and @")
        wait_until(lambda: completions() == ["@file:", "@mcp:", "@skill:", "@agents.md:"])

        pipe_input.send_text("mcp:")
        wait_until(lambda: completions() == ["@mcp:github", "@mcp:gitlab", "@mcp:playwright"])

        pipe_input.send_text("gi")
        wait_until(lambda: completions() == ["@mcp:github", "@mcp:gitlab"])

        pipe_input.send_text(" and @file:tu")
        wait_until(lambda: completions() == ["@file:wizolt/tui.py"])

        pipe_input.send_text(" and $")
        wait_until(lambda: completions() == ["@skill:release"])

        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_selecting_mention_kind_opens_its_candidate_list(monkeypatch):
    rows = [
        MenuRow("All applicable", "every instructions file below", "@agents.md:"),
        MenuRow("global · ~/.wizolt/AGENTS.md", "whole file", "@agents.md:global"),
    ]
    app = TuiApp(completer=CommandCompleter(skills=lambda: ("release", "review"), agents_rows=lambda: rows))

    def completions():
        state = app.input_buffer.complete_state
        return None if state is None else [c.text for c in state.completions]

    def state():
        current = app.input_buffer.complete_state
        return None if current is None else current.complete_index

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("@")
        wait_until(lambda: completions() == ["@file:", "@mcp:", "@skill:", "@agents.md:"])

        # Shift-Tab highlights the last namespace row; Enter commits it as real input, and Tab
        # then asks that namespace for its own candidates.
        pipe_input.send_text("\x1b[Z")
        wait_until(lambda: app.input_buffer.text == "@agents.md:")
        pipe_input.send_text("\r")
        wait_until(lambda: state() is None and app.input_buffer.text == "@agents.md:")
        pipe_input.send_text("\t")
        wait_until(lambda: completions() == [row.insert for row in rows])
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize(
    ("typed", "namespace", "expected"),
    [
        ("@m", "@mcp:", ["@mcp:github", "@mcp:gitlab"]),
        ("@sk", "@skill:", ["@skill:release", "@skill:review"]),
    ],
)
def test_selecting_partially_typed_name_kind_opens_its_candidate_list(monkeypatch, typed, namespace, expected):
    app = TuiApp(completer=CommandCompleter(mcp_servers=lambda: ("github", "gitlab"), skills=lambda: ("release", "review")))

    def completions():
        state = app.input_buffer.complete_state
        return None if state is None else [c.text for c in state.completions]

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text(typed)
        wait_until(lambda: completions() == [namespace])
        pipe_input.send_text("\t")
        wait_until(lambda: app.input_buffer.text == namespace)
        wait_until(lambda: completions() == expected)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize(
    ("steps", "expected", "next_level"),
    [
        (1, "@mcp:", ["@mcp:github", "@mcp:gitlab"]),
        (2, "@skill:", ["@skill:release", "@skill:review"]),
        (3, "@agents.md:", ["@agents.md:", "@agents.md:project"]),
    ],
)
def test_enter_on_a_browsed_kind_row_opens_its_candidates(monkeypatch, steps, expected, next_level):
    """Browsing the bare-@ menu already previews the kind into the input, so Enter changes no text;
    it still has to open the kind's own list, and a second Enter picks from that list."""
    rows = [MenuRow("All applicable", "every instructions file below", "@agents.md:"), MenuRow("Project · AGENTS.md", "whole file", "@agents.md:project")]
    app = TuiApp(completer=CommandCompleter(mcp_servers=lambda: ("github", "gitlab"), skills=lambda: ("release", "review"), agents_rows=lambda: rows))

    def menu():
        state = app.input_buffer.complete_state
        return None if state is None else ([c.text for c in state.completions], state.complete_index)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("@")
        wait_until(lambda: menu() is not None)
        pipe_input.send_text("\x0e" * (steps + 1))  # past @file: onto the kind
        wait_until(lambda: app.input_buffer.text == expected)
        pipe_input.send_text("\r")
        wait_until(lambda: menu() == (next_level, None))
        pipe_input.send_text("\x0e\x0e\r")  # the second row of the next level
        wait_until(lambda: app.input_buffer.text == next_level[1] and menu() is None)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_enter_on_all_applicable_commits_without_reopening_its_menu(monkeypatch):
    """`@agents.md:` is also the "All applicable" row of its own menu: there it is a finished
    choice, not a kind to open."""
    rows = [MenuRow("All applicable", "every instructions file below", "@agents.md:"), MenuRow("Project · AGENTS.md", "whole file", "@agents.md:project")]
    app = TuiApp(completer=CommandCompleter(agents_rows=lambda: rows))

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("@agents.md:")
        wait_until(lambda: app.input_buffer.complete_state is not None)
        pipe_input.send_text("\x0e\r")
        wait_until(lambda: app.input_buffer.complete_state is None)
        time.sleep(0.2)  # long enough for a wrongly scheduled reopen to land
        assert app.input_buffer.complete_state is None and app.input_buffer.text == "@agents.md:"
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_picking_an_mcp_server_cascades_into_its_tools(monkeypatch):
    """Enter on a server row commits the server -- a complete mention on its own -- and opens that
    server's tools with none highlighted, so a second Enter still sends the bare server. Typing the
    whole name offers the same tools; a server with no known tools has nothing to cascade into."""
    tools = {"github": ("create_issue", "search"), "gitlab": ()}
    app = TuiApp(completer=CommandCompleter(mcp_servers=lambda: ("github", "gitlab"), mcp_tools=lambda server: tools[server]))

    def menu():
        state = app.input_buffer.complete_state
        return None if state is None else ([c.text for c in state.completions], state.complete_index)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("@mcp:")
        wait_until(lambda: menu() == (["@mcp:github", "@mcp:gitlab"], None))
        pipe_input.send_text("\x0e\r")  # Ctrl-N onto github, Enter
        wait_until(lambda: menu() == (["@mcp:github.create_issue", "@mcp:github.search"], None))
        assert app.input_buffer.text == "@mcp:github"
        pipe_input.send_text("\x0e\r")  # into the first tool, Enter commits it and stops there
        wait_until(lambda: app.input_buffer.text == "@mcp:github.create_issue" and menu() is None)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)

    completer = CommandCompleter(mcp_servers=lambda: ("github", "gitlab"), mcp_tools=lambda server: tools[server])

    def offered(text):
        return [c.text for c in completer.get_completions(Document(text, len(text)), CompleteEvent())]

    assert offered("@mcp:github") == ["@mcp:github.create_issue", "@mcp:github.search"]
    assert offered("@mcp:gitlab") == []
    assert offered("@mcp:git") == ["@mcp:github", "@mcp:gitlab"]


def test_prose_and_email_do_not_open_completions(monkeypatch):
    """A menu on every keystroke would be noise: only a mention at the cursor opens one, and an
    address is not a mention because the `@` follows a word character."""
    app = TuiApp(completer=CommandCompleter(mcp_servers=lambda: ("github",)))
    seen = []

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("mail me at hit9@icloud")
        wait_until(lambda: app.input_buffer.text == "mail me at hit9@icloud")
        seen.append(app.input_buffer.complete_state)
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)

    assert seen == [None]


def test_mention_trigger_uses_canonical_scanner_spans():
    for text in (
        "use @file:tu",
        "use @",
        "use @skill:rel",
        "use @mcp:git",
    ):
        assert active_mention(text) is not None, text
    assert active_mention("mail me at hit9@icloud") is None
    assert active_mention("use file:notes here") is None
    assert active_mention("profile:x") is None


def test_file_picker_tab_replaces_only_active_span(monkeypatch):
    async def pick(query):
        return FilePick("docs/中文 notes.txt") if query == "not" else FilePick()

    app = TuiApp(file_picker_available_fn=lambda: True, file_picker_fn=pick)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("inspect @file:not please")
        for _ in range(len(" please")):
            pipe_input.send_text("\x1b[D")
        pipe_input.send_text("\t")
        wait_until(lambda: app.input_buffer.text == 'inspect @file:"docs/中文 notes.txt" please')
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_running_tab_keeps_completing_an_active_file_mention():
    """A file mention owns Tab even before its candidates arrive: with no picker and a completion
    that is still loading, the keystroke must not hold the half-typed path for the next turn."""
    held: list[str] = []
    app = TuiApp(file_picker_available_fn=lambda: False, file_complete_fn=lambda _query, _ready: None, on_queue_next_turn=held.append)
    app.set_running("working")
    app.input_buffer.insert_text("look at @file:ap")

    app.tab_or_complete(app.input_buffer, reverse=False)

    assert held == []
    assert app.input_buffer.text == "look at @file:ap"


def test_file_picker_opens_after_typing_without_tab(monkeypatch):
    queries = []

    async def pick(query):
        queries.append(query)
        return FilePick("wizolt/tui.py")

    app = TuiApp(file_picker_available_fn=lambda: True, file_picker_fn=pick)

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("inspect @file:")
        wait_until(lambda: app.input_buffer.text == "inspect @file:wizolt/tui.py")
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)

    assert queries == [""]


@pytest.mark.parametrize("typed", ["@f", "@fi"])
def test_selecting_partially_typed_file_kind_opens_picker(monkeypatch, typed):
    queries = []
    app = TuiApp(
        completer=CommandCompleter(),
        file_picker_available_fn=lambda: True,
        file_picker_fn=_recording_picker(queries),
    )

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text(typed)
        wait_until(lambda: app.input_buffer.complete_state is not None)
        pipe_input.send_text("\t")
        wait_until(lambda: queries == [""] and not app._file_picker_active)
        assert app.input_buffer.text == "@file:"
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize("key", ["\x1b[B", "\t", "\x0e"], ids=["down", "tab", "ctrl-n"])
def test_browsing_bare_kind_menu_does_not_launch_file_picker(monkeypatch, key):
    """Highlighting @file: in the bare-@ menu is a preview, not a choice: arrow/Tab/Ctrl-N through
    the four kind rows without the file picker grabbing the terminal, and Enter on a later row
    commits it (the picker only opens on an explicit Enter on @file:). Tab is the one that used to
    slip: on a previewed @file: it read as "open the picker", not "next row"."""
    queries = []
    app = TuiApp(
        completer=CommandCompleter(mcp_servers=lambda: ("github",), skills=lambda: ("release", "review")),
        file_picker_available_fn=lambda: True,
        file_picker_fn=_recording_picker(queries),
    )

    def state():
        current = app.input_buffer.complete_state
        return None if current is None else (current.complete_index, [c.text for c in current.completions])

    kinds = ["@file:", "@mcp:", "@skill:", "@agents.md:"]

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("@")
        wait_until(lambda: state() is not None and state()[1] == kinds)

        for expected_index, expected_text in ((0, "@file:"), (1, "@mcp:"), (2, "@skill:"), (3, "@agents.md:")):
            pipe_input.send_text(key)
            wait_until(lambda text=expected_text, idx=expected_index: app.input_buffer.text == text and state() is not None and state()[0] == idx)
            assert state()[1] == kinds  # still browsing the same kind rows
            assert queries == [] and not app._file_picker_active

        pipe_input.send_text("\r")
        wait_until(lambda: state() is None and app.input_buffer.text == "@agents.md:")
        assert queries == [] and not app._file_picker_active
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize("mode", [InputMode.CHAT, InputMode.RUNNING])
def test_tab_browses_past_the_file_kind_in_every_direction(monkeypatch, mode):
    """Tab on a previewed @file: row is "next row", never "open the picker" -- forwards, after
    Shift-Tab brings the cursor back onto it, and after the menu wraps past its last row -- while a
    turn is running as much as when idle. Each landing on @file: waits out the namespace transition
    delay, so a picker scheduled late would still be caught."""
    queries = []
    app = TuiApp(
        completer=CommandCompleter(mcp_servers=lambda: ("github",), skills=lambda: ("release",)),
        file_picker_available_fn=lambda: True,
        file_picker_fn=_recording_picker(queries),
    )

    def at(text, index):
        state = app.input_buffer.complete_state
        return app.input_buffer.text == text and state is not None and state.complete_index == index

    def press(key, text, index):
        pipe.send_text(key)
        wait_until(lambda: at(text, index))
        if text == "@file:":
            time.sleep(0.2)
            assert at(text, index), "the preview must still be the menu's"
        assert queries == [] and not app._file_picker_active, f"picker opened on {key!r} to {text}"

    def drive(pipe_input):
        nonlocal pipe
        pipe = pipe_input
        wait_until(lambda: app.app is not None and app.app.is_running)
        app.input_mode = mode
        pipe_input.send_text("@")
        wait_until(lambda: at("@", None))
        press("\t", "@file:", 0)
        press("\t", "@mcp:", 1)
        press("\x1b[Z", "@file:", 0)  # Shift-Tab back onto @file:
        press("\t", "@mcp:", 1)  # and Tab still moves on
        press("\t", "@skill:", 2)
        press("\t", "@agents.md:", 3)
        press("\t", "@", None)  # past the last row: back to what was typed
        press("\t", "@file:", 0)  # and round again, still a preview
        app.app.loop.call_soon_threadsafe(app.app.exit)

    pipe = None
    run_interactive_tui(monkeypatch, app, drive=drive)


@pytest.mark.parametrize("key", ["\x1b[B", "\t", "\x0e"], ids=["down", "tab", "ctrl-n"])
def test_enter_on_at_file_kind_row_opens_the_file_picker(monkeypatch, key):
    """Browsing to @file: is inert; an explicit Enter on the row commits the kind and opens the
    picker with an empty query, exactly as typing the namespace does -- however the row was
    reached."""
    queries = []
    app = TuiApp(
        completer=CommandCompleter(),
        file_picker_available_fn=lambda: True,
        file_picker_fn=_recording_picker(queries),
    )

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("@")
        wait_until(lambda: app.input_buffer.complete_state is not None)
        pipe_input.send_text(key)
        wait_until(lambda: app.input_buffer.text == "@file:")
        time.sleep(0.2)  # past the namespace transition delay
        assert queries == [] and not app._file_picker_active  # preview alone must not open it
        pipe_input.send_text("\r")
        wait_until(lambda: queries == [""] and not app._file_picker_active)
        assert app.input_buffer.text == "@file:"
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_pasting_file_namespace_does_not_open_picker(monkeypatch):
    queries = []
    app = TuiApp(file_picker_available_fn=lambda: True, file_picker_fn=_recording_picker(queries))

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("\x1b[200~@file:\x1b[201~")
        wait_until(lambda: app.input_buffer.text == "@file:")
        time.sleep(app.MENTION_TRANSITION_DELAY * 2)
        assert queries == []
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_file_picker_cancel_keeps_buffer(monkeypatch):
    queries = []
    app = TuiApp(file_picker_available_fn=lambda: True, file_picker_fn=_recording_picker(queries))

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("@file:keep")
        wait_until(lambda: queries == ["keep"] and not app._file_picker_active)
        time.sleep(app.MENTION_TRANSITION_DELAY * 2)
        assert app.input_buffer.text == "@file:keep"
        assert queries == ["keep"]  # Cancel does not reopen until the input changes again.
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_enter_commits_highlighted_completion_without_sending(monkeypatch):
    """Tab previews a mention row; Enter commits it into the input instead of sending the
    message, so the prompt stays open and a second Enter sends."""
    submitted = []
    app = TuiApp(
        completer=CommandCompleter(skills=lambda: ("release", "review")),
        on_chat_submit=submitted.append,
    )

    def state():
        current = app.input_buffer.complete_state
        return None if current is None else (current.complete_index, [c.text for c in current.completions])

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("use @skill:")
        wait_until(lambda: (state() or (None, []))[1] == ["@skill:release", "@skill:review"])
        pipe_input.send_text("\t")
        wait_until(lambda: state() is not None and state()[0] is not None)
        pipe_input.send_text("\r")
        wait_until(lambda: state() is None and app.input_buffer.text == "use @skill:release")
        assert submitted == []
        pipe_input.send_text("\r")
        wait_until(lambda: submitted == ["use @skill:release"])
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)


def test_enter_sends_when_completion_menu_has_no_highlighted_row(monkeypatch):
    """The menu opens while typing with no row highlighted; Enter there still sends, so a fully
    typed mention goes out in one press (only Tab-highlighted rows are committed by Enter)."""
    submitted = []
    app = TuiApp(
        completer=CommandCompleter(skills=lambda: ("release", "review")),
        on_chat_submit=submitted.append,
    )

    def drive(pipe_input):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe_input.send_text("use @skill:")
        wait_until(lambda: app.input_buffer.complete_state is not None and app.input_buffer.complete_state.current_completion is None)
        pipe_input.send_text("\r")
        wait_until(lambda: submitted == ["use @skill:"])
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)
