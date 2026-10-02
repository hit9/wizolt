"""Net diffs: what a turn or a session changed, reconstructed from recorded edits.

A pure algorithm over `TurnDiff` records and the files on disk -- unified-diff generation,
forward/reverse hunk application, legacy reconstruction, and rename detection. It reads `cwd` as
an argument and nothing else from the session, which is why it lives beside `Session` rather than
on it: the session owns the records, this owns what they mean.

`Session.latest_round_diff_sections` and `Session.session_diff_sections` are the two entry points
the UI uses; everything else here is how they are computed.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from wizolt.base import split_lines

if TYPE_CHECKING:
    from wizolt.session import TurnDiff


def net_diff_for_path(status: str, path: str, before: str, after: str) -> tuple[str, str, str] | None:
    if before == after:
        return None
    text = "".join(difflib.unified_diff(split_lines(before), split_lines(after), fromfile="/dev/null" if not before else path, tofile=path))
    return (status, path, text) if text else None


@dataclass
class PathEdits:
    """One agent's receipts for one path; external changes break snapshot continuity.

    A net diff needs an unbroken chain of this agent's states. Gaps are not its edits: retain
    individual receipts rather than attributing a peer's changes or guessing how to undo them.
    """

    snapshots: tuple[str, str] | None = None
    discontinuous: bool = False
    recorded: list[str] = field(default_factory=list)
    legacy: list[str] = field(default_factory=list)
    trailing: list[str] = field(default_factory=list)

    def add(self, diff: TurnDiff) -> None:
        self.recorded.append(diff.diff)
        if not (diff.before or diff.after):
            self.legacy.append(diff.diff)
            self.trailing.append(diff.diff)
            return
        before = diff.before
        if self.snapshots is not None:
            previous = self.snapshots[1]
            expected = _forward_apply(previous, self.trailing) if self.trailing else previous
            if diff.before != expected:
                self.discontinuous = True
            before = self.snapshots[0]
        elif self.trailing:
            # Recover a snapshot-less prefix before clearing it; later middle edits must never
            # be reverse-applied to this first baseline. Failed recovery retains all receipts.
            original = _reverse_apply(before, self.trailing)
            if original is None:
                self.discontinuous = True
            else:
                before = original
        self.snapshots = (before, diff.after)
        self.trailing.clear()

    def chunk(self, path: str, status: str, cwd: str) -> str:
        if self.discontinuous:
            return self.receipts()
        if self.snapshots is not None:
            before, after = self.snapshots
            if self.trailing:
                # Today's shared file is not our final state. Derive it from our own receipts.
                final = _forward_apply(after, self.trailing)
                if final is None:
                    return self.receipts()
                after = final
            section = net_diff_for_path(status, path, before, after)
            return section[2] if section else ""
        # No snapshots: reverse application changes only our recorded hunks. If context is
        # ambiguous or was changed externally, retain the individual receipts.
        reconstructed = _reconstruct_legacy_diff(cwd, path, self.legacy, status) if cwd else None
        return reconstructed if reconstructed is not None else self.receipts()

    def receipts(self) -> str:
        return "\n".join(chunk.rstrip("\n") for chunk in self.recorded)


def net_diff_sections(diffs: list[TurnDiff], status: str, *, cwd: str = "") -> list[tuple[str, str, str]]:
    histories: dict[str, PathEdits] = {}
    for diff in diffs:
        histories.setdefault(diff.path, PathEdits()).add(diff)

    # Bash can move a file between Edit calls. Only continuous snapshot histories can identify
    # a unique move; a shared-file gap must never be interpreted as our rename.
    while True:
        states = {path: history.snapshots for path, history in histories.items() if history.snapshots is not None and not history.discontinuous}
        legacy = {path: history.legacy for path, history in histories.items() if history.legacy}
        move = _find_unambiguous_move(states, legacy)
        if move is None:
            break
        source, target = move
        histories[target].snapshots = (states[source][0], states[target][1])
        del histories[source]

    sections = []
    for path, history in histories.items():
        if chunk := history.chunk(path, status, cwd):
            sections.append((status, path, chunk.rstrip("\n") + "\n"))
    return sections


def _current_content(cwd: str, path: str) -> str | None:
    if not cwd:
        return None
    abspath = path if os.path.isabs(path) else os.path.join(cwd, path)
    try:
        with open(abspath, encoding="utf-8") as file:
            return file.read()
    except (OSError, UnicodeDecodeError):
        return None


_HUNK_RE: re.Pattern[str] = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def _reverse_apply(current: str, chunks: list[str]) -> str | None:
    """Walk `current` back to the state before the given per-Edit hunks by reverse-applying them
    in reverse chronological order. Each hunk's after-text must occur uniquely in the buffer; if
    not (external mutation, ambiguous context, or hunks that don't belong to this buffer's
    history), return None so the caller can fall back."""
    hunk_pairs: list[tuple[str, str]] = []
    for chunk in chunks:
        pairs = _split_hunks(chunk)
        if pairs is None:
            return None
        hunk_pairs.extend(pairs)
    for after_text, before_text in reversed(hunk_pairs):
        if not after_text or not before_text:
            return None
        if current.count(after_text) != 1:
            return None
        current = current.replace(after_text, before_text, 1)
    return current


def _forward_apply(current: str, chunks: list[str]) -> str | None:
    """Apply the given per-Edit hunks forward to `current` in chronological order, deriving the
    content they produce. Each hunk's before-text must occur uniquely in the buffer; if not
    (external mutation or ambiguous context), return None so the caller can fall back. The mirror
    of `_reverse_apply`: used to recover a file's final content from its last snapshot when the
    file is no longer on disk."""
    hunk_pairs: list[tuple[str, str]] = []
    for chunk in chunks:
        pairs = _split_hunks(chunk)
        if pairs is None:
            return None
        hunk_pairs.extend(pairs)
    for after_text, before_text in hunk_pairs:
        if not after_text or not before_text:
            return None
        if current.count(before_text) != 1:
            return None
        current = current.replace(before_text, after_text, 1)
    return current


def _reconstruct_legacy_diff(cwd: str, path: str, chunks: list[str], status: str) -> str | None:
    final = _current_content(cwd, path)
    if final is None:
        return None
    original = _reverse_apply(final, chunks)
    if original is None:
        return None
    section = net_diff_for_path(status, path, original, final)
    return section[2] if section else ""


def _split_hunks(chunk: str) -> list[tuple[str, str]] | None:
    pairs: list[tuple[str, str]] = []
    before_lines: list[str] | None = None
    after_lines: list[str] | None = None
    for line in chunk.splitlines():
        if before_lines is None and line.startswith(("--- ", "+++ ")):
            # The file headers precede the first hunk. Inside one, a deleted line whose content
            # starts with "-- " (SQL and Lua comments) renders as "--- ..." and is content, not a
            # header: dropping it here silently ate that line out of the reconstructed diff.
            continue
        if _HUNK_RE.match(line):
            if before_lines is not None and after_lines is not None:
                pairs.append(("\n".join(after_lines), "\n".join(before_lines)))
            before_lines, after_lines = [], []
            continue
        if before_lines is None or after_lines is None:
            return None
        if line.startswith("+"):
            after_lines.append(line[1:])
        elif line.startswith("-"):
            before_lines.append(line[1:])
        elif line.startswith(" "):
            before_lines.append(line[1:])
            after_lines.append(line[1:])
        elif line == "\\ No newline at end of file":
            continue
        else:
            return None
    if before_lines is not None and after_lines is not None:
        pairs.append(("\n".join(after_lines), "\n".join(before_lines)))
    return pairs


def _find_unambiguous_move(states: dict[str, tuple[str, str]], legacy: dict[str, list[str]]) -> tuple[str, str] | None:
    sources_by_after: dict[str, list[str]] = {}
    targets_by_before: dict[str, list[str]] = {}
    for path, (before, after) in states.items():
        if path in legacy:
            continue
        if after:
            sources_by_after.setdefault(after, []).append(path)
        if before:
            targets_by_before.setdefault(before, []).append(path)
    for content, sources in sources_by_after.items():
        targets = targets_by_before.get(content, [])
        if len(sources) == 1 and len(targets) == 1 and sources[0] != targets[0]:
            return sources[0], targets[0]
    return None
