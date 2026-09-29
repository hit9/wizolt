"""Asking the terminal for its background, against a real pseudo-terminal."""

import os
import pty
import select
import termios
import threading
import time
from types import SimpleNamespace

import pytest

from wizolt.ui.render import Theme
from wizolt.utils import terminal

QUERY = b"\x1b]11;?\x1b\\\x1b[c"
ATTRIBUTES = b"\x1b[?62;22c"


@pytest.fixture
def tty(monkeypatch):
    """wizolt's stdin and stdout on the child side of a pty; the test plays the terminal."""
    parent, child = pty.openpty()
    stdin, stdout = os.fdopen(os.dup(child), "rb", buffering=0), os.fdopen(os.dup(child), "wb", buffering=0)
    # The module's own view of sys: pytest puts its capture back on the real sys.stdout per test.
    monkeypatch.setattr(terminal, "sys", SimpleNamespace(stdin=stdin, stdout=stdout))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(terminal, "_reported", terminal._UNQUERIED)
    terminal.background.cache_clear()
    yield parent, child
    terminal.background.cache_clear()
    for handle in (stdin, stdout):
        handle.close()
    os.close(parent)
    os.close(child)


def answer(parent, reply):
    """Answer the query once it arrives, as a terminal would, and hand back what was asked."""
    asked = bytearray()

    def respond():
        while not asked.endswith(QUERY):
            asked.extend(os.read(parent, 1024))
        os.write(parent, reply)

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    return thread, asked


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b"\x1b]11;rgb:fdfd/f6f6/e3e3\x1b\\" + ATTRIBUTES, (253, 246, 227)),  # ST-terminated, four digits
        (b"\x1b]11;rgb:28/28/28\x07" + ATTRIBUTES, (40, 40, 40)),  # BEL-terminated, two digits
    ],
)
def test_the_terminal_reports_its_background(tty, reply, expected):
    parent, _child = tty
    thread, asked = answer(parent, reply)

    assert terminal.background() == expected
    thread.join(timeout=1)
    assert bytes(asked) == QUERY


def test_a_terminal_that_ignores_the_question_costs_one_round_trip(tty):
    parent, _child = tty
    answer(parent, ATTRIBUTES)

    started = time.monotonic()
    assert terminal.background() is None
    assert time.monotonic() - started < terminal.TIMEOUT / 2


def test_keys_typed_ahead_are_left_for_the_prompt(tty):
    parent, child = tty
    os.write(parent, b"hello\n")  # a whole line: the pty is still in canonical mode
    time.sleep(0.05)

    assert terminal.background() is None
    assert select.select([child], [], [], 1)[0], "the typed-ahead line was swallowed"
    assert os.read(child, 1024) == b"hello\n"


def test_the_terminal_mode_is_restored(tty):
    parent, child = tty
    before = termios.tcgetattr(child)
    answer(parent, b"\x1b]11;rgb:0000/0000/0000\x1b\\" + ATTRIBUTES)

    assert terminal.background() == (0, 0, 0)
    assert termios.tcgetattr(child) == before


def test_nothing_is_asked_into_piped_output(tty, tmp_path):
    """`wizolt | tee log`: the question would land in the log, and nothing would answer it."""
    with open(tmp_path / "log", "wb") as piped:
        terminal.sys.stdout = piped

        assert terminal.background() is None
        assert piped.tell() == 0


def test_auto_follows_the_reported_background_over_colorfgbg(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "15;0")  # says dark
    monkeypatch.setattr(terminal, "background", lambda: (253, 246, 227))
    assert Theme.detect() == "light"
    monkeypatch.setattr(terminal, "background", lambda: (40, 40, 40))
    assert Theme.detect() == "dark"
    monkeypatch.setattr(terminal, "background", lambda: None)
    assert Theme.detect() == "dark"
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert Theme.detect() == "light"
