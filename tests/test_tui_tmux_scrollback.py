"""Real-tmux acceptance test for transcript integrity across repeated resizes.

`KNOWN_ISSUES.md` requires this test to exist, and requires it to be real: a deterministic
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
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux")

WIDE, NARROW = 100, 62
TALL, SHORT = 30, 18
CYCLES = 30
SEED_LINES = 20
# Enough output to outlast the resize loop and still finish inside the test, so the run covers
# both resizing mid-stream and a settled transcript to compare against.
MARKERS = 200
DRIVER = Path(__file__).with_name("tmux_driver.py")


def tmux(*args: str, check: bool = True) -> str:
    result = subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@pytest.fixture(params=["on", "off"], ids=["alt-screen-on", "alt-screen-off"])
def pane(tmp_path, request):
    # One session per test. These run under xdist, and a shared name makes two workers fight
    # over the same pane, which looks exactly like the corruption the test is meant to detect.
    session = f"wizolt-{request.node.name[:32]}-{os.getpid()}"
    tmux("kill-session", "-t", session, check=False)
    tmux("new-session", "-d", "-s", session, "-x", str(WIDE), "-y", str(TALL), "-c", str(tmp_path), "sh")
    # Both halves of acceptance criterion 11. Nothing here uses the alternate screen, so the
    # projection must behave identically either way -- and a terminal with it disabled is
    # exactly where a stray 1049 would go unnoticed until it ate someone's screen.
    tmux("set-option", "-t", session, "-w", "alternate-screen", request.param)
    time.sleep(0.4)
    try:
        yield Pane(session, tmp_path)
    finally:
        tmux("kill-session", "-t", session, check=False)


class Pane(NamedTuple):
    session: str
    path: Path

    def send(self, keys: str) -> None:
        tmux("send-keys", "-t", self.session, keys, "Enter")

    def resize(self, width: int, height: int) -> None:
        tmux("resize-window", "-t", self.session, "-x", str(width), "-y", str(height))

    def capture(self) -> list[str]:
        return tmux("capture-pane", "-t", self.session, "-p", "-S", "-").split("\n")


def _wait_for_markers(log: Path, count: int, timeout: float = 60.0) -> None:
    """Block until the driver has written at least `count` markers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists() and log.read_text().count("wrote MARKER-") >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"driver wrote fewer than {count} markers within {timeout}s")


def cycle_size(cycle: int) -> tuple[int, int]:
    return NARROW if cycle % 2 else WIDE, SHORT if cycle % 4 >= 2 else TALL


def test_transcript_survives_repeated_resize_cycles(pane):
    log = pane.path / "write.log"
    # Seed the pane so the app starts partway down a screen that already has content, the way a
    # real session does. Whether these survive is not asserted: see the module docstring.
    pane.send(f"i=1; while [ $i -le {SEED_LINES} ]; do echo SHELL-$i; i=$((i+1)); done")
    time.sleep(0.8)
    pane.send(f"{sys.executable} {DRIVER} {MARKERS} 0.05 {log}")
    _wait_for_markers(log, 1)

    for cycle in range(CYCLES):
        pane.resize(*cycle_size(cycle))
        # Alternate settled resizes with rapid ones: the failure modes differ. A slow cycle lets
        # a full repaint land between resizes, a fast one leaves streaming output mid-flight.
        time.sleep(0.12 if cycle % 3 else 0.4)

    pane.resize(WIDE, TALL)
    # Let the driver finish before reading anything. Capturing while it still emits makes markers
    # written after the capture look destroyed, which under load is the difference between this
    # test passing alone and failing beside the rest of the suite.
    _wait_for_markers(log, MARKERS)
    time.sleep(1.2)
    lines = pane.capture()

    text = "\n".join(lines)
    seen = Counter(int(m) for m in re.findall(r"MARKER-(\d+)", text))
    written = {int(m) for m in re.findall(r"wrote MARKER-(\d+)", log.read_text())}
    assert written, "the driver never emitted anything; the acceptance run proves nothing"

    destroyed = sorted(written - set(seen))
    assert not destroyed, f"markers written but no longer in scrollback: {destroyed[:10]}"
    duplicated = sorted(marker for marker, count in seen.items() if count > 1)
    assert not duplicated, f"markers present more than once: {duplicated[:10]}"

    prompts = [line for line in lines if line.startswith("> ")]
    assert len(prompts) <= 1, f"stale prompt copies left in scrollback: {len(prompts)}"

    # The driver opens and closes a selector on its own timer while all of this happens. Assert it
    # actually did: a run where the selector never opened proves nothing about selectors.
    cycles = log.read_text().count("selector cycle")
    assert cycles >= 2, f"the selector opened {cycles} times; this run did not exercise it"


def _blank_rows_after(pane, cycles: int) -> int:
    """Emit a fixed transcript, then resize `cycles` times, and count the blank rows left."""
    pane.send(f"{sys.executable} {DRIVER} 40 0.03 {pane.path / f'blank-{cycles}.log'}")
    # Let the whole transcript land before resizing, so both arms compare the same content and
    # the only difference between them is how many times the pane was resized.
    time.sleep(3.5)
    for cycle in range(cycles):
        pane.resize(*cycle_size(cycle))
        time.sleep(0.15)
    pane.resize(WIDE, TALL)
    time.sleep(1.2)
    return sum(1 for line in pane.capture() if not line.strip())


def test_blank_rows_do_not_grow_with_resize_cycles(pane):
    """The shipped implementation accumulates blank rows per resize; this pins that down.

    An absolute bound cannot express the property, because `UiPrinter` puts real blank rows
    between blocks and their number tracks the transcript. What must not happen is blank rows
    tracking the number of *resizes*, so the same transcript is resized a few times and many
    times and the two counts are compared.
    """
    few = _blank_rows_after(pane, 4)
    tmux("send-keys", "-t", pane.session, "C-c")
    time.sleep(0.6)
    pane.send("clear")
    time.sleep(0.5)
    many = _blank_rows_after(pane, CYCLES)

    assert many <= few + 2, f"blank rows grew with resize count: {few} after 4 cycles, {many} after {CYCLES}"
