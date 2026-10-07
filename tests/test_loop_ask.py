"""loop ask (split from tests/test_loop_commands.py)."""

import asyncio
import json
import re
import time
from types import SimpleNamespace

import pytest
from agent_harness import session
from prompt_toolkit.formatted_text import to_formatted_text

import wizolt.ui.cli.modals as modals_mod
from wizolt.agent.engine import Agent
from wizolt.base import (
    DISMISSED,
    SELECTION_BACK,
    LogBlock,
    LogEdge,
    LogLine,
    LogRole,
    Text,
    ToolCall,
)
from wizolt.tools import AskSpec, Tool, toolblocks
from wizolt.tools.toolblocks import ToolDisplay
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.modals import choice_application, question_interaction
from wizolt.ui.cli.presentation import Presentation
from wizolt.ui.render import Theme
from wizolt.ui.tui import ASK_DONE, ASK_FREE_TEXT


def _answers(answer):
    """A stand-in for TuiApp.request_input: the question is put on the loop and awaited."""

    async def request_input(prompt):
        return answer(prompt) if callable(answer) else answer

    return request_input


def _modals(results):
    """A stand-in for TuiApp.show_modal, handing back one scripted result per call."""

    async def show_modal(fragments_fn, key_fn, **_kwargs):
        return next(results) if hasattr(results, "__next__") else results(fragments_fn, key_fn)

    return show_modal


async def test_choice_application_expands_escaped_preview_newlines(tmp_path):
    output = []
    loop = CommandLoop(Agent(session(tmp_path), output_fn=output.append), input_fn=lambda prompt="": "", output_fn=output.append)
    loop.interactive_input = True
    rendered = []

    class Modal:
        async def show_modal(self, fragments_fn, key_fn, exclusive=False):
            rendered.extend(fragments_fn())
            return key_fn("enter", "")

    loop.presentation.tui = Modal()

    result = await choice_application(
        loop,
        "Select:",
        ("A", "B"),
        {},
        "",
        set(),
        preview_fn=lambda choice: "one\\ntwo" if choice == "A" else "",
    )

    assert result == "A"
    previews = [text for style, text in rendered if style == "class:choice.preview"]
    assert previews == ["  │ one\n", "  │ two\n"]
    assert all("\\n" not in text for _, text in rendered)


async def test_ask_free_text_prompt_has_no_control_newline(tmp_path):
    """A free-text page drops out of the modal to the shared input row; the answer flows into
    the batch and the modal reopens (ASK_DONE ends it)."""
    output = []
    loop = CommandLoop(Agent(session(tmp_path), output_fn=output.append), input_fn=lambda prompt="": "", output_fn=output.append)
    loop.interactive_input = True
    prompts = []
    results = iter([(ASK_FREE_TEXT, 0), ASK_DONE])
    loop.presentation.tui = SimpleNamespace(
        request_input=_answers(lambda prompt: prompts.append(prompt) or "typed answer"),
        show_modal=_modals(results),
    )

    assert await question_interaction(loop, [AskSpec("Pick?", choices=["A"], previews=["preview"])]) == ["typed answer"]
    assert prompts == ["\nPick?"]  # one shared-input prompt, the question spelled out again


async def test_ask_free_text_empty_answer_is_kept(tmp_path):
    """An explicitly empty free-text answer is a legal answer: the batch must return [""] and
    never fall back to the question text (which is only the placeholder for unanswered pages)."""
    output = []
    loop = CommandLoop(Agent(session(tmp_path), output_fn=output.append), input_fn=lambda prompt="": "", output_fn=output.append)
    loop.interactive_input = True
    results = iter([(ASK_FREE_TEXT, 0), ASK_DONE])
    loop.presentation.tui = SimpleNamespace(
        request_input=_answers(""),
        show_modal=_modals(results),
    )

    assert await question_interaction(loop, [AskSpec("Pick?")]) == [""]


async def test_ask_free_text_on_last_question_submits_without_reentering_modal(tmp_path):
    """A free-text answer to the final question completes the batch right after the shared input
    row; the modal must not reopen for it (a second show_modal would fail the call-count assert)."""
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), input_fn=lambda prompt: "", output_fn=lambda text: None)
    loop.interactive_input = True
    calls = []

    def show_modal(fragments_fn, key_fn):
        calls.append(1)
        key_fn("enter")  # page 1: accept the preselected option, advance to page 2
        key_fn("2")  # page 2: move onto "Type freely..." (digits only move the cursor)
        return key_fn("enter")  # ...and select it -> drops to the shared input row

    loop.presentation.tui = SimpleNamespace(request_input=_answers("typed"), show_modal=_modals(show_modal))

    assert await question_interaction(loop, [AskSpec("One?", choices=["A"]), AskSpec("Two?", choices=["B"])]) == ["A", "typed"]
    assert len(calls) == 1


async def test_ask_without_choices_uses_shared_tui_input(tmp_path):
    """A question without choices is a single Type-freely page; Enter drops to the shared row."""
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), input_fn=lambda prompt: "fallback", output_fn=lambda text: None)
    loop.interactive_input = True
    prompts = []
    results = iter([(ASK_FREE_TEXT, 0), ASK_DONE])
    loop.presentation.tui = SimpleNamespace(
        request_input=_answers(lambda prompt: prompts.append(prompt) or "typed answer"),
        show_modal=_modals(results),
    )

    assert await question_interaction(loop, [AskSpec("Explain the issue")]) == ["typed answer"]
    assert prompts == ["\nExplain the issue"]


async def test_ask_headless_keeps_plain_per_question_prompts(tmp_path):
    """Without a TUI the batch falls back to one read_input per question, in order."""
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), input_fn=lambda prompt: "fallback", output_fn=lambda text: None)
    prompts = []
    loop.read_input_sync = lambda prompt: prompts.append(prompt) or "answer"

    assert await question_interaction(loop, [AskSpec("One?"), AskSpec("Two?", choices=["A"])]) == ["answer", "answer"]
    assert prompts == ["\nOne?", "\nTwo?"]


async def test_ask_choice_is_not_echoed_before_final_tool_log(tmp_path, monkeypatch):
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), output_fn=lambda text: None)
    emitted = []
    loop.presentation.emit = lambda text="", indent=0: emitted.append(text)

    async def answered(_loop, _specs):
        return ["B"]

    monkeypatch.setattr(modals_mod, "question_interaction", answered)

    assert await modals_mod.question_interaction(loop, [AskSpec("Which?", choices=["A", "B"])]) == ["B"]
    assert emitted == []


async def test_ask_notes_flow_into_the_answer(tmp_path):
    """A note entered on a page (`n`, text, Enter) is appended to that question's answer."""
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), input_fn=lambda prompt: "fallback", output_fn=lambda text: None)
    loop.interactive_input = True

    def show_modal(fragments_fn, key_fn):
        key_fn("n")
        for ch in "keep the header":
            key_fn("any", ch)
        key_fn("enter")  # save the note
        return key_fn("enter")  # pick the recommended "A" and submit the batch

    loop.presentation.tui = SimpleNamespace(show_modal=_modals(show_modal))

    assert await question_interaction(loop, [AskSpec("Q?", choices=["A"], recommended=0)]) == ["A\n\nUser notes: keep the header"]


async def test_ask_escape_cancels_the_whole_batch(tmp_path):
    """Esc on any page cancels every question with the DISMISSED marker."""
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), input_fn=lambda prompt: "fallback", output_fn=lambda text: None)
    loop.interactive_input = True
    loop.presentation.tui = SimpleNamespace(show_modal=_modals(lambda fragments_fn, key_fn: SELECTION_BACK))

    result = await question_interaction(loop, [AskSpec("One?"), AskSpec("Two?")])
    assert result == [DISMISSED, DISMISSED]


async def test_ask_ctrl_c_cancels_the_turn_without_leaking_keyboard_interrupt(tmp_path):
    """Ctrl-C is a turn cancellation, not a process-level KeyboardInterrupt escaping asyncio."""
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), output_fn=lambda text: None)
    loop.interactive_input = True
    loop.presentation.tui = SimpleNamespace(show_modal=_modals(lambda fragments_fn, key_fn: key_fn("c-c")))

    with pytest.raises(asyncio.CancelledError):
        await question_interaction(loop, [AskSpec("Pick?", choices=["A", "B"])])


async def test_elapsed_since_uses_whole_seconds(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 104.9)
    assert Text.elapsed_since(100.0) == "4s"

    monkeypatch.setattr(time, "monotonic", lambda: 162.9)
    assert Text.elapsed_since(100.0) == "1m02s"


async def test_bash_live_start_pauses_standalone_status(tmp_path):
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), output_fn=lambda text: None)
    loop.presentation.ui.color = True
    loop.presentation.live_preview.start = lambda: setattr(loop.presentation.live_preview, "active", True)
    loop.presentation.status_bar.running = True
    loop.presentation.status_bar.stop = lambda: setattr(loop.presentation.status_bar, "running", False)
    loop.presentation.status_bar.start = lambda **_kwargs: setattr(loop.presentation.status_bar, "running", True)

    loop.presentation.tool_live_start()
    assert loop.presentation.live_status_paused is True
    assert loop.presentation.status_bar.running is False

    loop.presentation.tool_live_output("", "")
    assert loop.presentation.live_status_paused is False
    assert loop.presentation.status_bar.running is True


async def test_command_loop_indents_intermediate_and_final_messages(tmp_path):
    output = []
    loop = CommandLoop(Agent(session(tmp_path), output_fn=output.append), output_fn=output.append)

    loop.presentation.emit_narration("First line.\nSecond line.")
    loop.presentation.ui.emit_answer("Done.\nFinal detail.")

    assert output == ["  First line.\n  Second line.", "Done.\nFinal detail."]


async def test_colored_assistant_and_tool_blocks_each_start_with_one_blank_line(tmp_path):
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda _text: None), output_fn=lambda _text: None)
    loop.presentation.ui.color = True
    loop.presentation.ui.emit_phase_rule = lambda: None  # the narration's opening rule is this test's noise
    loop.presentation.ui.trailing_blanks = 0  # content above with no gap after it: the blocks need their blank line
    transcript = _transcript(loop)

    loop.presentation.emit_narration("Working on it.")
    loop.presentation.tool_output(LogBlock.hierarchy(LogLine("Bash", "first"), []))
    loop.presentation.tool_output(LogBlock.hierarchy(None, [LogLine("stored", "tr.1")]))
    loop.presentation.tool_output(LogBlock.hierarchy(LogLine("Bash", "second"), []))

    assert transcript() == "\n  Working on it.\n\n  Bash  first\n    stored  tr.1\n\n  Bash  second\n"


def _colored_loop(tmp_path):
    """A CommandLoop with color on, whose rendered output is collected instead of printed."""
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda _text: None), output_fn=lambda _text: None)
    loop.presentation.ui.color = True
    loop.presentation.ui._scrollback_print = lambda _fragment: None
    return loop


def _transcript(loop):
    """What the loop's printer recorded, as the plain rows a replay at 80 columns draws."""
    recorded = []
    loop.presentation.ui.transcript_sink = recorded.append
    return lambda: re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", "".join(item(80) for item in recorded)).replace("\r", "")


async def test_interim_narration_closes_with_a_phase_rule_when_far_from_last_rule(tmp_path):
    """A turn's interim narration is closed by the same full-width rule the turn ends with,
    minus the label, provided the rule would not land too close to the one above it. The blank
    line and the narration's own rows count toward the distance."""
    loop = _colored_loop(tmp_path)
    rules = []
    loop.presentation.ui.emit_phase_rule = lambda: rules.append(1)
    # A rule has already been drawn this turn, so distance applies; one row short of the
    # threshold before the blank line and the narration itself count. The block above ends without
    # a gap, so the narration does open one, and that row counts.
    loop.presentation.ui.rows_since_rule = loop.presentation.MIN_ROWS_BETWEEN_RULES - 1
    loop.presentation.ui.trailing_blanks = 0

    loop.presentation.emit_narration("Working on it.")

    assert rules == [1]


async def test_a_user_message_opens_the_turn_with_whitespace(tmp_path):
    loop = _colored_loop(tmp_path)
    rules = []
    loop.presentation.ui.emit_phase_rule = lambda: rules.append(1)
    loop.presentation.ui.rows_since_rule = 100
    loop.presentation.ui.trailing_blanks = 0

    loop.presentation.user_turn_rule()

    assert rules == []
    assert loop.presentation.ui.trailing_blanks == 1
    assert loop.presentation.ui.rows_since_rule == 0


async def test_user_turn_rule_restarts_the_silent_batch_count(tmp_path):
    loop = _colored_loop(tmp_path)
    loop.presentation._silent_batches = 3

    loop.presentation.user_turn_rule()

    assert loop.presentation._silent_batches == 0


async def test_interim_narration_skips_the_rule_when_too_close_to_the_last_one(tmp_path):
    """The agent saying two things in quick succession is one phase, not two: a rule that would
    land within MIN_ROWS_BETWEEN_RULES of the one above it is skipped, so the transcript does not
    collect a row of dashes for every sentence."""
    loop = _colored_loop(tmp_path)
    rules = []
    loop.presentation.ui.emit_phase_rule = lambda: rules.append(1)
    loop.presentation.ui.rows_since_rule = 0

    loop.presentation.emit_narration("Working on it.")

    assert rules == []


async def test_final_answer_takes_no_phase_rule(tmp_path):
    """The turn-end rule already closes the turn, so the answer must not add a second rule of its
    own -- two rules in a row would read as a box."""
    loop = _colored_loop(tmp_path)
    rules = []
    loop.presentation.ui.emit_phase_rule = lambda: rules.append(1)
    loop.presentation.ui.rows_since_rule = 100

    loop.presentation.agent_answer_output("Done.")

    assert rules == []


async def test_a_run_of_one_line_calls_is_packed_into_a_list(tmp_path):
    """Calls that each fit on one line run together as a list; the blank row comes back for the
    first one of a run and for any call that brings something with it."""
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda _text: None), output_fn=lambda _text: None)
    loop.presentation.ui.color = True
    loop.presentation.ui.trailing_blanks = 0
    transcript = _transcript(loop)

    def one_liner(name):
        return LogBlock([LogLine(name, "x.py", LogRole.TOOL)])

    loop.presentation.tool_output(one_liner("Read"))
    assert transcript() == "\n  Read  x.py\n"  # nothing above it was a one-line call, so it still parts itself

    loop.presentation.tool_output(one_liner("Read"))
    assert transcript().endswith("\n  Read  x.py\n  Read  x.py\n")  # packed straight under the call above

    with_output = LogBlock.hierarchy(LogLine("Bash", "pytest -q", LogRole.TOOL), [LogLine("", "41 passed", LogRole.OUTPUT, LogEdge.END)])
    loop.presentation.tool_output(with_output)
    assert transcript().endswith("  Read  x.py\n\n  Bash  pytest -q\n    └ 41 passed\n")  # a call that brings output is parted

    loop.presentation.tool_output(one_liner("Read"))
    assert transcript().endswith("    └ 41 passed\n\n  Read  x.py\n")  # and the run has to start over under it


STANDARD_CALLS = "\n  Bash  rg x0 → tr.0\n    └ hit0\n\n  Bash  rg x1 → tr.1\n    └ hit1\n"
MINIMAL_CALLS = "\n  ● bash  rg x0 → tr.0\n  ● bash  rg x1 → tr.1\n"


@pytest.mark.parametrize(("printed", "switched", "before", "after"), [({}, "preset:minimal", STANDARD_CALLS, MINIMAL_CALLS), ({"format": "preset:minimal"}, "", MINIMAL_CALLS, STANDARD_CALLS)])
async def test_the_gaps_between_calls_follow_a_record_format_switched_after_them(tmp_path, printed, switched, before, after):
    """Redrawn records keep the transcript's spacing rule: one-line calls packed into a list, a
    call with output parted by a blank row. A gap decided in the old format would leave a
    minimal checklist double-spaced, or standard records glued together."""
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda _text: None), output_fn=lambda _text: None)
    loop.presentation.ui.color = True
    loop.presentation.ui.trailing_blanks = 0
    loop.session.config.transcript = printed
    transcript = _transcript(loop)
    for index in range(2):
        call = ToolCall(f"bash-{index}", "Bash", [f"rg x{index}"])
        output = Tool.process_result("BashToolResult", 0, f"hit{index}", "")
        loop.presentation.tool_output(toolblocks.finish_display(loop.session, call, f"tr.{index}", output, failed=False))
    assert transcript() == before

    loop.session.config.transcript = {"format": switched}
    assert transcript() == after


async def test_a_live_call_printed_under_minimal_regains_its_output_and_gap(tmp_path):
    """Under `minimal` a live call's settled record draws nothing below its call line; it is still
    printed, so switching back shows the output under the line and parts the next call from it."""
    session_ = session(tmp_path)
    session_.config.transcript = {"format": "preset:minimal"}
    loop = CommandLoop(Agent(session_, output_fn=lambda _text: None), output_fn=lambda _text: None)
    loop.presentation.ui.color = True
    loop.presentation.ui.trailing_blanks = 0
    loop.agent.tools.output_fn = loop.presentation.tool_output
    transcript = _transcript(loop)
    runner = loop.agent.tools
    live = ToolCall("bash-1", "Bash", ["pytest -q"])
    runner.emit(toolblocks.running_line(session_, live, ToolDisplay()))
    runner.emit(toolblocks.finish_display(session_, live, "tr.1", Tool.process_result("BashToolResult", 0, "41 passed", ""), failed=False, d=ToolDisplay(nested_display=True)))
    runner.emit(toolblocks.finish_display(session_, ToolCall("read-1", "Read", [{"path": "x.py"}]), "tr.2", "body", failed=False))
    assert transcript() == "\n  ● bash  pytest -q\n  ● read  x.py → tr.2\n"

    session_.config.transcript = {}
    assert transcript() == "\n  Bash  pytest -q\n    └ 41 passed · tr.1\n\n  Read  x.py → tr.2\n"


async def test_tool_batch_closes_a_long_silent_run_with_a_phase_rule(tmp_path):
    """While the agent works in silence its calls run together; a stretch of silent tool
    batches -- the model never saying anything back -- closes with the same seam, fired after
    the batch's output is out so a batch is never cut in half."""
    loop = _colored_loop(tmp_path)
    rules = []
    loop.presentation.ui.emit_run_seam = lambda style: rules.append(style())
    loop.presentation._silent_batches = loop.presentation.TOOL_RUN_RULE_BATCHES - 1
    loop.presentation.ui.rows_since_rule = loop.presentation.MIN_ROWS_BETWEEN_RULES

    loop.presentation.tool_batch_output(True)

    assert rules == ["rule"]


async def test_a_silent_run_too_close_to_the_rule_above_draws_no_seam(tmp_path):
    """Long enough by the batch count, but only a few rows of packed one-line calls: a rule there
    would part nothing. The count keeps running, so the seam lands once the rows are there."""
    loop = _colored_loop(tmp_path)
    rules = []
    loop.presentation.ui.emit_run_seam = lambda style: rules.append(style())
    loop.presentation._silent_batches = loop.presentation.TOOL_RUN_RULE_BATCHES - 1
    loop.presentation.ui.rows_since_rule = loop.presentation.MIN_ROWS_BETWEEN_RULES - 2

    loop.presentation.tool_batch_output(True)
    assert rules == []

    loop.presentation.ui.rows_since_rule = loop.presentation.MIN_ROWS_BETWEEN_RULES
    loop.presentation.tool_batch_output(True)
    assert rules == ["rule"]


async def test_tool_batch_keeps_a_short_silent_run_together(tmp_path):
    loop = _colored_loop(tmp_path)
    rules = []
    loop.presentation.ui.emit_run_seam = lambda style: rules.append(style())
    loop.presentation._silent_batches = loop.presentation.TOOL_RUN_RULE_BATCHES - 2

    loop.presentation.tool_batch_output(True)

    assert rules == []


async def test_a_voiced_batch_is_not_silent(tmp_path):
    """A batch that carried narration reports through the same hook but does not count
    toward the silent run: the agent said something, so the seam is not needed yet."""
    loop = _colored_loop(tmp_path)
    rules = []
    loop.presentation.ui.emit_run_seam = lambda style: rules.append(style())
    loop.presentation._silent_batches = loop.presentation.TOOL_RUN_RULE_BATCHES - 1

    loop.presentation.tool_batch_output(False)

    assert rules == []
    assert loop.presentation._silent_batches == loop.presentation.TOOL_RUN_RULE_BATCHES - 1


async def test_engine_routes_the_answer_and_batch_end_to_the_loop(tmp_path):
    """The engine's final answer and batch-end facts are wired to the loop's presentation hooks:
    the answer goes to the no-rule path, and each tool batch reports its end."""
    loop = _colored_loop(tmp_path)

    assert loop.agent.final_output_fn.__func__ is Presentation.agent_answer_output
    assert loop.agent.hooks.on_tool_batch.__func__ is Presentation.tool_batch_output


async def test_phase_rule_renders_as_an_unlabelled_full_width_solid_rule(tmp_path):
    """The phase rule is the turn-end rule's line with the label removed: the same gray, the same
    solid dash, edge to edge, and no text of its own -- the narration it closes is the label."""
    loop = _colored_loop(tmp_path)
    frags = []
    loop.presentation.ui._scrollback_print = lambda fragment: frags.append(fragment)

    loop.presentation.ui.emit_phase_rule()

    # The dash line, then a blank row that lifts the rule off whatever follows it (the callers
    # draw the blank row above, so each seam lands once).
    fragments = to_formatted_text(frags[0])
    assert [style for style, _ in fragments] == [Theme.fg("rule"), ""]
    text = "".join(fragment for _, fragment in fragments)
    assert text.endswith("\n\n")
    assert set(text) <= {"─", "\n"}
    assert loop.presentation.ui.rows_since_rule == 0


async def test_emit_counts_rendered_rows_toward_rule_distance(tmp_path):
    """The distance between rules is measured in rendered rows, not in blocks: one Bash call with
    its output goes further than four Reads, and a wrapped block counts what it actually took."""
    loop = _colored_loop(tmp_path)

    loop.presentation.ui.emit("a\nb")
    assert loop.presentation.ui.rows_since_rule == 2
    loop.presentation.ui.emit("")
    assert loop.presentation.ui.rows_since_rule == 3


async def test_rule_due_reports_whether_a_rule_would_land_far_enough(tmp_path):
    """The distance query is the one place a phase rule's spacing is judged: color off never
    draws (there are no rules to be close to), and the threshold is inclusive."""
    loop = _colored_loop(tmp_path)
    assert not loop.presentation.ui.rule_due(loop.presentation.MIN_ROWS_BETWEEN_RULES)
    loop.presentation.ui.rows_since_rule = loop.presentation.MIN_ROWS_BETWEEN_RULES
    assert loop.presentation.ui.rule_due(loop.presentation.MIN_ROWS_BETWEEN_RULES)
    loop.presentation.ui.color = False
    assert not loop.presentation.ui.rule_due(loop.presentation.MIN_ROWS_BETWEEN_RULES)


async def test_turn_end_rule_resets_rule_distance(tmp_path):
    """The turn-end rule is a solid rule like any other, so the distance counter starts over at
    the close of a turn rather than carrying the whole turn's length into the next one."""
    loop = _colored_loop(tmp_path)
    loop.presentation.ui.emit("a\nb")

    loop.presentation.ui.emit_turn_end(1.0)

    assert loop.presentation.ui.rows_since_rule == 0




async def test_full_turn_parts_at_user_rule_narration_and_silent_batches(tmp_path):
    """End to end through the engine: the turn opens with whitespace, later phases are parted only
    when far enough apart, and a long silent tool run gets one separator too."""
    loop = _colored_loop(tmp_path)
    rules = []

    def emit_rule():
        rules.append(loop.presentation.ui.rows_since_rule)
        loop.presentation.ui.rows_since_rule = 0

    loop.presentation.ui.emit_phase_rule = emit_rule
    real_seam = loop.presentation.ui.emit_run_seam

    def emit_seam(style):
        if style() == "rule":
            rules.append(loop.presentation.ui.rows_since_rule)
        real_seam(style)

    loop.presentation.ui.emit_run_seam = emit_seam
    silences = []
    on_batch = loop.agent.hooks.on_tool_batch
    loop.agent.hooks.on_tool_batch = lambda silent: (on_batch(silent), silences.append(silent))

    class FakeModel:
        on_stream = None

        def __init__(self):
            self.calls = 0

        async def request(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return {}, [ToolCall("c1", "Bash", ["printf nar1"])], "先看入口。"
            if self.calls == 2:
                return {}, [ToolCall("c2", "Bash", ["printf nar2"])], "这里接着读。"
            if self.calls <= 10:
                return {}, [ToolCall(f"c{self.calls}", "Bash", ["printf silent"])], ""
            return {"role": "assistant", "content": "改完了。"}, [], "改完了。"

        def estimated_request_tokens(self, messages, tools=None):
            return 10

    loop.agent.model = FakeModel()

    loop.presentation.user_turn_rule()
    assert await loop.agent.run("x") == "改完了。"

    assert len(rules) == 2  # second narration and the silent run
    assert rules[0] >= loop.presentation.MIN_ROWS_BETWEEN_RULES
    assert rules[1] >= loop.presentation.MIN_ROWS_BETWEEN_RULES
    assert silences == [False, False, *[True] * 8]  # narration batches voiced, the rest silent


async def test_resumed_session_draws_user_narration_and_silent_batch_rules(tmp_path):
    """A resumed session replays its turns with the same phase rules the live run drew: the user's
    message opens a turn with whitespace, interim narration closes with a rule once far enough from
    the boundary above, and a silent run of tool batches closes with the batch rule -- even though
    the engine never runs again."""
    from wizolt.session import ToolResultRecord

    def rules_for(messages, records):
        loop = _colored_loop(tmp_path)
        s = loop.session
        s.resumed = True
        s.transcript_messages = messages
        s.tool_records = records
        rules = []
        real_rule = loop.presentation.ui.emit_phase_rule
        loop.presentation.ui.emit_phase_rule = lambda: (rules.append(loop.presentation.ui.rows_since_rule), real_rule())
        real_seam = loop.presentation.ui.emit_run_seam
        loop.presentation.ui.emit_run_seam = lambda style: (style() == "rule" and rules.append(loop.presentation.ui.rows_since_rule), real_seam(style))
        loop.resume.render_resumed_session()
        return rules

    tool_call = lambda i: {"id": f"c{i}", "type": "function", "function": {"name": "Bash", "arguments": json.dumps([f"printf {i}"])}}
    record = lambda: ToolResultRecord(key="tr.1", name="Bash", args=[["printf x"]], output="x")

    # A short exchange needs no separator below its shaded user message.
    rules = rules_for(
        [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "answer"}],
        [],
    )
    assert len(rules) == 0

    # Interim narration opens with a rule once it is far enough from the boundary above: the first
    # narration lands too close to the user's message to draw, the second one clears it. A list
    # keeps its per-item lines instead of being folded into one paragraph by markdown.
    rules = rules_for(
        [
            {"role": "user", "content": "q1"},
            {
                "role": "assistant",
                "content": "- " + "\n- ".join(f"point {i}" for i in range(5)),
                "tool_calls": [tool_call(1)],
            },
            {"role": "assistant", "content": "later narration", "tool_calls": [tool_call(2)]},
            {"role": "assistant", "content": "answer"},
        ],
        [record(), record()],
    )
    assert len(rules) == 1
    assert rules[0] >= Presentation.MIN_ROWS_BETWEEN_RULES  # the second narration's rule is far enough

    # Four silent batches of one-line calls are four packed rows: long enough by the batch count,
    # but too close to the user message to part anything, so no rule is drawn.
    silent_run = lambda batches: [
        {"role": "user", "content": "q1"},
        *[{"role": "assistant", "content": "", "tool_calls": [tool_call(i)]} for i in range(1, batches + 1)],
        {"role": "assistant", "content": "answer"},
    ]
    assert len(rules_for(silent_run(4), [record()] * 4)) == 0

    # Once the same silence has filled enough rows, the batch rule closes it.
    rules = rules_for(silent_run(8), [record()] * 8)
    assert len(rules) == 1
    assert rules[0] >= Presentation.MIN_ROWS_BETWEEN_RULES  # the silent run's rule is far enough
