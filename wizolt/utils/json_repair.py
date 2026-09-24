"""Repair near-JSON model output into the object it plainly meant to be.

Ported in miniature from json_repair (https://github.com/mangiucugna/json_repair, MIT license):
the same recursive-descent shape -- read what is there, close whatever a truncation left open,
tolerate what models mistype -- without the parts a compaction summary never contains (JSON
schemas, streaming, file input) and without the heuristics that go past reading (merging repeated
objects, closing a string around an unescaped inner quote). The one caller is the compaction
summary parser, on the path where json.loads has already failed; input worse than this reads
falls through to the compactor's retry and then to deterministic trimming.
"""

from __future__ import annotations

import re
from typing import Any

# Smart quotes pair with their closing mark; the ASCII quotes close with themselves.
_CLOSERS = {'"': '"', "'": "'", "“": "”", "„": "”"}
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_LITERALS = {"true": True, "false": False, "null": None, "none": None}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


class _NotAnObject(Exception):
    """Raised when a `{` opens something that reads no key-value pair: prose's stray braces and
    code samples, which the reference reader returns as an array rather than an object."""


class _Reader:
    """One pass over the text, holding only a cursor: every method reads forward and never back."""

    # Nesting past this is not a compaction summary; the parse gives up rather than spending a
    # real stack on it. The reference reader refuses the same input outright.
    MAX_DEPTH = 64

    def __init__(self, text: str, index: int):
        self.text = text
        self.index = index
        self.depth = 0

    def char(self) -> str:
        return self.text[self.index] if self.index < len(self.text) else ""

    def skip(self) -> None:
        """Step over whitespace and #, //, and /* */ comments, the way models annotate drafts."""
        text = self.text
        while self.index < len(text):
            char = text[self.index]
            if char in " \t\r\n":
                self.index += 1
            elif char == "#" or char == "/" and text[self.index + 1 : self.index + 2] == "/":
                self.index = _line_end(text, self.index)
            elif char == "/" and text[self.index + 1 : self.index + 2] == "*":
                self.index = text.find("*/", self.index + 2)
                self.index = len(text) if self.index < 0 else self.index + 2
            else:
                return

    def quoted(self) -> str:
        """The string whose opening quote is under the cursor. A string the output was cut short
        in ends at the end of the text, the same leniency a truncation elsewhere gets."""
        text = self.text
        closer = _CLOSERS[self.char()]
        chars: list[str] = []
        index = self.index + 1
        while index < len(text):
            char = text[index]
            if char == "\\" and index + 1 < len(text):
                escaped = text[index + 1]
                if escaped == "u" and index + 6 <= len(text):
                    try:
                        chars.append(chr(int(text[index + 2 : index + 6], 16)))
                        index += 6
                        continue
                    except ValueError:
                        pass  # not four hex digits: a stray escape, kept as written below
                if escaped in _ESCAPES:
                    chars.append(_ESCAPES[escaped])
                else:
                    chars.append(text[index : index + 2])  # a stray escape: kept as written
                index += 2
                continue
            index += 1
            if char == closer:
                break
            chars.append(char)
        self.index = index
        return "".join(chars)

    def bare(self, stop: str) -> str:
        """A key or value that lost its quotes: read up to the next stop character and trim."""
        start = self.index
        text = self.text
        while self.index < len(text) and text[self.index] not in stop:
            self.index += 1
        return text[start : self.index].strip()

    def value(self, stop: str) -> Any:
        self.skip()
        char = self.char()
        if char in _CLOSERS:
            return self.quoted()
        if char == "{":
            if self.depth >= self.MAX_DEPTH:
                # Deeper than any summary: step past the brace and read nothing, so what follows
                # becomes this object's own next content instead of a brace glued to a key.
                self.index += 1
                return ""
            self.depth += 1
            value = self.object()
            self.depth -= 1
            return value
        if char == "[":
            if self.depth >= self.MAX_DEPTH:
                self.index += 1
                return ""
            self.depth += 1
            value = self.array()
            self.depth -= 1
            return value
        run = self.bare(stop)
        if _NUMBER.fullmatch(run):
            return float(run) if any(mark in run for mark in ".eE") else int(run)
        if run.lower() in _LITERALS:
            return _LITERALS[run.lower()]
        return run

    def object(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        content = False  # anything read inside the braces that was not a key-value pair
        self.index += 1  # the opening brace
        while True:
            self.skip()
            char = self.char()
            if char == "}" or char == "":
                # A closed object, or one the output was cut short in: the pairs read so far stand.
                # Braces that read no pair at all were not an object -- unless they never got
                # anything else either: a summary truncated to its braces is an empty object.
                self.index += char == "}"
                if not result and content:
                    raise _NotAnObject
                return result
            if char == ",":
                self.index += 1
                continue
            key = self.quoted() if char in _CLOSERS else self.bare(":,}]")
            self.skip()
            if self.char() == ":":
                self.index += 1
                self.skip()
                if self.char() in (",", "}", ""):
                    result[key] = ""  # a pair cut off between its colon and its value
                else:
                    result[key] = self.value(",}]")
            elif not key and self.index < len(self.text):
                self.index += 1  # a stray character: step over it rather than loop on it
            elif key:
                content = True  # a key that never met its colon
            self.skip()
            char = self.char()
            if char == ",":
                self.index += 1
            elif char == "}" or char == "":
                self.index += char == "}"
                if not result and content:
                    raise _NotAnObject
                return result

    def array(self) -> list[Any]:
        result: list[Any] = []
        self.index += 1  # the opening bracket
        while True:
            self.skip()
            char = self.char()
            if char == "]" or char == "":
                self.index += char == "]"
                return result
            if char == ",":
                self.index += 1
                continue
            result.append(self.value(",]"))
            self.skip()
            char = self.char()
            if char == ",":
                self.index += 1
            elif char == "]" or char == "":
                self.index += char == "]"
                return result


def _line_end(text: str, index: int) -> int:
    end = len(text)
    for mark in ("\n", "\r"):
        found = text.find(mark, index)
        if found >= 0:
            end = min(end, found)
    return end


def repair_json_object(text: str) -> dict[str, Any] | None:
    """The first object the text contains, or None when there is none to recover.

    Reading starts at each `{` in turn, so prose around the object is skipped; a start that reads
    no object at all (prose's stray braces, code samples) gives way to the next `{`. An object
    cut down to nothing but its braces is still an object: the compactor plainly answered, just
    before the output ran out, and an empty summary is the compaction echo check's to catch.

    Each `{` costs one pass bounded by the whole text, so a malformed text of many braces could
    in principle go quadratic; the attempt cap keeps that linear on input size."""
    attempts = 0
    for start, char in enumerate(text):
        if char != "{":
            continue
        attempts += 1
        if attempts > 256:
            return None
        try:
            return _Reader(text, start).object()
        except _NotAnObject:
            continue
        except RecursionError:  # pathological nesting: not a summary, let the next `{` try
            continue
    return None
