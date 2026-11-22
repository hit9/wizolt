"""Separate terminal query replies from editable input on the live VT stream."""

import asyncio
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from prompt_toolkit.application import Application
from prompt_toolkit.input.vt100 import Vt100Input

from wizolt.utils import terminal


class TerminalReplies:
    """Remove OSC 11/device replies before the key parser, preserving keys and pasted text."""

    COLOR = re.compile(r"\x1b\]11;rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}(?:\x07|\x1b\\)")
    ATTRIBUTES = re.compile(r"\x1b\[\?[0-9;]*c")
    PARTIAL_COLOR = re.compile(r"\x1b\]11;rgb:[0-9a-fA-F/]{0,32}\x1b?")
    PARTIAL_ATTRIBUTES = re.compile(r"\x1b\[[?>][0-9;]{0,64}")

    POSITIONS = ("\x1b[1;3R", "\x1b[1;4R")
    SECONDARY = re.compile(r"\x1b\[>([0-9]+);[0-9;]+c")

    def __init__(
        self,
        forward: Callable[[str], None],
        in_paste: Callable[[], bool],
        received: Callable[[tuple[int, int, int]], None],
        write: Callable[[str], None],
        tmux_mode: Callable[[bool], None] | None,
    ):
        self.forward = forward
        self.in_paste = in_paste
        self.received = received
        self.pending = ""
        self.write = write
        self.tmux_mode = tmux_mode
        self.awaiting = "identity" if tmux_mode else ""

    def feed(self, data: str) -> None:
        if not self.pending and "\x1b" not in data:
            self.forward(data)
            return
        index = 0
        while index < len(data):
            if self.in_paste():
                # A reply can follow the closing paste delimiter in the same read.
                end = data.find("~", index)
                stop = len(data) if end < 0 else end + 1
                self.forward(data[index:stop])
                index = stop
                continue
            self.pending += data[index]
            index += 1
            text = self.pending
            if self.COLOR.fullmatch(text):
                self.pending = ""
                color = terminal.parse_background(text.encode())
                assert color is not None
                self.received(color)
            elif match := self.SECONDARY.fullmatch(text):
                self.pending = ""
                if match[1] == "84" and self.tmux_mode is not None:  # tmux's secondary device type ('T').
                    if self.awaiting == "fence":
                        self.awaiting = "position"
                    elif self.awaiting == "identity":
                        # Only tmux receives the mode probe. With alternate-screen on,
                        # leaving it restores column 3; with it off, column 4 remains.
                        # Fence its CPR behind a DA2 reply so earlier renderer CPRs
                        # keep their owner. Restore the real cursor before ending sync.
                        self.awaiting = "fence"
                        self.write("\x1b[?2026h\x1b7\x1b[1;3H\x1b[>c\x1b[?1049h\x1b[1;4H\x1b[?1049l\x1b[6n\x1b8\x1b[?2026l")
            elif self.awaiting == "position" and text in self.POSITIONS:
                self.pending = ""
                self.awaiting = ""
                assert self.tmux_mode is not None
                self.tmux_mode(text == "\x1b[1;3R")
                self.tmux_mode = None
            elif self.ATTRIBUTES.fullmatch(text):
                self.pending = ""
            elif (
                any(prefix.startswith(text) for prefix in ("\x1b]11;rgb:", "\x1b[?", "\x1b[>"))
                or (self.awaiting == "position" and any(reply.startswith(text) for reply in self.POSITIONS))
                or self.PARTIAL_COLOR.fullmatch(text)
                or self.PARTIAL_ATTRIBUTES.fullmatch(text)
            ):
                continue
            else:
                self.flush()

    def flush(self) -> None:
        # An idle gap inside a recognizable terminal reply is not an Escape key. Keep its
        # bounded partial sequence for the next input chunk; lone Esc and ambiguous prefixes
        # still follow prompt-toolkit's normal timeout so keyboard chords remain responsive.
        if (
            (self.pending.startswith("\x1b]11;") and ("\x1b]11;rgb:".startswith(self.pending) or self.PARTIAL_COLOR.fullmatch(self.pending)))
            or self.PARTIAL_ATTRIBUTES.fullmatch(self.pending)
            or (self.awaiting == "position" and self.pending.startswith("\x1b[1") and any(reply.startswith(self.pending) for reply in self.POSITIONS))
        ):
            return
        text, self.pending = self.pending, ""
        if text:
            self.forward(text)


@contextmanager
def terminal_queries(
    app: Application,
    background: Callable[[tuple[int, int, int]], None] | None,
    tmux_mode: Callable[[bool], None] | None,
) -> Iterator[None]:
    """Ask on the application's own input stream, without delaying its first frame."""
    input_stream = app.input
    if not isinstance(input_stream, Vt100Input):
        yield
        return
    if not input_stream.stdin.isatty() or app.output.get_size().columns < 4:
        tmux_mode = None
    if background is None and tmux_mode is None:
        yield
        return

    def write(data: str) -> None:
        app.output.write_raw(data)
        app.output.flush()

    parser = input_stream.vt100_parser
    feed, flush = parser.feed, parser.flush
    replies = TerminalReplies(feed, lambda: parser._in_bracketed_paste, background or (lambda _: None), write, tmux_mode)

    def flush_input() -> None:
        replies.flush()
        flush()

    parser.feed, parser.flush = replies.feed, flush_input
    query = ("\x1b[>c" if tmux_mode else "") + ("\x1b]11;?\x1b\\\x1b[c" if background else "")
    handle = asyncio.get_running_loop().call_soon(write, query)
    try:
        yield
    finally:
        handle.cancel()
        parser.feed, parser.flush = feed, flush
