"""Real-tmux acceptance test for transcript integrity across repeated resizes.

The Terminal boundary section in `DESIGN.md` requires a real-tmux test: a deterministic
terminal model cannot reproduce tmux's reflow across multiple width transitions, its persistent
wrap flags, or the ordering between resize, SIGWINCH and redraw. Every earlier attempt at this
problem looked correct against a model or a single clean resize and failed on the second one.

What is asserted, and why each one is separate:

* Every marker the driver wrote is still present, exactly once. Under-erasing leaves duplicate
  fragments; over-erasing and mis-positioned writes destroy transcript. Counting distinct
  markers catches the first, comparing against the driver's own write log catches the second.
* Blank rows do not grow with the number of resize cycles. This is the artifact the shipped
  implementation accumulates: erasing clears cells but does not remove rows from the text flow.
* The prompt and status rows appear once. A stale copy left behind by a reflow shows up here.

Pre-existing shell history is deliberately *not* asserted to survive. A width change rebuilds
the terminal from the transcript and purges scrollback; see `wizolt/tui/scrollback.py` for why
that trade is taken rather than guessing how many physical rows to delete.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from prompt_toolkit.utils import get_cwidth

# Skipping is right on a developer machine without tmux, but in CI a silent skip is how this
# test quietly stops guarding anything -- violating the terminal contract in `DESIGN.md`. CI
# installs tmux, so if it is missing there, that is a broken pipeline, not an absent tool.
if shutil.which("tmux") is None and os.environ.get("CI"):
    raise RuntimeError("CI must run the real-tmux acceptance tests, but tmux is not installed")

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux")

WIDE, NARROW = 100, 62
TALL, SHORT = 30, 18
CYCLES = 30
SEED_LINES = 20
# Enough output to outlast the resize loop and still finish inside the test, so the run covers
# both resizing mid-stream and a settled transcript to compare against.
MARKERS = 200
DRIVER = Path(__file__).with_name("tmux_driver.py")
# Each test worker owns a server; a crash or personal tmux config must not affect other panes
# (especially the developer's own session). The last session's cleanup also stops this server.
SOCKET = f"wizolt-tests-{os.getpid()}"


def tmux(*args: str, check: bool = True) -> str:
    result = subprocess.run(["tmux", "-L", SOCKET, "-f", os.devnull, *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@pytest.fixture(params=["on", "off"], ids=["alt-screen-on", "alt-screen-off"])
def panes(tmp_path, request):
    """Hand out fresh tmux panes, each torn down at the end of the test.

    A test that needs two runs gets two panes rather than trying to reclaim one. Killing the
    driver in place meant sending Ctrl-C, which the selector swallows whenever it happens to be
    open, leaving the pane owned by an app that never exited and every later keystroke going to
    it instead of the shell.
    """
    created: list[str] = []

    def make() -> Pane:
        # One session per pane. These run under xdist, and a shared name makes two workers fight
        # over the same pane, which looks exactly like the corruption the test detects.
        session = f"wizolt-{request.node.name[:24]}-{os.getpid()}-{len(created)}"
        tmux("kill-session", "-t", session, check=False)
        tmux("new-session", "-d", "-s", session, "-x", str(WIDE), "-y", str(TALL), "-c", str(tmp_path), "sh")
        # Both halves of acceptance criterion 11. Nothing here uses the alternate screen, so the
        # projection must behave identically either way -- and a terminal with it disabled is
        # exactly where a stray 1049 would go unnoticed until it ate someone's screen.
        tmux("set-option", "-t", session, "-w", "alternate-screen", request.param)
        created.append(session)
        time.sleep(0.4)
        return Pane(session, tmp_path)

    try:
        yield make
    finally:
        for session in created:
            tmux("kill-session", "-t", session, check=False)


@pytest.fixture
def pane(panes):
    return panes()


class Pane(NamedTuple):
    session: str
    path: Path

    def send(self, keys: str) -> None:
        tmux("send-keys", "-t", self.session, keys, "Enter")

    def resize(self, width: int, height: int) -> None:
        tmux("resize-window", "-t", self.session, "-x", str(width), "-y", str(height))

    def capture(self) -> list[str]:
        return tmux("capture-pane", "-t", self.session, "-p", "-S", "-").split("\n")


@pytest.mark.parametrize("height", [12, 18, 30])
def test_long_inline_selector_keeps_context_visible(pane, height):
    pane.resize(WIDE, height)
    log = pane.path / "choices.log"
    pane.send("echo SHELL-CONTEXT")
    pane.send(f"{sys.executable} {DRIVER} 0 0 {log} choices")

    def visible_containing(needle):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            visible = tmux("capture-pane", "-t", pane.session, "-p")
            if needle in visible:
                return visible
            time.sleep(0.05)
        raise AssertionError(f"missing {needle!r} in visible pane:\n{visible}")

    visible_containing("WIZOLT-BANNER")
    for cycle in range(3):
        log.with_suffix(f".open-{cycle}").touch()
        visible_containing("provider-00")
        tmux("send-keys", "-t", pane.session, "G")
        opened = visible_containing("provider-79")
        banner_row = opened.splitlines().index("WIZOLT-BANNER")
        if cycle == 1:
            tmux("send-keys", "-t", pane.session, "/", "provider-42")
            visible_containing("provider-42")
            tmux("send-keys", "-t", pane.session, "Enter", "Enter")
        else:
            tmux("send-keys", "-t", pane.session, "Escape")
        deadline = time.monotonic() + 15
        while f"closed {cycle}:" not in log.read_text():
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        _settled_capture(pane)
        visible = visible_containing("tmux-driver")
        assert "WIZOLT-BANNER" in visible
        assert visible.splitlines().index("WIZOLT-BANNER") == banner_row, "closing the selector scrolled the context"
        assert "PROVIDER-COMMAND" in visible
        history = "\n".join(pane.capture())
        assert history.count("WIZOLT-BANNER") == 1
        assert history.count("PROVIDER-COMMAND") == 1
        assert "SHELL-CONTEXT" in history
    assert "closed 1: provider-42" in log.read_text()


def test_cli_startup_banner_survives_growing_and_shrinking_pane(pane):
    config = pane.path / "config.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{pane.path}/data"\n'
        '[provider]\nactive = "test"\n[provider.test]\n'
        'url = "http://127.0.0.1:9/v1"\nkey = "test-only"\nmodel = "tmux-startup-model"\n'
    )
    # Run the real CLI entry and startup handoff. Only background network checks are disabled;
    # no model request is made, and the session/config live entirely inside the temporary pane.
    entry = pane.path / "startup.py"
    entry.write_text(
        "from wizolt.cli.update import UpdateChecker\n"
        "from wizolt.providers.sync import CatalogRuntime\n"
        "from wizolt.__main__ import main\n"
        "UpdateChecker.load_cached = lambda self: False\n"
        "CatalogRuntime.refresh_due = lambda self: False\n"
        "main()\n"
    )
    pane.send(f"{sys.executable} {entry} --config {config}")
    deadline = time.monotonic() + 15
    while not any("tmux-startup-model" in line for line in pane.capture()):
        assert time.monotonic() < deadline, "CLI did not start"
        time.sleep(0.05)
    for cycle, (width, height) in enumerate([(140, 45), (62, 18), (100, 30)] * 3):
        pane.resize(width, height)
        # Editing (without submitting) confirms a fresh frame after each resize, rather than
        # accepting the previous size's capture while SIGWINCH is still being handled.
        tmux("send-keys", "-t", pane.session, "C-u")
        draft = f"startup-check-{cycle}"
        tmux("send-keys", "-t", pane.session, "-l", draft)
        deadline = time.monotonic() + 15
        while not any(draft in line for line in pane.capture()):
            assert time.monotonic() < deadline, "CLI did not render the new draft"
            time.sleep(0.05)
        lines = _settled_capture(pane)
        assert "\n".join(lines).count("/help for commands.") == 1
        assert sum(line == ">" or line.startswith("> ") for line in lines) == 1
        assert sum("tmux-startup-model" in line for line in lines) == 1


def _wait_for_markers(log: Path, count: int, timeout: float = 30.0) -> None:
    """Block until the driver has written at least `count` markers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists() and log.read_text().count("wrote MARKER-") >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"driver wrote fewer than {count} markers within {timeout}s (log exists: {log.exists()})")


@pytest.mark.parametrize("columns", [5, 7])
def test_physical_rows_matches_tmux_wide_glyph_and_tab_wrapping(pane, columns):
    from wizolt.tui.scrollback import physical_rows

    pane.resize(columns, TALL)
    payload = "\x1b[31m" + "中" * 9 + "\r\na\tbcd\r\n\x1b[0m"
    script = pane.path / "rows.py"
    script.write_text(
        "import sys, time\n"
        f"sys.stdout.write('\\x1b[H\\x1b[2J' + {payload!r} + 'END')\n"
        "sys.stdout.flush()\ntime.sleep(30)\n"
    )
    pane.send(f"{sys.executable} {script}")
    deadline = time.monotonic() + 15
    while True:
        lines = tmux("capture-pane", "-t", pane.session, "-p").splitlines()
        if "END" in lines:
            break
        assert time.monotonic() < deadline, lines
        time.sleep(0.05)
    assert lines.index("END") == physical_rows(payload, columns)


def _settled_capture(pane, attempts: int = 40) -> list[str]:
    """Capture once the pane stops changing.

    A fixed pause is a bet on how loaded the machine is; CI runs four workers on two cores and
    loses that bet. Comparing consecutive captures waits for the thing that actually matters.
    """
    previous = pane.capture()
    for _ in range(attempts):
        time.sleep(0.15)
        current = pane.capture()
        if current == previous:
            return current
        previous = current
    capture_path = pane.path / "unstable-capture.txt"
    capture_path.write_text("\n".join(previous))
    raise AssertionError(
        f"pane did not settle after {attempts} captures; last capture saved to {capture_path}\n"
        + "Last 40 rows:\n" + "\n".join(previous[-40:])
    )


def _stop_selectors(log: Path, timeout: float = 10.0) -> None:
    """Wait for the driver to close its selector and stop reopening it before final capture."""
    log.with_suffix(".settle").touch()
    deadline = time.monotonic() + timeout
    while "selectors stopped\n" not in log.read_text():
        assert time.monotonic() < deadline, "the driver did not settle its selector"
        time.sleep(0.05)


@pytest.mark.parametrize("stable", [False, True])
def test_capture_requires_a_stable_frame(tmp_path, monkeypatch, stable):
    frames = iter([["first"], ["second"], ["second" if stable else "third"]])
    pane = SimpleNamespace(path=tmp_path, capture=lambda: next(frames))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    if stable:
        assert _settled_capture(pane, attempts=2) == ["second"]
        assert not (tmp_path / "unstable-capture.txt").exists()
    else:
        with pytest.raises(AssertionError, match="pane did not settle"):
            _settled_capture(pane, attempts=2)
        assert (tmp_path / "unstable-capture.txt").read_text() == "third"


def cycle_size(cycle: int) -> tuple[int, int]:
    return NARROW if cycle % 2 else WIDE, SHORT if cycle % 4 >= 2 else TALL


def test_transcript_survives_repeated_resize_cycles(pane):
    log = pane.path / "write.log"
    # A real split and zoom toggle exercise tmux's pane reflow, not only window resizing.
    tmux("split-window", "-d", "-h", "-t", pane.session, "sh")
    tmux("resize-pane", "-Z", "-t", pane.session)
    # Seed the pane so the app starts partway down a screen that already has content, the way a
    # real session does. Whether these survive is not asserted: see the module docstring.
    pane.send(f"i=1; while [ $i -le {SEED_LINES} ]; do echo SHELL-$i; i=$((i+1)); done")
    time.sleep(0.8)
    pane.send(f"{sys.executable} {DRIVER} {MARKERS} 0.05 {log}")
    _wait_for_markers(log, 1)

    for cycle in range(CYCLES):
        pane.resize(*cycle_size(cycle))
        tmux("resize-pane", "-Z", "-t", pane.session)
        # Alternate settled resizes with rapid ones: the failure modes differ. A slow cycle lets
        # a full repaint land between resizes, a fast one leaves streaming output mid-flight.
        time.sleep(0.12 if cycle % 3 else 0.4)

    pane.resize(WIDE, TALL)
    # Let the driver finish before reading anything. Capturing while it still emits makes markers
    # written after the capture look destroyed, which under load is the difference between this
    # test passing alone and failing beside the rest of the suite.
    _wait_for_markers(log, MARKERS)
    _stop_selectors(log)
    lines = _settled_capture(pane)

    text = "\n".join(lines)
    seen = Counter(int(m) for m in re.findall(r"MARKER-(\d+)", text))
    written = {int(m) for m in re.findall(r"wrote MARKER-(\d+)", log.read_text())}
    assert written, "the driver never emitted anything; the acceptance run proves nothing"

    destroyed = sorted(written - set(seen))
    assert not destroyed, f"markers written but no longer in scrollback: {destroyed[:10]}"
    duplicated = sorted(marker for marker, count in seen.items() if count > 1)
    assert not duplicated, f"markers present more than once: {duplicated[:10]}"

    prompts = [line for line in lines if line == ">" or line.startswith("> ")]
    assert len(prompts) == 1, f"expected one prompt, found {len(prompts)}"
    assert sum("tmux-driver" in line for line in lines) == 1, "status missing or duplicated"

    # The driver opens and closes a selector on its own timer while all of this happens. Assert it
    # actually did: a run where the selector never opened proves nothing about selectors.
    cycles = log.read_text().count("selector cycle")
    assert cycles >= 2, f"the selector opened {cycles} times; this run did not exercise it"


def _blank_rows_after(pane, cycles: int) -> int:
    """Emit a fixed transcript, then resize `cycles` times, and count the blank rows left."""
    log = pane.path / f"blank-{cycles}.log"
    pane.send(f"{sys.executable} {DRIVER} 40 0.03 {log}")
    # Let the whole transcript land before resizing, so both arms compare the same content and
    # the only difference between them is how many times the pane was resized.
    _wait_for_markers(log, 40)
    # Settle the selector before resizing, not after. The driver opens it on its own timer, so a
    # resize can land while it is open; shrinking the pane then pushes its rows into native
    # history, where they stay. That leaves option rows in the capture no later close can remove,
    # and their rows in the count this returns. Selectors under resize are what
    # `test_transcript_survives_repeated_resize_cycles` is for; what this measures is resizes.
    _stop_selectors(log)
    for cycle in range(cycles):
        pane.resize(*cycle_size(cycle))
        time.sleep(0.15)
    pane.resize(WIDE, TALL)
    lines = _settled_capture(pane)
    assert not any(" option " in line for line in lines), "the selector was still visible:\n" + "\n".join(lines)
    assert sum(line == ">" or line.startswith("> ") for line in lines) == 1, "prompt missing or duplicated"
    assert Counter(re.findall(r"MARKER-(\d+)", "\n".join(lines))) == Counter(f"{index:04d}" for index in range(1, 41))
    return sum(1 for line in lines if not line.strip())


def test_blank_rows_do_not_grow_with_resize_cycles(panes):
    """The shipped implementation accumulates blank rows per resize; this pins that down.

    An absolute bound cannot express the property, because `UiPrinter` puts real blank rows
    between blocks and their number tracks the transcript. What must not happen is blank rows
    tracking the number of *resizes*, so the same transcript is resized a few times and many
    times and the two counts are compared.
    """
    few = _blank_rows_after(panes(), 4)
    many = _blank_rows_after(panes(), CYCLES)

    assert many <= few + 2, f"blank rows grew with resize count: {few} after 4 cycles, {many} after {CYCLES}"


@pytest.mark.parametrize("markers", [1, 40], ids=["short-history", "scrollback"])
def test_completed_rules_fit_each_width_without_losing_text(pane, markers):
    log = pane.path / "rules.log"
    pane.send(f"{sys.executable} {DRIVER} {markers} 0.01 {log} rules")
    deadline = time.monotonic() + 30
    while not log.exists() or "rules complete" not in log.read_text():
        assert time.monotonic() < deadline, "the driver did not finish its rules"
        time.sleep(0.05)

    # Inspect the narrow state too: returning to the original width hides wrapped rule tails.
    for width in (WIDE, NARROW, 37, WIDE, NARROW):
        pane.resize(width, TALL)
        lines = _settled_capture(pane)
        joined = "".join(lines)
        for marker in ("USER-BEGIN", "USER-END", "ANSWER-BEGIN", "ANSWER-END", "done in 1m05s", "[worker] 完成"):
            assert joined.count(marker) == 1, (width, marker, lines)
        rules = [line for line in lines if "─" in line]
        assert len(rules) == 3, f"expected three single-row rules at width {width}: {rules}"
        assert all(get_cwidth(line) == width for line in rules), (width, rules)



def test_exit_drains_output_accepted_before_the_last_render(pane):
    log = pane.path / "exit.log"
    pane.send(f"{sys.executable} {DRIVER} 1 0.01 {log} exit")
    deadline = time.monotonic() + 30
    while not log.exists() or "driver exited" not in log.read_text():
        assert time.monotonic() < deadline, "the application did not exit"
        time.sleep(0.05)
    text = "\n".join(_settled_capture(pane))
    for marker in ("MARKER-0001", "EXIT-FIRST", "EXIT-SECOND"):
        assert text.count(marker) == 1, (marker, text)
    assert text.index("EXIT-FIRST") < text.index("EXIT-SECOND"), text
