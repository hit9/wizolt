"""AGENTS.md instruction sources: loading, section parsing, menu rows, and `@agents.md:` resolution.

Two sources exist: the global `<data_dir>/AGENTS.md` and the project file (`AGENTS.md`, falling
back to `CLAUDE.md`, in the session cwd). Both are plain files the user may read and edit; this
module never writes them. A reference either names a whole file (`global`, `project`) or one
Markdown section by its visible heading path (`global/Contributing/PR body`), and expansion
attaches the original text -- never a paraphrase -- under one shared token cap.

Layering: the path helpers at module scope are imported by config and session; `AgentsFile`
owns one file's content (sections, lookups, its menu rows); `AgentsMentions` is the session
runtime that gathers sources, builds the menu, and expands references against the current
files.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wizolt.base import WizoltError
from wizolt.mentions import scan_mentions

if TYPE_CHECKING:
    from wizolt.session import Session

GLOBAL_FILENAME = "AGENTS.md"
GLOBAL_SCOPE = "global"
PROJECT_SCOPE = "project"
# Shared by the fixed context prefix and every reference expansion in one request (the design's
# "bound the fixed prefix" rule). Estimated at four characters per token, matching
# ContextManager.estimated_text_tokens.
TOKEN_CAP = 8_000
_CHARS_PER_TOKEN = 4
MAX_REFERENCES = 20


class AgentsReferenceError(WizoltError):
    """A `@agents.md:` reference that cannot be resolved against the current files."""


# -- Path helpers (imported by config.py and session/, so they stay module-scope).


def global_agents_md_path(data_dir: str) -> str:
    return os.path.join(os.path.abspath(os.path.expanduser(data_dir)), GLOBAL_FILENAME)


def display_path(path: str) -> str:
    """Abbreviate the home prefix to `~`, so the default data dir reads `~/.wizolt/AGENTS.md`
    while a custom data directory is shown by its actual path."""

    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def is_global_agents_md(path: str, data_dir: str) -> bool:
    """True only for the exact global file. Reading it needs no out-of-workspace prompt."""

    try:
        return os.path.realpath(path) == os.path.realpath(global_agents_md_path(data_dir))
    except OSError:
        return False


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


@dataclass(frozen=True)
class Section:
    """One ATX heading and its text, through the next heading of the same or higher level."""

    path: tuple[str, ...]  # visible heading titles from the file root
    text: str  # the heading line plus the following text

    @property
    def heading(self) -> str:
        return "/".join(self.path)


@dataclass(frozen=True)
class MenuRow:
    """One selectable row of the `@agents.md:` menu."""

    display: str
    meta: str
    insert: str  # the canonical reference text completion inserts


@dataclass(frozen=True)
class AgentsFile:
    """One instructions source: its scope, location, and verbatim content.

    Owns everything derivable from that content alone: Markdown sections, section lookup, and
    this file's rows of the completion menu. Knows nothing about the session or other files."""

    scope: str  # "global" or "project"
    path: str  # absolute filesystem path
    display: str  # what the menu and truncation markers show, e.g. "~/.wizolt/AGENTS.md"
    content: str

    _ATX = re.compile(r"^(#{1,6})(?:\s+(.*))?$")
    _FENCE = re.compile(r"^(```+|~~~+)\s*")
    EXCERPT_LIMIT = 80

    @property
    def label(self) -> str:
        return f"{self.scope} · {self.display}"

    @classmethod
    def load(cls, scope: str, path: str, display: str) -> AgentsFile | None:
        """Read one source; None when it is missing or unreadable."""

        content = _read(path)
        return None if content is None else cls(scope, path, display, content)

    @classmethod
    def global_file(cls, data_dir: str) -> AgentsFile | None:
        path = global_agents_md_path(data_dir)
        return cls.load(GLOBAL_SCOPE, path, display_path(path))

    @classmethod
    def project_file(cls, cwd: str, source_name: str) -> AgentsFile | None:
        """Read the project file by the name the session loaded it under (AGENTS.md or CLAUDE.md)."""

        if not source_name:
            return None
        path = os.path.abspath(os.path.join(cwd, source_name))
        return cls.load(PROJECT_SCOPE, path, "./" + source_name)

    # -- Sections (pure functions of the content).

    def sections(self) -> list[Section]:
        """Split the content into ATX-headed sections, ignoring headings inside fenced code.

        A section runs from its heading through the next heading of the same or higher level, so
        a parent section's text includes its subsections. Text before the first heading is not
        a section; it belongs to the whole-file reference."""

        # Stack of open sections, outermost first: (level, heading path, body lines, opening line
        # index). A nested heading extends the stack rather than replacing its parent, so the
        # parent keeps the text it collected before the subsection and gains the subsection's
        # rendered text back when it closes. Sections close innermost-first, so the opening
        # index restores document order (a parent before its own subsections).
        open_sections: list[tuple[int, tuple[str, ...], list[str], int]] = []
        sections: list[tuple[int, Section]] = []
        fence: tuple[str, int] | None = None

        def close_one() -> None:
            _level, path, body, index = open_sections.pop()
            rendered = Section(path, "\n".join(body).rstrip() + "\n")
            sections.append((index, rendered))
            if open_sections:
                open_sections[-1][2].append(rendered.text)

        for line_number, line in enumerate(self.content.splitlines()):
            if fence is not None:
                # A closing fence is the same character, at least as long as the opening, alone.
                stripped = line.strip()
                if stripped and set(stripped) == {fence[0]} and len(stripped) >= fence[1]:
                    fence = None
            elif (fence_match := self._FENCE.match(line)) is not None:
                fence = (fence_match.group(1)[0], len(fence_match.group(1)))
            elif (heading := self._ATX.match(line)) is not None:
                level = len(heading.group(1))
                while open_sections and open_sections[-1][0] >= level:
                    close_one()
                path = (*(entry[1][-1] for entry in open_sections), (heading.group(2) or "").strip())
                open_sections.append((level, path, [line], line_number))
                continue
            elif open_sections:
                pass
            else:
                continue
            if open_sections:
                open_sections[-1][2].append(line)
        while open_sections:
            close_one()
        return [section for _, section in sorted(sections, key=lambda item: item[0])]

    def section_text(self, heading: str) -> str | None:
        """The original text of one section, or None. Ambiguous duplicate heading paths raise."""

        matches = [section for section in self.sections() if section.heading == heading]
        if not matches:
            return None
        if len(matches) > 1:
            raise AgentsReferenceError(
                f'@agents.md:"{self.scope}/{heading}" is ambiguous: {len(matches)} sections share that heading path; rename them or cite a deeper heading'
            )
        return matches[0].text

    # -- Menu (this file's rows only; the runtime prepends the "All applicable" row).

    def menu_rows(self) -> list[MenuRow]:
        rows = [MenuRow(self.label, "whole file", self.reference())]
        for section in self.sections():
            rows.append(MenuRow(section.heading, self._row_meta(section), self.reference(section.heading)))
        return rows

    def _row_meta(self, section: Section) -> str:
        excerpt = " ".join(section.text.split())
        if len(excerpt) > self.EXCERPT_LIMIT:
            excerpt = excerpt[: self.EXCERPT_LIMIT - 1].rstrip() + "…"
        return f"{self.scope} · {excerpt}" if excerpt else self.scope

    def reference(self, heading: str = "") -> str:
        """The canonical `@agents.md:` form for this file or one of its sections."""

        payload = f"{self.scope}/{heading}" if heading else self.scope
        if any(ch in payload for ch in ' \t"'):
            payload = json.dumps(payload, ensure_ascii=False)
        return "@agents.md:" + payload


@dataclass(frozen=True)
class Reference:
    """One parsed `@agents.md:` mention; the empty payload is the valid "all applicable" form."""

    payload: str

    @classmethod
    def spans(cls, text: str) -> list[Reference]:
        return [cls(span.payload) for span in scan_mentions(text) if span.kind == "agents" and span.complete]

    @property
    def scope(self) -> str:
        return self.payload.partition("/")[0]

    @property
    def heading(self) -> str:
        _, slash, rest = self.payload.partition("/")
        return rest if slash else ""


class AgentsMentions:
    """Session runtime for `@agents.md:` references.

    The completion menu comes from the session-start snapshot (the fixed context prefix's copy,
    so both stay fixed together), while expansion resolves against the files as they are on
    disk at send time. Raises AgentsReferenceError for unknown or ambiguous references so the
    sender can surface the error instead of silently dropping the citation."""

    MAX_MENU_ROWS = 50

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- Sources.

    def _project_source_name(self) -> str:
        info = self.session.system_info
        return info.agents_md_source if info is not None else ""

    def current_sources(self) -> tuple[AgentsFile, ...]:
        """The sources as they are on disk now, in prefix order (global, then project)."""

        files: list[AgentsFile] = []
        if (global_file := AgentsFile.global_file(self.session.config.data_dir)) is not None:
            files.append(global_file)
        if (project := AgentsFile.project_file(self.session.cwd, self._project_source_name())) is not None:
            files.append(project)
        return tuple(files)

    def cached_sources(self) -> tuple[AgentsFile, ...]:
        """The session-start snapshot of both sources, for the fixed prefix and the menu."""

        info = self.session.system_info
        if info is None:
            return ()
        files: list[AgentsFile] = []
        if info.agents_md_global:
            files.append(
                AgentsFile(
                    GLOBAL_SCOPE,
                    global_agents_md_path(self.session.config.data_dir),
                    info.agents_md_global_display or GLOBAL_FILENAME,
                    info.agents_md_global,
                )
            )
        if info.agents_md and info.agents_md_source:
            files.append(
                AgentsFile(
                    PROJECT_SCOPE,
                    os.path.abspath(os.path.join(self.session.cwd, info.agents_md_source)),
                    "./" + info.agents_md_source,
                    info.agents_md,
                )
            )
        return tuple(files)

    def _file_for(self, scope: str, files: tuple[AgentsFile, ...]) -> AgentsFile | None:
        return next((candidate for candidate in files if candidate.scope == scope), None)

    # -- Menu.

    def menu_rows(self) -> list[MenuRow]:
        rows = [MenuRow("All applicable", "every instructions file below", "@agents.md:")]
        for file in self.cached_sources():
            rows.extend(file.menu_rows())
        return rows[: self.MAX_MENU_ROWS]

    def matching_rows(self, query: str) -> list[MenuRow]:
        """Rows matching a query by source, heading path, and original text; `All applicable`
        always stays first."""

        rows = self.menu_rows()
        query = query.strip().strip('"').lower()
        if not query:
            return rows
        matched = [row for row in rows[1:] if query in row.display.lower() or query in row.meta.lower()]
        return [*rows[:1], *matched][: self.MAX_MENU_ROWS]

    # -- Expansion.

    def resolve_mentions(self, text: str) -> str:
        """Expand every `@agents.md:` reference in the text into one bounded block, or ""."""

        files = self.current_sources()
        references = Reference.spans(text)[:MAX_REFERENCES]
        if not references:
            return ""
        expanded: list[tuple[AgentsFile, str]] = []
        seen: set[str] = set()
        for reference in references:
            if reference.payload in seen:
                continue
            seen.add(reference.payload)
            expanded.extend(self._expand_one(reference, files))
        if not expanded:
            return ""
        return self._render(expanded)

    def validation_error(self, text: str) -> str | None:
        """The user-facing error for an unresolvable reference, or None when the text is fine."""

        try:
            self.resolve_mentions(text)
        except AgentsReferenceError as error:
            return str(error)
        return None

    def _expand_one(self, reference: Reference, files: tuple[AgentsFile, ...]) -> list[tuple[AgentsFile, str]]:
        if not reference.payload:
            return [(file, file.content) for file in files]
        file = self._file_for(reference.scope, files)
        if file is None:
            where = ", ".join(candidate.scope for candidate in files) or "none is loaded"
            raise AgentsReferenceError(f'unknown @agents.md reference "{reference.payload}" (available sources: {where})')
        if not reference.heading:
            return [(file, file.content)]
        text = file.section_text(reference.heading)
        if text is None:
            raise AgentsReferenceError(f'unknown @agents.md reference "{reference.payload}": no section "{reference.heading}" in {file.label}')
        return [(file, text)]

    def _render(self, expanded: list[tuple[AgentsFile, str]]) -> str:
        """Join the expanded texts under one shared cap; clipped references say so with a path."""

        budget = TOKEN_CAP * _CHARS_PER_TOKEN
        clipped: list[str] = []
        parts: list[str] = []
        for file, text in expanded:
            if not text:
                continue
            if len(text) > budget:
                text = self._clip(text, budget, file.label)
                clipped.append(file.path)
            budget = max(0, budget - len(text))
            parts.extend((f"[{file.label}]", text.rstrip(), ""))
        header = [
            "--- AGENTS.MD REFERENCES ---",
            "The user cited these instructions for this request; the original text follows.",
        ]
        if clipped:
            header.append("Clipped to fit the shared reference budget; read the full file: " + ", ".join(clipped))
        return "\n".join([*header, "", *parts]).rstrip()

    @staticmethod
    def _clip(text: str, budget_chars: int, label: str) -> str:
        if len(text) <= budget_chars:
            return text
        marker = f"... ({label} clipped to fit the shared reference budget; read the full file for the rest) ..."
        head_limit = max(1, (budget_chars - len(marker) - 2) * 2 // 5)
        head = text[:head_limit].rstrip()
        tail = text[len(text) - max(1, budget_chars - len(head) - len(marker) - 2) :].lstrip()
        return "\n".join(part for part in (head, marker, tail) if part)
