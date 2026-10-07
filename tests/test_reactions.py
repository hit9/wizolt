"""The model's emoji reaction to the user's message: the marker it opens a reply with, the held
message row the reaction joins, and the resumed transcript that draws it again."""

import re

from prompt_toolkit.formatted_text import fragment_list_to_text
from tui_harness import loop as make_loop

from wizolt.agent.prompts import split_reaction
from wizolt.base import LogBlock, LogLine, LogRole
from wizolt.ui.cli import TuiRuntime
from wizolt.ui.tui import TuiApp


def _live(tmp_path):
    """A TUI runtime with color on, whose printed scrollback is recorded instead of printed."""
    loop = make_loop(tmp_path)
    runtime = TuiRuntime(loop)
    loop.presentation.tui = TuiApp()
    loop.presentation.tui.set_running = lambda label: None
    loop.presentation.ui.color = True
    recorded = []
    loop.presentation.ui.transcript_sink = recorded.append
    return loop, runtime, lambda width=80: _plain("".join(item(width) for item in recorded))


def _plain(text):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r", "")


def _rows(text):
    return [row.rstrip() for row in text.splitlines()]


async def test_a_reply_opening_with_a_reaction_puts_it_beside_the_users_message(tmp_path):
    loop, runtime, transcript = _live(tmp_path)
    presentation = loop.presentation
    seen = {}

    async def run(user_input):
        presentation.model_stream_output("output", "[react:")
        # Half a marker is held back from the preview, and the message waits unprinted.
        seen["partial"] = fragment_list_to_text(loop.view.model_stream_fragments())
        seen["printed"] = transcript()
        presentation.model_stream_output("output", "🙏] Glad it")
        seen["held"] = fragment_list_to_text(presentation.ui.held_fragments(80))
        seen["preview"] = fragment_list_to_text(loop.view.model_stream_fragments())
        presentation.agent_answer_output("[react:🙏] Glad it works.")
        return "[react:🙏] Glad it works."

    loop.agent.run = run
    assert not await runtime.dispatch("thanks, that fixed it")
    await runtime.run_agent_turn("thanks, that fixed it")

    assert "[react" not in seen["partial"] and seen["printed"] == ""
    assert "• thanks, that fixed it  ← 🙏" in seen["held"]  # shown live, the moment the marker completes
    assert "Glad it" in seen["preview"] and "react" not in seen["preview"]
    printed = transcript()
    assert "• thanks, that fixed it  ← 🙏" in _rows(printed)
    assert printed.count("thanks, that fixed it") == 1
    assert "Glad it works." in printed and "react" not in printed
    assert presentation.ui.held_fragments(80) == []


async def test_the_first_output_prints_the_message_and_a_later_reaction_is_dropped(tmp_path):
    loop, runtime, transcript = _live(tmp_path)
    presentation = loop.presentation

    async def run(user_input):
        presentation.tool_output(LogBlock([LogLine("Read", "a.py", LogRole.TOOL)]))
        presentation.agent_answer_output("[react:👍] Done.")
        return "[react:👍] Done."

    loop.agent.run = run
    assert not await runtime.dispatch("read it")
    await runtime.run_agent_turn("read it")

    rows = _rows(transcript())
    assert "• read it" in rows  # no reaction: the row was already printed when it arrived
    assert rows.index("• read it") < next(index for index, row in enumerate(rows) if "a.py" in row)
    assert "Done." in transcript() and "react" not in transcript() and "←" not in transcript()


async def test_a_turn_reset_prints_a_message_nothing_else_printed(tmp_path):
    loop, runtime, transcript = _live(tmp_path)

    loop.presentation.user_message("are you there", turn=True)
    assert transcript() == ""
    runtime.reset_turn()

    assert "• are you there" in _rows(transcript())


async def test_commands_and_reactions_off_print_the_message_at_once(tmp_path):
    loop, runtime, transcript = _live(tmp_path)
    printed_before_command = []

    async def command(text):
        printed_before_command.append(transcript())
        return True, False

    loop.command = command
    assert await runtime.dispatch("/status")
    assert "• /status" in _rows(printed_before_command[0])

    loop.session.settings.reactions = False
    loop.presentation.user_message("plain turn", turn=True)
    assert "• plain turn" in _rows(transcript())


async def test_the_reaction_takes_a_row_of_its_own_when_the_last_row_is_full(tmp_path):
    loop, _runtime, transcript = _live(tmp_path)

    loop.presentation.ui.emit_answer("short", role="user", rule=False, reaction="🎉")
    loop.presentation.ui.emit_answer("x" * 15, role="user", rule=False, reaction="🎉")

    rows = _rows(transcript(20))
    assert "• short  ← 🎉" in rows
    # 17 cells of message leave room for the arrow but not the emoji: the mark starts the next
    # row whole rather than leaving its arrow behind.
    assert rows[rows.index("• " + "x" * 15) + 1] == "← 🎉"


async def test_a_resumed_session_draws_each_reaction_beside_the_message_it_answered(tmp_path):
    loop, _runtime, transcript = _live(tmp_path)
    loop.session.resumed = True
    loop.session.messages.extend(
        [
            {"role": "user", "content": "thanks"},
            {"role": "assistant", "content": "[react:🙏] Glad it works."},
            {"role": "user", "content": "next"},
            {"role": "assistant", "content": "[react:🦄] Unknown emoji stays as written."},
        ]
    )

    loop.resume.render_resumed_session()
    loop.presentation.ui.drain_scrollback()

    rows = _rows(transcript())
    assert "• thanks  ← 🙏" in rows and "• next" in rows
    assert "Glad it works." in transcript() and "[react:🙏]" not in transcript()
    assert "[react:🦄] Unknown emoji stays as written." in transcript()


def test_only_a_leading_known_marker_is_a_reaction():
    assert split_reaction("[react:👍] Done.") == ("👍", "Done.")
    assert split_reaction("\n[react:🎉]\nShipped.") == ("🎉", "Shipped.")
    assert split_reaction("[react:🦄] Hi") == ("", "[react:🦄] Hi")
    assert split_reaction("Use [react:👍] to react.") == ("", "Use [react:👍] to react.")
