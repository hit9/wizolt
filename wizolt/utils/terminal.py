"""What the terminal says about its own background, asked once at startup."""

from __future__ import annotations

import functools
import os
import re
import sys
import time

# `ESC ] 11 ; rgb:RRRR/GGGG/BBBB`, each channel one to four hex digits, ended by BEL or ST.
BACKGROUND_REPLY = re.compile(rb"\x1b\]11;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})")
# The primary device attributes reply, `ESC [ ? ... c`.
ATTRIBUTES_REPLY = re.compile(rb"\x1b\[\?[0-9;]*c")
# Only a terminal that answers neither question waits this long; over SSH a reply takes a round trip.
TIMEOUT = 0.2
_UNQUERIED = object()
_reported: tuple[int, int, int] | None | object = _UNQUERIED


def remember_background(color: tuple[int, int, int] | None) -> None:
    """Store the live input reader's reply; later theme changes must never read stdin again."""
    global _reported
    _reported = color
    background.cache_clear()


@functools.cache
def background() -> tuple[int, int, int] | None:
    """The terminal's background as 0-255 channels, asked with OSC 11, or None if it cannot say.

    A primary device attributes request follows the question. Every terminal answers that one and
    answers in order, so a terminal that ignores OSC 11 costs one round trip instead of the whole
    timeout, and no late reply is left to arrive in the prompt. Nothing is asked while keys are
    already waiting: reading the reply would swallow what the user typed ahead.
    """
    if _reported is not _UNQUERIED:
        return _reported if isinstance(_reported, tuple) else None
    try:
        import select
        import termios
        import tty

        if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("TERM", "dumb") == "dumb":
            return None
        stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
        if select.select([stdin], [], [], 0)[0]:
            return None
        saved = termios.tcgetattr(stdin)
    except (ImportError, OSError, ValueError):
        return None
    reply = b""
    try:
        tty.setcbreak(stdin, termios.TCSANOW)
        os.write(stdout, b"\x1b]11;?\x1b\\\x1b[c")
        deadline = time.monotonic() + TIMEOUT
        while not ATTRIBUTES_REPLY.search(reply):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([stdin], [], [], remaining)[0]:
                break
            reply += os.read(stdin, 1024)
    except OSError:
        return None
    finally:
        termios.tcsetattr(stdin, termios.TCSANOW, saved)
    return parse_background(reply)


def parse_background(reply: bytes) -> tuple[int, int, int] | None:
    match = BACKGROUND_REPLY.search(reply)
    if match is None:
        return None
    red, green, blue = (int(channel, 16) * 255 // (16 ** len(channel) - 1) for channel in match.groups())
    return red, green, blue


def is_light(color: tuple[int, int, int]) -> bool:
    red, green, blue = color
    return 0.299 * red + 0.587 * green + 0.114 * blue > 127.5
