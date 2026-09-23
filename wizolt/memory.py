"""Global user memory: MEMORY.md parsing, the session-fixed catalog, and @mem: expansion.

The memory file lives at <data_dir>/MEMORY.md and is shared across projects. Each entry starts
with a `## <id> <title>` heading and runs to the next one; the id is an internal, stable label
(eight lowercase hex digits), the title is what people search and reference, so it stays unique.
Nothing here writes the file: entries are added and edited through the Edit tool under the user's
explicit ask, exactly like any other file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wizolt.base import MAX_MEMORY_FILE_BYTES, MEMORY_MD_FILENAME
from wizolt.mentions import scan_mentions

if TYPE_CHECKING:
    from wizolt.session import Session

# One entry heading: eight lowercase-hex id chars, whitespace, then a non-empty title. Anything
# else under `##` is not an entry -- the parser never guesses where one begins.
ENTRY_HEADING = re.compile(r"^## ([0-9a-f]{8})[ \t]+(.*?)[ \t]*$")

# The catalog rides every request's fixed prefix; bound it like the AGENTS.md budget and say when
# it gave entries up instead of silently dropping them.
MAX_CATALOG_CHARS = 4_000
MAX_REFERENCES = 50
MAX_MENTION_BLOCK_CHARS = 16_000
PREVIEW_CHARS = 40


@dataclass(frozen=True)
class MemoryEntry:
    """One parsed memory entry: stable id, unique-by-convention title, free-form body ("" allowed)."""

    id: str
    title: str
    body: str


def parse_memory(text: str) -> list[MemoryEntry]:
    """Split MEMORY.md into entries, keeping file order; lines before the first entry are ignored."""

    entries: list[MemoryEntry] = []
    current: MemoryEntry | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal current, body
        if current is not None:
            entries.append(MemoryEntry(current.id, current.title, "\n".join(body).rstrip()))
        current, body = None, []

    for line in text.splitlines():
        match = ENTRY_HEADING.match(line)
        if match is not None and match.group(2).strip():
            flush()
            current = MemoryEntry(match.group(1), match.group(2).strip(), "")
        elif current is not None:
            body.append(line)
    flush()
    return entries


def preview(body: str, limit: int = PREVIEW_CHARS) -> str:
    """One line of body for menus: the first non-blank line, clipped, with an ellipsis when cut."""

    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= limit else stripped[:limit].rstrip() + "…"
    return ""


class MemoryStore:
    """Read-side owner of the global memory file: catalog, mention expansion, menu rows."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._catalog: str | None = None

    def path(self) -> str:
        return self.session.data_path(MEMORY_MD_FILENAME)

    def read(self) -> str:
        try:
            with open(self.path(), "rb") as file:
                content = file.read(MAX_MEMORY_FILE_BYTES + 1)
            return content.decode("utf-8") if len(content) <= MAX_MEMORY_FILE_BYTES else ""
        except (OSError, UnicodeDecodeError):
            return ""

    def file_size(self) -> int:
        try:
            return os.path.getsize(self.path())
        except OSError:
            return 0

    def over_cap(self) -> bool:
        return self.file_size() > MAX_MEMORY_FILE_BYTES

    def entries(self) -> list[MemoryEntry]:
        """The entries as the file holds them right now -- send-time reads see current bodies."""

        return parse_memory(self.read())

    def menu_entries(self) -> tuple[tuple[str, str], ...]:
        """(title, full body) rows in file order, for the @mem: completion menu's keyword filter."""

        return tuple((entry.title, entry.body) for entry in self.entries())

    def catalog(self) -> str:
        """The session-fixed MEMORY catalog injected into the request prefix.

        Computed once per session from the file as it stood then, so the fixed prefix stays stable
        even if the file changes mid-session; later turns see changes through @mem: expansion and
        Read, which resolve at send time.
        """

        if self._catalog is None:
            self._catalog = self._build_catalog()
        return self._catalog

    def _build_catalog(self) -> str:
        lines = [
            "--- MEMORY ---",
            f"Path: {self.path()}",
            (
                "Long-term memory shared across all projects; entries may be stale and never override the "
                "current request or AGENTS.md instructions. When a request touches long-term preferences, "
                "standing conventions, or cross-project background, read the file with Read (it needs no "
                "confirmation). Edit MEMORY.md only when the user explicitly asks to remember, update, or "
                "forget something; read the file first so other entries are not overwritten, and give every "
                "new entry a unique title and a random eight-digit lowercase-hex id checked against the ids "
                "already in the file."
            ),
            "",
        ]
        if self.over_cap():
            lines.append(
                f"The file is {self.file_size()} bytes, over the {MAX_MEMORY_FILE_BYTES}-byte cap: no entries were loaded; tell the user to slim the file."
            )
            return "\n".join(lines)
        entries = self.entries()
        if not entries:
            lines.append("No saved entries yet.")
            return "\n".join(lines)
        lines.append("")
        rows = [f"- {entry.id} {entry.title}" for entry in entries]
        total = sum(len(row) + 1 for row in rows)
        if total > MAX_CATALOG_CHARS:
            kept: list[str] = []
            used = 0
            for row in rows:
                if used + len(row) + 1 > MAX_CATALOG_CHARS:
                    break
                kept.append(row)
                used += len(row) + 1
            omitted = len(rows) - len(kept)
            note = f"... (catalog truncated: {omitted} of {len(rows)} entries omitted; /memory lists the full file)"
            while kept and used + len(note) + 1 > MAX_CATALOG_CHARS:
                used -= len(kept.pop()) + 1
                omitted += 1
            rows = [*kept, note]
        lines.extend(rows)
        return "\n".join(lines)

    def resolve_mentions(self, text: str) -> str:
        """Expand complete @mem: mentions into one bounded block with current bodies inlined.

        Titles resolve exactly and only: one match inlines the body, several matches with one title
        are reported as ambiguous instead of guessed, and an unknown title is named as such. The
        block is attached to the request that carried the mention and never re-resolved later.
        """

        spans = [span for span in scan_mentions(text) if span.kind == "mem" and span.complete and span.payload]
        if not spans:
            return ""
        if self.over_cap():
            return f"--- MEMORY MENTIONS ---\nThe memory file exceeds its {MAX_MEMORY_FILE_BYTES}-byte cap; tell the user to slim it before using @mem:."
        if not os.path.exists(self.path()):
            return "--- MEMORY MENTIONS ---\nNo memory file exists; tell the user this @mem: reference cannot be resolved."
        by_title: dict[str, list[MemoryEntry]] = {}
        for entry in self.entries():
            by_title.setdefault(entry.title, []).append(entry)
        sections: list[str] = []
        notes: list[str] = []
        seen: set[str] = set()
        omitted = 0
        for span in spans:
            title = span.payload
            if title in seen:
                continue
            seen.add(title)
            matches = by_title.get(title, [])
            if not matches:
                notes.append(f"* {title}: no memory entry with this title; suggest /memory so the user can pick one")
                continue
            if len(matches) > 1:
                notes.append(f"* {title}: {len(matches)} entries share this title; ask the user which one, do not guess")
                continue
            if len(sections) >= MAX_REFERENCES:
                omitted += 1
                continue
            entry = matches[0]
            sections.append(f"## {entry.title}" + (f"\n{entry.body}" if entry.body else ""))
        if omitted:
            notes.append(f"* {omitted} additional memory mention(s) omitted at the {MAX_REFERENCES}-reference cap")
        if not sections and not notes:
            return ""
        header = [
            "--- MEMORY MENTIONS ---",
            "The user referenced these memories by title; bodies are inlined as of this request and are not re-resolved for later turns.",
            "",
        ]
        block = "\n".join([*header, *sections, *notes]).strip()
        if len(block) > MAX_MENTION_BLOCK_CHARS:
            block = block[:MAX_MENTION_BLOCK_CHARS].rstrip() + "\n... (memory mentions truncated to fit the request)"
        return block
