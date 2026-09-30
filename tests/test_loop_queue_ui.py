"""loop queue ui (split from tests/test_loop_commands.py)."""

import asyncio
import hashlib
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from agent_harness import call, queue, session

import wizolt.ui.cli.loop as loop_module
from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.agent.runner import ToolRunner
from wizolt.image import IMAGE_MARKER, ImageRef, UserInput
from wizolt.paste import PASTE_MARKER, PasteRef
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.commands import COMMAND_NAMES
from wizolt.ui.tui import TuiApp


def test_queue_live_region_shows_divider_and_pending(tmp_path):
    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda prompt: "", output_fn=lambda text: None)
    queue(s, "run tests", "then push")

    sent, waiting = loop.view.followup_fragments()
    text = "".join(t for _, t in [*sent, *waiting])
    assert "2 queued" in text and "working" in text
    assert "+ run tests" in text and "+ then push" in text
    # A blank row lifts the queued block off the divider, so the queue reads as its own region.
    waiting_lines = "".join(t for _, t in waiting).splitlines()
    assert waiting_lines[waiting_lines.index("+ run tests") - 1] == ""

    claimed = s.claim_user_inputs()
    sent, waiting = loop.view.followup_fragments()
    sent_text = "".join(t for _, t in sent)
    waiting_text = "".join(t for _, t in waiting)
    assert "• run tests" in sent_text and "• then push" in sent_text
    assert "queued" not in waiting_text
    assert "run tests" not in waiting_text and "then push" not in waiting_text

    queue(s, "after claim")
    sent, waiting = loop.view.followup_fragments()
    assert "• run tests" in "".join(t for _, t in sent)
    assert "1 queued" in "".join(t for _, t in waiting)
    assert "+ after claim" in "".join(t for _, t in waiting)

    s.release_user_inputs()
    sent, waiting = loop.view.followup_fragments()
    assert sent == []
    released = "".join(t for _, t in waiting)
    assert "3 queued" in released
    assert "+ run tests" in released and "+ then push" in released and "+ after claim" in released

    s.claim_user_inputs()
    s.acknowledge_user_inputs(claimed)
    sent, waiting = loop.view.followup_fragments()
    assert "run tests" not in "".join(t for _, t in [*sent, *waiting])
    assert "then push" not in "".join(t for _, t in [*sent, *waiting])

    # The divider animates a comet head across the dashes while its label remains stable.
    with pytest.MonkeyPatch.context() as mp:
        seen_head = False
        for tick in range(200):
            mp.setattr(time, "monotonic", lambda tick=tick: tick * 0.1)
            fragments = loop.view.queue_divider_fragments()
            seen_head = seen_head or any(style == "class:divider.glow0" and "─" in text for style, text in fragments)
            assert any(style == "class:divider.working" and text.startswith("working") for style, text in fragments)
            assert all(not style.startswith("class:divider.glow") or set(text) == {"─"} for style, text in fragments)
        assert seen_head

    s.pending_user_inputs = []
    sent, waiting = loop.view.followup_fragments()
    empty = "".join(t for _, t in [*sent, *waiting])
    assert "working" in empty and "queued" not in empty and "run tests" not in empty


def test_queued_inputs_keep_chips_out_of_the_live_region_and_recall_restores_them(tmp_path):
    """A folded paste or an attached image is a chip in the queue's rows -- held back with Tab or
    sent as a follow-up -- and stays one once claimed and echoed above the divider. The entry's
    flattened text is for the model and the snapshot; a queued paste body of many lines would
    otherwise flood the live region and bury the divider."""
    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda prompt: "", output_fn=lambda text: None)
    body = "\n".join(f"line {index}" for index in range(40))
    paste = PasteRef(text=body + "\n", lines=41, chars=len(body) + 1)
    # A stored asset whose digest is its ref, the way a real attachment arrives.
    blob = b"image bytes"
    ref = hashlib.sha256(blob).hexdigest()
    image = ImageRef(ref=ref, name="shot.png", media_type="image/png", width=2, height=2, size=len(blob))
    asset = Path(s.images.asset_path(image))
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(blob)
    s.enqueue_user_input(UserInput(f"review {PASTE_MARKER} {IMAGE_MARKER}", pastes=(paste,), images=(image,)), next_turn=True)
    s.enqueue_user_input(UserInput(f"then {PASTE_MARKER}", pastes=(paste,)))

    _sent, waiting = loop.view.followup_fragments()
    rows = "".join(text for _, text in waiting)
    assert rows.count(paste.label(1)) == 2  # one chip per queued paste
    assert "[Image #1 · shot.png]" in rows
    assert "line 0" not in rows and "line 39" not in rows  # no paste body in the live region
    assert any(style == "class:divider.working" for style, _ in waiting)

    s.claim_user_inputs()
    sent, _waiting = loop.view.followup_fragments()
    claimed = "".join(text for _, text in sent)
    assert paste.label(1) in claimed and "line 39" not in claimed  # the echoed row is a chip too

    recalled = loop.recall_pending_input(lambda: None)
    assert isinstance(recalled, UserInput) and recalled.pastes == (paste,)  # the editor gets its chips back


def divider_glow_steps(fragments):
    """The comet's glow step at each rendered cell, including its hidden label span."""
    steps = []
    for style, text in fragments:
        if text and set(text) == {"─"} and (style == "class:queue.rule" or style.startswith("class:divider.glow")):
            step = int(style.removeprefix("class:divider.glow")) if style.startswith("class:divider.glow") else None
            steps.extend([step] * len(text))
        else:
            steps.extend([None] * len(text))
    return steps


def test_divider_formula_moves_both_ways_and_restarts_with_the_turn(tmp_path, monkeypatch):
    from wizolt.ui.render import Theme

    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size((100, 20)))
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), input_fn=lambda prompt: "", output_fn=lambda text: None)
    view = loop.view
    formula = "exp(-((x - pingpong(t * 20, w + 12) + 6) / 4) ** 2)"
    assert not view.presentation.status_bar.layout.configure({"sweep": formula}, Theme.bar_styles)
    heads = []
    for now in (2, 3, 4, 7, 8, 9):
        monkeypatch.setattr(time, "monotonic", lambda now=now: now)
        steps = divider_glow_steps(view.sweep_divider_fragments("working"))
        heads.append(min(range(len(steps)), key=lambda index: (steps[index] is None, steps[index])))
    assert heads[0] < heads[1] < heads[2]
    assert heads[3] > heads[4] > heads[5]
    view.presentation.status_bar.started_at = 100.0
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    assert all(step is None for step in divider_glow_steps(view.sweep_divider_fragments("working")))


def test_divider_glow_fades_between_cells_and_every_step_has_a_style(tmp_path):
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), input_fn=lambda prompt: "", output_fn=lambda text: None)
    styled = {rule for rule, _ in loop.view.style().style_rules}

    with pytest.MonkeyPatch.context() as mp:
        seen = set()
        for tick in range(400):
            mp.setattr(time, "monotonic", lambda tick=tick: 1000.0 + tick * 0.017)
            seen.update(step for step in divider_glow_steps(loop.view.queue_divider_fragments()) if step is not None)

    # Every shade the comet can emit must exist in the style, or those cells render as plain text.
    assert seen and all(f"divider.glow{step}" in styled for step in seen)
    # A head resting between two cells lights both at the same reduced shade instead of snapping
    # onto the nearer one, which is what keeps the motion smooth when a frame arrives late.
    label = "working"
    trail_start = 3 + len(label) + 2
    with pytest.MonkeyPatch.context() as mp:
        from wizolt.ui.render import Theme

        target = trail_start + 3.5
        assert not loop.presentation.status_bar.layout.configure({"sweep": f"exp(-((x - {target}) / 2) ** 2)"}, Theme.bar_styles)
        steps = divider_glow_steps(loop.view.sweep_divider_fragments(label))

    assert steps[trail_start + 3] == steps[trail_start + 4]
    assert steps[trail_start + 2] == steps[trail_start + 5] > steps[trail_start + 3]


def test_live_bash_output_stays_above_working_divider_and_queue(tmp_path):
    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda prompt: "", output_fn=lambda text: None)
    queue(s, "follow up")
    loop.presentation.live_preview.active = True
    loop.presentation.live_preview.text = "live output"
    loop.presentation.live_preview.started_at = time.monotonic()

    text = "".join(fragment for _, fragment in loop.view.tui_activity_fragments())

    assert text.index("live output") < text.index("working") < text.index("+ follow up")
    assert "live output\n\n───" in text


def test_queue_flush_moves_messages_into_log(tmp_path, monkeypatch):
    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda prompt: "", output_fn=lambda _text: None)
    # The agent's flush hook is wired to move queued messages up into the scrollback log.
    assert loop.agent.hooks.on_queue_flush == loop.flush_queued_to_log

    echoed = []
    monkeypatch.setattr(loop_module, "print_formatted_text", lambda value, **_kwargs: echoed.append("".join(text for _, text in value)))

    loop.flush_queued_to_log(["do a thing", "then verify", "  "])

    assert echoed == ["\n• do a thing\n\n• then verify\n\n"]


async def test_queue_command_runs_readonly(tmp_path):
    """A read-only slash command in the queue runs immediately and is not queued for the LLM."""
    s = session(tmp_path)
    out = []
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda *a, **k: "", output_fn=out.append)

    await loop.run_queued_command("/status")

    assert s.pending_user_inputs == []
    assert out and not any("unavailable" in t for t in out)


@pytest.mark.parametrize("text", ["/config", "/catalog", "/catalog status"])
async def test_queue_command_runs_the_other_read_only_reports(tmp_path, text):
    """Reading how the session is set up costs the running turn nothing, so it needs no interrupt."""
    s = session(tmp_path)
    out = []
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda *a, **k: "", output_fn=out.append)

    await loop.run_queued_command(text)

    assert s.pending_user_inputs == []
    assert out and not any("available while the agent is working" in t for t in out)


@pytest.mark.parametrize("text", ["/catalog sync", "/model"])
async def test_queue_command_still_refuses_a_mutating_form(tmp_path, text):
    """A queue-safe command is only safe in the form that reports: its mutating form still waits,
    whether it picks a new model or syncs a catalog the running turn resolves its policy
    against."""
    s = session(tmp_path)
    out = []
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda *a, **k: "", output_fn=out.append)

    await loop.run_queued_command(text)

    assert any("available while the agent is working" in t for t in out)
    assert s.pending_user_inputs == []


async def test_queue_command_runs_yolo_toggle(tmp_path):
    """/yolo flips the runtime flag from the queue while the agent works."""
    s = session(tmp_path)
    out = []
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda *a, **k: "", output_fn=out.append)

    before = s.settings.yolo
    await loop.run_queued_command("/yolo")

    assert s.settings.yolo is (not before)
    assert s.pending_user_inputs == []


async def test_hints_command_is_removed(tmp_path):
    s = session(tmp_path)
    out = []
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda *a, **k: "", output_fn=out.append)

    # Every /hints spelling follows the normal unknown-command path; there is no toggle left.
    for variant in ("/hints", "/hints on", "/hints off"):
        handled, _ = await loop.command(variant)
        assert handled is True
        assert out[-1].endswith("Unknown command: /hints")
    assert "/hints" not in loop_module.COMMAND_LOOKUP
    assert "/hints" not in COMMAND_NAMES


async def test_queue_command_rejects_mutating(tmp_path):
    """A state-mutating slash command is refused while the agent works, not queued or run."""
    s = session(tmp_path)
    out = []
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda *a, **k: "", output_fn=out.append)

    await loop.run_queued_command("/model")

    assert s.pending_user_inputs == []
    assert any("unavailable while the agent is working" in t for t in out)


async def test_queue_command_rejects_mutating_mcp_subcommand(tmp_path):
    """Read-only /mcp is allowed; mutating subcommands like connect are refused."""
    s = session(tmp_path)
    out = []
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda *a, **k: "", output_fn=out.append)

    await loop.run_queued_command("/mcp connect test")

    assert any("read-only /mcp" in t for t in out)


async def test_tool_input_without_tui_uses_injected_input(tmp_path):
    """Without a TUI there is no prompt on the loop, so the injected blocking reader runs on a
    worker and its answer is awaited like any other."""
    s = session(tmp_path)
    calls = []
    loop = CommandLoop(
        Agent(s, output_fn=lambda text: None),
        input_fn=lambda prompt: calls.append(prompt) or "y",
        output_fn=lambda text: None,
    )

    assert await loop.tool_input("[Y/n or reason] ") == "y"

    assert calls == ["[Y/n or reason] "]


@pytest.mark.parametrize("reader", ["chat", "tool"])
def test_cancelled_injected_reader_does_not_hold_asyncio_run(tmp_path, reader):
    """A synchronous embedding callback cannot be interrupted, but it must not keep the CLI's
    default executor alive after the coroutine awaiting it has been cancelled."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors = []

    def input_fn(_prompt):
        started.set()
        release.wait()
        return "late"

    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda _text: None), input_fn=input_fn, output_fn=lambda _text: None)

    async def exercise():
        pending = asyncio.create_task(loop.read_input("") if reader == "chat" else loop.tool_input("approve"))
        while not started.is_set():
            await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    def run():
        try:
            asyncio.run(exercise())
        except BaseException as error:  # noqa: BLE001 - forward failures from the runtime thread.
            errors.append(error)
        finally:
            finished.set()

    runtime = threading.Thread(target=run)
    runtime.start()
    try:
        assert started.wait(1)
        assert finished.wait(1), "asyncio.run waited for the cancelled input callback"
    finally:
        release.set()
        runtime.join(1)
    assert errors == []


async def test_default_pipe_input_is_loop_driven_and_preserves_following_lines(tmp_path, monkeypatch):
    """The process stdin path reads readiness itself: no input() worker is left behind, and one
    kernel read may safely supply more than one prompt."""
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)
    try:
        os.write(write_fd, b"first\nsecond\n")
        assert await loop.read_input("") == "first"
        assert await loop.read_input("") == "second"
    finally:
        os.close(write_fd)
        stdin.close()


async def test_cancelling_default_pipe_input_removes_the_reader(tmp_path, monkeypatch):
    """A pipe whose writer remains open cannot hold asyncio.run's executor during shutdown."""
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)
    read = asyncio.create_task(loop.read_input(""))
    await asyncio.sleep(0)
    read.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await read
        assert os.get_blocking(read_fd)
    finally:
        os.close(write_fd)
        stdin.close()


async def test_default_pipe_input_restores_blocking_when_reader_registration_fails(tmp_path, monkeypatch):
    """Embedding failures must not leave the process stdin descriptor in non-blocking mode."""
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    event_loop = asyncio.get_running_loop()

    def fail_add_reader(*_args):
        raise RuntimeError("unsupported reader")

    monkeypatch.setattr(event_loop, "add_reader", fail_add_reader)
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), output_fn=lambda text: None)
    try:
        with pytest.raises(RuntimeError, match="unsupported reader"):
            await loop.read_input("")
        assert os.get_blocking(read_fd)
    finally:
        os.close(write_fd)
        stdin.close()


async def test_default_pipe_input_restores_blocking_when_reader_removal_fails(tmp_path, monkeypatch):
    """Reader cleanup errors may propagate, but restoration of the shared descriptor may not."""
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    event_loop = asyncio.get_running_loop()
    remove_reader = event_loop.remove_reader

    def fail_after_remove(fd):
        remove_reader(fd)
        raise RuntimeError("reader cleanup failed")

    monkeypatch.setattr(event_loop, "remove_reader", fail_after_remove)
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda text: None), output_fn=lambda text: None)
    try:
        os.write(write_fd, b"answer\n")
        with pytest.raises(RuntimeError, match="reader cleanup failed"):
            await loop.read_input("")
        assert os.get_blocking(read_fd)
    finally:
        os.close(write_fd)
        stdin.close()


async def test_tool_runner_edit_approval_prints_full_inline_preview(tmp_path, monkeypatch):
    s = session(tmp_path)
    outputs = []
    runner = ToolRunner(s, ContextManager(s), input_fn=lambda prompt: "y", output_fn=lambda text: outputs.append(str(text)))
    content = "".join(f"line {index}\n" for index in range(50))

    await runner.run([call("Edit", ["new.txt", "", [{"op": "create", "content": content}]])])

    assert outputs[0].startswith("  Edit  new.txt\n    │ --- ")
    assert "+line 49" in outputs[0]
    assert "preview truncated" not in outputs[0]
    assert any("[approved]" in output for output in outputs)


def test_queue_live_region_marks_worker_followups(tmp_path):
    """Follow-ups queued for a delegating worker are told apart by their marker's color alone --
    the status bar is where the worker is named -- count as queued, and move above the divider
    once the worker's request claims them."""
    from wizolt.config import Config
    from wizolt.session import Session

    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda prompt: "", output_fn=lambda text: None)
    worker = Session(cwd=s.cwd, config=Config(), settings=s.settings, uid=s.uid + ".w", listed=False)
    loop.session.worker = worker
    worker._active_turn_messages.append({"role": "user", "content": "order"})
    worker.enqueue_user_input("check the queue")

    sent, waiting = loop.view.followup_fragments()
    waiting_text = "".join(t for _, t in waiting)
    assert "+ check the queue" in waiting_text
    assert ("class:divider.worker", "+ ") in waiting  # the worker's color on the marker
    assert "worker" not in waiting_text  # colored, never named, below the divider
    assert "1 queued" in waiting_text

    worker.claim_user_inputs()
    sent, waiting = loop.view.followup_fragments()
    sent_text = "".join(t for _, t in sent)
    waiting_text = "".join(t for _, t in waiting)
    assert "\u2022 check the queue" in sent_text and "worker" not in sent_text
    assert ("class:divider.worker", "\u2022 ") in sent
    assert "queued" not in waiting_text


def test_worker_queue_hint_names_where_the_text_went(tmp_path):
    """A follow-up waiting on the worker is not recalled by `↑`, so the running hint says so
    instead of claiming the queue is empty."""
    from wizolt.config import Config
    from wizolt.session import Session

    s = session(tmp_path)
    loop = CommandLoop(Agent(s, output_fn=lambda text: None), input_fn=lambda prompt: "", output_fn=lambda text: None)
    tui = TuiApp()
    loop.presentation.tui = tui
    tui.set_running("working")
    worker = Session(cwd=s.cwd, config=Config(), settings=s.settings, uid=s.uid + ".w", listed=False)
    s.worker = worker

    assert loop.view.tui_input_hint() == loop.view.QUEUE_EMPTY_HINT
    worker.enqueue_user_input("check the worker queue")
    assert loop.view.tui_input_hint() == loop.view.QUEUE_WORKER_HINT
    s.enqueue_user_input("and a parent one")  # the parent's own queue wins the recall promise
    assert loop.view.tui_input_hint() == loop.view.QUEUE_PENDING_HINT
