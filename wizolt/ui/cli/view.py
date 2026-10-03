"""TUI fragment and style supply for the interactive shell.

The view-facing half of the interactive loop: dividers, follow-up markers, model stream
previews, the input hint, and the prompt-toolkit style map. Reads session and presentation
state directly, without a command-loop or agent handle.
"""

from __future__ import annotations

import heapq
import re
import shutil
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, ClassVar

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from wizolt.agentsmd import MenuRow
from wizolt.base import LogBlock, LogEdge, Text, TurnBox
from wizolt.config import PROVIDER_API_CHOICES
from wizolt.mentions import MentionSpan, active_mention, encode_file_mention, mention_spellings
from wizolt.providers.compat import bundled_policy
from wizolt.session import QueuedInput, Session
from wizolt.ui.bars import DIVIDER_PRESETS, PRESETS, STATUS_PRESETS
from wizolt.ui.cli.appearance import KINDS
from wizolt.ui.cli.commands import NEEDS_ARGUMENT, SET_KEYS, SET_VALUES
from wizolt.ui.cli.hints import Context as HintContext
from wizolt.ui.cli.hints import HintPicker
from wizolt.ui.cli.registry import CommandCatalog
from wizolt.ui.cli.runtime import RESUME_STATUS_LABEL, STARTING_STATUS_LABEL
from wizolt.ui.render import ActivityPulse, InputStyle, LiveSpark, Theme, UiPrinter
from wizolt.ui.tui import InputMode

if TYPE_CHECKING:
    from wizolt.ui.cli.presentation import Presentation


class CommandCompleter(Completer):
    """Prompt-toolkit completer for slash commands, their arguments, and @/$ mentions."""

    # The kinds offered on a bare "@", each with its one-line meta (SPEC 4.2).
    KINDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("file:", "files in this repo"),
        ("mcp:", "MCP servers and tools"),
        ("skill:", "installed skills"),
        ("agents.md:", "AGENTS.md instructions and sections"),
    )
    MAX_ROWS = 50  # SPEC R4: cap the menu.

    def __init__(
        self,
        providers: Callable[[], tuple[str, ...]] = tuple,
        models: Callable[[], tuple[str, ...]] = tuple,
        # The active model's own scale, so completion offers exactly what `/reason` accepts.
        reasoning_choices: Callable[[], tuple[str, ...]] = lambda: ("off", *bundled_policy().effort_order),
        # Workers can target another provider/model, so offer the catalog-wide vocabulary here.
        mcp_servers: Callable[[], tuple[str, ...]] = tuple,
        mcp_connected_servers: Callable[[], tuple[str, ...]] = tuple,
        mcp_tools: Callable[[str], tuple[str, ...]] = lambda _server: (),
        skills: Callable[[], tuple[str, ...]] = tuple,
        # "project" or "user" for a skill name, shown beside it so two sources never look alike.
        skill_source: Callable[[str], str] = lambda _name: "",
        # (name, argument hint) of each skill `/name` can start.
        skill_commands: Callable[[], tuple[tuple[str, str], ...]] = tuple,
        files: Callable[[], tuple[tuple[str, str], ...]] = tuple,
        file_matches: Callable[[str], tuple[str, ...]] | None = None,
        agents_rows: Callable[[], list[MenuRow]] = list,
        commands: CommandCatalog | None = None,
    ):
        self.providers = providers
        self.models = models
        self.reasoning_choices = reasoning_choices
        self.mcp_servers = mcp_servers
        self.mcp_connected_servers = mcp_connected_servers
        self.mcp_tools = mcp_tools
        self.skills = skills
        self.skill_source = skill_source
        self.skill_commands = skill_commands
        # (lowercase, original) workspace-relative paths from the session's cached path list.
        self.files = files
        self.file_matches = file_matches
        self.agents_rows = agents_rows
        self.commands = commands or CommandCatalog()

    def get_completions(self, document, complete_event):
        del complete_event
        text = document.text_before_cursor
        if text.startswith("/set "):
            tail = text[len("/set ") :]
            if " " not in tail:
                yield from self.matches(SET_KEYS, tail, more=SET_KEYS)  # every key takes a value
                return
            key, _, value = tail.partition(" ")
            yield from self.matches(SET_VALUES.get(key, ()), value)
            return
        if text.startswith("/theme "):
            tail = text[len("/theme ") :]
            if " " not in tail:
                yield from self.matches((*Theme.choices(), *KINDS[1:6]), tail, more=KINDS[1:6])
                return
            kind, _, value = tail.partition(" ")
            if kind in ("statusbar", "divider"):
                if value.startswith("theme "):
                    yield from self.matches(("inherit", *Theme.choices()), value[len("theme ") :])
                    return
                if " " not in value:
                    presets = STATUS_PRESETS if kind == "statusbar" else DIVIDER_PRESETS
                    yield from self.matches((*presets, "theme"), value, more=("theme",))
                    return
            values = {
                "diff": Theme.diff_styles(),
                "statusbar": tuple(STATUS_PRESETS),
                "divider": tuple(DIVIDER_PRESETS),
                "sweep": tuple(PRESETS["sweep"]),
                "input": tuple(InputStyle.PRESETS),
            }
            if kind in values:
                yield from self.matches(values[kind], value)
            return
        for command, values in (
            ("/model ", self.models),
            ("/provider ", self.providers),
            ("/reason ", self.reasoning_choices),
            ("/effort ", self.reasoning_choices),
            ("/api ", lambda: PROVIDER_API_CHOICES),
            ("/strict ", lambda: ("on", "off")),
            ("/compact ", lambda: ("log",)),
        ):
            if text.startswith(command):
                yield from self.matches(values(), text[len(command) :])
                return
        if text.startswith("/mcp "):
            tail = text[len("/mcp ") :]
            if " " not in tail:
                yield from self.matches(("connect", "disconnect", "tools"), tail, more=("connect", "disconnect"))
                return
            sub, _, value = tail.partition(" ")
            if sub == "connect":
                completed, _, prefix = value.rpartition(" ")
                selected = set(completed.split())
                yield from self.matches((name for name in self.mcp_servers() if name not in selected), prefix)
                return
            if sub == "disconnect":
                yield from self.matches(self.mcp_servers(), value)
                return
            if sub == "tools":
                yield from self.matches(self.mcp_connected_servers(), value)
                return

        for command, choices in (("/catalog ", ("status", "sync")), ("/skills ", ("list", "reload", "trust", "untrust"))):
            if text.startswith(command) and " " not in (tail := text[len(command) :]):
                yield from self.matches(choices, tail)
                return

        span = active_mention(text)
        if span is not None:
            completions = list(self._mention_completions(span, span.start - len(text)))
            # A fully typed mention with nothing longer beside it needs no menu, as a fully typed
            # command gets none: Enter sends it as typed.
            if not (len(completions) == 1 and completions[0].text in mention_spellings(text[span.start :])):
                yield from completions
            return

        if text.startswith("/") and " " not in text:
            names = self.commands.names()
            entries = self.commands.entries()
            for completion in self.matches(names, text, more=NEEDS_ARGUMENT):
                entry = entries.get(str(completion.display_text))
                if entry and entry.description:
                    completion = Completion(
                        completion.text,
                        start_position=completion.start_position,
                        display=completion.display,
                        display_meta=entry.description,
                    )
                yield completion
            for name, hint in self.skill_commands():
                command = "/" + name
                if command.startswith(text) and command not in names:
                    # A skill that takes arguments opens them on Enter, like `/set`.
                    yield Completion(command + " " if hint else command, start_position=-len(text), display=command, display_meta=hint or "skill")

    def command_state(self, word: str) -> str:
        """How a `/` draft's first word should be coloured while it is typed: "known" when a
        command or skill would run it, "partial" while it is still a prefix of one, "" once no
        spelling would accept it."""
        spellings = self.commands.names() + tuple("/" + name for name, _ in self.skill_commands())
        if word in spellings:
            return "known"
        return "partial" if any(spelling.startswith(word) for spelling in spellings) else ""

    def leads_on(self, before: str, completion: Completion) -> bool:
        """Whether taking `completion`, offered for the input `before`, is a step toward a longer
        choice rather than a choice: Enter then fills it in and opens what comes next instead of
        finishing. The completer produced the row, so it is the one place that knows.

        A command or argument that needs more after it carries a trailing space (see `matches`).
        A mention kind is a step from the bare `@` menu -- but `@agents.md:` in its own menu is
        the "All applicable" row, a choice. An MCP server is a step toward its tools, though it is
        a whole mention too, which is why its tools open with none highlighted."""
        text = completion.text
        if text.endswith(" "):
            return True
        span = active_mention(before)
        if span is None:
            return False
        if span.kind == "bare" and text in {"@" + kind for kind, _ in self.KINDS}:
            return True
        return text.startswith("@mcp:") and "." not in text and self._known_server(text[len("@mcp:") :]) is not None

    @staticmethod
    def matches(values, prefix: str, more=()):
        """Prefix matches. A value in `more` needs something after it (`/set`, a `/set` key, `/mcp
        connect`): it completes with a trailing space, which is how the input knows to open the next
        level on Enter instead of running the command half-typed."""
        return (Completion(value + " " if value in more else value, start_position=-len(prefix), display=value) for value in values if value.startswith(prefix))

    def _mention_completions(self, span: MentionSpan, start: int) -> Iterator[Completion]:
        """Complete one scanner-owned span and always insert canonical namespace forms."""
        if span.kind == "file":
            yield from self._file_completions(span.payload, start)
        elif span.kind == "mcp":
            yield from self._mcp_completions(span.payload, start)
        elif span.kind == "skill":
            yield from self._skill_completions(span.payload, start)
        elif span.kind == "agents":
            yield from self._agents_completions(span.payload, start)
        else:
            yield from self._merged_completions(span.payload, start)

    def _merged_completions(self, raw: str, start: int) -> Iterator[Completion]:
        """The bare "@" menu: kinds while they prefix-match, then all three sources (SPEC 4.2).

        Repository files deliberately stay out of this menu: selecting @file: opens the dedicated
        picker without scanning the repository merely because the user typed "@".
        """
        if not raw:
            for kind, meta in self.KINDS:
                yield Completion("@" + kind, start_position=start, display_meta=meta)
            return
        lower = raw.lower()
        kind_items = [Completion("@" + kind, start_position=start, display_meta=meta) for kind, meta in self.KINDS if kind.startswith(lower)]

        server_part, dot, tool_part = raw.partition(".")
        if dot:
            server = self._known_server(server_part)
            mcp_items = (
                [
                    Completion(f"@mcp:{server}.{name}", start_position=start, display_meta="mcp")
                    for name in self._matching_names(self.mcp_tools(server), tool_part)
                ]
                if server is not None
                else []
            )
        else:
            mcp_items = [Completion(f"@mcp:{name}", start_position=start, display_meta="mcp") for name in self._matching_names(self.mcp_servers(), raw)]
        skill_items = [
            Completion(f"@skill:{name}", start_position=start, display_meta=" · ".join(filter(None, ("skill", self.skill_source(name)))))
            for name in self._matching_names(self.skills(), raw)
        ]
        yield from [*kind_items, *mcp_items, *skill_items][: self.MAX_ROWS]

    def _file_completions(self, query: str, start: int) -> Iterator[Completion]:
        for path in self._matching_files(query):
            yield Completion(encode_file_mention(path), start_position=start)

    def _matching_files(self, query: str) -> list[str]:
        """Case-insensitive substring over the whole workspace-relative path (SPEC M3), ranked
        by prefix of basename, then substring of basename, then substring of path (R1); ties by
        shorter path, then alphabetical (R3). Deterministic - no recency, no MRU."""
        if self.file_matches is not None:
            return list(self.file_matches(query))
        q = query.lower()

        def ranked():
            for lower, path in self.files():
                if q not in lower:
                    continue
                base = lower.rsplit("/", 1)[-1]
                if base.startswith(q):
                    score = 0
                elif q in base:
                    score = 1
                else:
                    score = 2
                yield score, len(lower), lower, path

        return [path for _, _, _, path in heapq.nsmallest(self.MAX_ROWS, ranked())]

    def _mcp_completions(self, query: str, start: int) -> Iterator[Completion]:
        """After "@mcp:": servers, then "server." expands to that server's tools. A payload that
        already names a server whole offers its tools first, so picking a server cascades into them,
        then any longer server names, then the server itself: last, so Down reaches a tool first,
        yet present, so the input reads as a complete mention and Enter keeps it rather than
        swapping in a longer server (`@mcp:git` beside `@mcp:github`)."""
        server_part, dot, tool_part = query.partition(".")
        if dot:
            server = self._known_server(server_part)
            if server is not None:
                for name in self._matching_names(self.mcp_tools(server), tool_part):
                    yield Completion(f"@mcp:{server}.{name}", start_position=start)
            return
        server = self._known_server(query) if query else None
        if server is not None:
            for name in self.mcp_tools(server):
                yield Completion(f"@mcp:{server}.{name}", start_position=start)
        for name in self._matching_names(self.mcp_servers(), query):
            if name != server:
                yield Completion(f"@mcp:{name}", start_position=start)
        if server is not None:
            yield Completion(f"@mcp:{server}", start_position=start)

    def _skill_completions(self, query: str, start: int) -> Iterator[Completion]:
        for name in self._matching_names(self.skills(), query):
            yield Completion(f"@skill:{name}", start_position=start, display_meta=self.skill_source(name) or None)

    def _agents_completions(self, query: str, start: int) -> Iterator[Completion]:
        """After "@agents.md:": the bounded menu of files and their sections, filtered by
        source, heading path, and original text (the `All applicable` row always stays first)."""

        needle = query.strip().strip('"').lower()
        rows = self.agents_rows()
        if needle:
            rows = [row for row in rows if any(needle in field.lower() for field in (row.display, row.meta, row.insert, row.search_text))]
        for row in rows[: self.MAX_ROWS]:
            display = (row.filtered_display or row.display) if needle else row.display
            yield Completion(row.insert, start_position=start, display=display, display_meta=row.meta)

    @staticmethod
    def _matching_names(values, query: str) -> list[str]:
        """Servers, skills, tools, kinds: prefix match, then substring (SPEC M2), alphabetical."""
        q = query.lower()
        prefix, substring = [], []
        for name in dict.fromkeys(values):
            low = name.lower()
            if low.startswith(q):
                prefix.append(name)
            elif q in low:
                substring.append(name)
        return [*sorted(prefix), *sorted(substring)][: CommandCompleter.MAX_ROWS]

    def _known_server(self, name: str) -> str | None:
        for candidate in self.mcp_servers():
            if candidate == name or candidate.lower() == name.lower():
                return candidate
        return None


class View:
    """Fragments and style derived from session and presentation state."""

    GLOW_STEPS: ClassVar[int] = 12

    QUEUE_EMPTY_HINT = "Enter follow-up · Tab next turn · Ctrl-C interrupts"
    QUEUE_PENDING_HINT = "↑ recalls queued · Tab next turn · Ctrl-C interrupts"

    # Line-level markdown tokens the live stream preview styles. Block constructs (headings,
    # lists, fenced code) are deliberately not parsed: the preview shows partial streaming text,
    # and only tokens that close on one line can render without flickering as the stream grows.
    # Each token must hold a non-blank character, so whitespace-only runs (`** **`, `* *`) stay
    # literal; the italic branch's `(?<!\*) ... (?!\*)` guards keep a lone star from borrowing
    # itself out of a `**` run.
    STREAM_INLINE_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(\*\*[^*\n]*[^*\s\n][^*\n]*\*\*|`[^`\n]*[^`\s\n][^`\n]*`|(?<!\*)\*[^*\n]*[^*\s\n][^*\n]*\*(?!\*))"
    )

    def __init__(self, session: Session, presentation: Presentation) -> None:
        self.session = session
        self.presentation = presentation
        self._hint_picker = HintPicker()  # idle-placeholder tips; see wizolt/ui/cli/hints.py
        # The app reads the style on every render through a DynamicStyle; it is rebuilt only when
        # the theme changes.
        self._style: tuple[tuple[tuple[str, int], str], Style] | None = None

    def waiting_pulse_fragments(self) -> StyleAndTextTuples:
        if self.session.state.current_model_call_started_at <= 0:
            return []
        return ActivityPulse.fragments(time.monotonic())

    def sweep_divider_fragments(
        self,
        label: str,
        prefix: StyleAndTextTuples | None = None,
        label_style: str = "class:divider.working",
        *,
        running: bool = True,
        fields: dict | None = None,
    ) -> StyleAndTextTuples:
        bar = self.presentation.status_bar
        values = bar.values()
        values.update(fields or {})
        values.update(running=running, label=label, spinner="".join(fragment[1] for fragment in (prefix or [])))
        if not bar.started_at:
            values["elapsed"] = time.monotonic() if running else 0.0

        def styles(specs: set[str], kind: str) -> dict[str, str]:
            resolved = Theme.bar_styles(specs, kind=kind)
            if "divider.label" not in Theme.bar_palette(kind).highlights:
                resolved["divider.label"] = label_style
            if prefix:
                resolved["spinner"] = prefix[0][0]
            return resolved

        ramp = ("class:queue.rule", *(f"class:divider.glow{step}" for step in reversed(range(self.GLOW_STEPS))))
        return list(bar.layout.render("divider", values, max(0, shutil.get_terminal_size((80, 20)).columns - 2), styles, ramp=ramp))

    def idle_divider_fragments(self) -> StyleAndTextTuples:
        return self.sweep_divider_fragments("", running=False)

    def queue_divider_fragments(self, queued: int = 0, next_turn: int = 0) -> StyleAndTextTuples:
        tui = self.presentation.tui
        status = tui.status_label if tui is not None and tui.status_label else "working"
        activity, rate = status, ""
        if status in {"working", "retrying", "compacting context"}:
            retry_status = self.presentation.status_bar.retry_status()
            attempt_status = self.presentation.status_bar.model_attempt_status()
            phase = self.presentation.model_stream_kind
            activity = retry_status or (
                ({"reasoning": "thinking", "output": "responding"}.get(phase, phase) or status) + (" · " + attempt_status if attempt_status else "")
            )
            # The elapsed time says how long the wait has been; the rate says whether it is moving.
            # Empty whenever nothing is streaming (between requests, non-streaming providers, a
            # summary), so the divider never claims a speed it is not currently seeing.
            rate = self.presentation.status_bar.output_rate()
            label = f"{activity} ({Text.elapsed_since(self.presentation.status_bar.started_at)}{' · ' + rate if rate else ''})"
        elif status == "running script":
            # Timed like the working phases, but never relabelled from the stream phase: nothing is
            # streaming while a script runs, so the last kind seen would be stale, not current.
            label = f"{status} ({Text.elapsed_since(self.presentation.status_bar.started_at)})"
        elif status == RESUME_STATUS_LABEL:
            # A quiet gray lead-in to the restored transcript: nothing streams and nothing sweeps
            # while the replay is being prepared, so no pulse pretends there is activity.
            return [("class:muted", status)]
        else:
            label = status
        if self.session.context_reset_requested:
            label += " · reset pending"
        counts = [f"{queued} queued"] if queued else []
        if next_turn:
            # Held-back inputs are invisible to the running turn, so they must not inflate the
            # count of follow-ups waiting for the next model step.
            counts.append(f"{next_turn} next turn")
        if counts:
            label = f"{label} [ {' · '.join(counts)} ]"
        label_style = "class:divider.working"
        return self.sweep_divider_fragments(
            label,
            prefix=self.waiting_pulse_fragments(),
            label_style=label_style,
            fields={"activity": activity, "rate": rate, "queue.total": queued + next_turn, "queue.followup": queued, "queue.next_turn": next_turn},
        )

    def followup_fragments(self) -> tuple[StyleAndTextTuples, StyleAndTextTuples]:
        pending = list(self.session.pending_user_inputs)

        def render(items: list[QueuedInput], marker: str, marker_style: str) -> StyleAndTextTuples:
            fragments: StyleAndTextTuples = []
            for item in items:
                # A held-back input starts the next turn, not this one; its own marker and label
                # keep the two queues apart on screen.
                item_marker, item_style = ("↪ next turn · ", "class:muted") if item.next_turn else (marker, marker_style)
                indent = " " * get_cwidth(item_marker)
                # The user's own form: a folded paste or an attached image stays a chip here,
                # not the entry's flattened text, so a queued paste never floods the live region.
                for index, line in enumerate(item.user_input().display_text().splitlines()):
                    fragments.extend([("", "\n"), (item_style, item_marker if index == 0 else indent), (UiPrinter.user_log_style(), line)])
            return fragments

        sent = [item for item in pending if item.inflight]
        queued = [item for item in pending if not item.inflight]
        transcript = render(sent, UiPrinter.USER_LOG_PREFIX, "class:prompt")
        # The divider is a standing boundary for the whole turn. Only messages that have not entered
        # a model request remain below it; sent messages render above it until the request commits them.
        waiting = self.queue_divider_fragments(
            sum(1 for item in queued if not item.next_turn),
            sum(1 for item in queued if item.next_turn),
        )
        if queued:
            # A blank row lifts the queued block off the divider, so the queue reads as its own
            # region below the boundary instead of a list glued to the divider's label.
            waiting.append(("", "\n"))
        waiting.extend(render(queued, "+ ", UiPrinter.user_log_style()))
        return transcript, waiting

    def tui_activity_fragments(self) -> StyleAndTextTuples:
        sent, waiting = self.followup_fragments()
        fragments = sent
        stream = self.model_stream_fragments()
        if fragments:
            fragments.append(("", "\n"))
            # A blank row lifts the echoed follow-up off whatever follows it: the streamed reply,
            # or the standing divider when no stream exists yet. The divider always sits below,
            # so the gap can never leave a hanging blank row at the end of the activity region.
            fragments.append(("", "\n"))
        fragments.extend(stream)
        if stream:
            fragments.append(("", "\n"))
        with self.presentation.live_preview.lock:
            rows = self.presentation.live_preview.frame_rows() if self.presentation.live_preview.active else []
        for row in rows:
            fragments.extend([*row, ("", "\n")])
        if rows:
            fragments.append(("", "\n"))
            if self.presentation.tui is not None:
                fragments.append(("", "\n" * self.presentation.tui.activity_gap_rows))
        fragments.extend(waiting)
        return fragments

    def model_stream_fragments(self) -> StyleAndTextTuples:
        text = self.presentation.model_stream_text
        kind = self.presentation.model_stream_kind
        if not text:
            return []
        width = max(20, shutil.get_terminal_size((120, 20)).columns)
        # Drawn with LogBlock's own rail so it cannot drift from the tree every tool call draws.
        # The rows carry CONTINUE (`│`) and nothing carries BRANCH: `├` is a T-junction, and there
        # is no line above the block for one to join. Nor does a `└` close it - the stream is still
        # arriving, and an end cap would say it had finished.
        rail = LogBlock.prefix(TurnBox.CONTENT_LEVEL + 1, LogEdge.CONTINUE)
        rows = [Text.clip_width(line.expandtabs(4), max(1, width - len(rail) - 1)) for line in text.replace("\r", "\n").splitlines()[-6:]]
        # The spark's row is the region's own, never the text's: a gray word beside the spark names
        # the phase (the same wording the divider below uses), and the first streamed line can arrive
        # on the next row down instead of racing for whatever room the spark leaves. The rows are
        # what the reader is actually reading, and breathing those would be a strobe rather than a
        # sign of life.
        spark = LogBlock.margin(TurnBox.CONTENT_LEVEL + 1) + LiveSpark.glyph(self.session.state.stream_started_at)
        label = {"reasoning": "thinking", "output": "responding"}.get(kind)
        fragments: StyleAndTextTuples = [
            # Anchored to the stream this preview is showing, which ModelClient stamps on the
            # first chunk of each request and clears between them, so every response opens the
            # spark at its crest instead of wherever the wall clock happened to be.
            (LiveSpark.style(self.session.state.stream_started_at), spark),
            *([("class:muted", label)] if label else []),
            ("", "\n"),
            # A blank row keeps the spark off the rail: the star caps the region, it does not sit
            # on it. The running-command frame draws the same gap whenever it has output.
            ("", "\n"),
        ]
        for row in rows:
            fragments.append(("class:muted", rail))
            fragments.extend(self._stream_inline_fragments(row))
            fragments.append(("", "\n"))
        return fragments

    @classmethod
    def _stream_inline_fragments(cls, row: str) -> StyleAndTextTuples:
        """One preview row as fragments: plain gray text with closed inline markdown tokens
        marked in the same gray tones (weight and underline, no color), so the region stays
        uniformly gray. A marker without its closing partner stays literal, so a stream that has
        not finished a token never toggles its style from frame to frame."""
        fragments: StyleAndTextTuples = []
        cursor = 0
        for match in cls.STREAM_INLINE_RE.finditer(row):
            if match.start() > cursor:
                fragments.append(("class:muted", row[cursor : match.start()]))
            token = match.group(1)
            if token.startswith("**"):
                fragments.append(("class:muted bold", token[2:-2]))
            elif token.startswith("`"):
                fragments.append(("class:muted underline", token[1:-1]))
            else:
                fragments.append(("class:muted italic", token[1:-1]))
            cursor = match.end()
        if cursor < len(row):
            fragments.append(("class:muted", row[cursor:]))
        return fragments or [("class:muted", row)]

    def tui_input_hint(self) -> str:
        tui = self.presentation.tui
        if tui is None:
            return ""
        if tui.input_mode == InputMode.RUNNING:
            if any(not item.inflight for item in self.session.pending_user_inputs):
                return self.QUEUE_PENDING_HINT
            return self.QUEUE_EMPTY_HINT
        if tui.input_mode == InputMode.CHAT:
            if self.presentation.starting:
                return STARTING_STATUS_LABEL
            return self._hint_picker.pick(self._hint_context(), self.session.state.round_count)
        return ""

    def _hint_context(self) -> HintContext:
        """Project the session into the small situation the hint mechanism selects on."""
        session = self.session
        return HintContext(
            early=not session.tool_records,
            skills_available=bool(session.skills and session.skills.skills),
            mcp_connected=bool(session.mcp and session.mcp.tools),
            jobs_running=any(job.status == "running" for job in session.jobs.values()),
        )

    def style(self) -> Style:
        """The one prompt-toolkit style map, built on the palette's semantic classes.

        `Theme.tui_styles()` supplies the vocabulary (`muted`, `accent`, `error`, …); the map below
        names the view's own widgets in terms of it. Light and dark therefore differ only inside the
        palette, never in a branch here.
        """
        # Component auto/pair themes follow a late terminal reply even with a fixed global theme.
        key = Theme.key(), Theme.detect()
        if self._style is None or self._style[0] != key:
            self._style = key, self._build_style()
        return self._style[1]

    def _build_style(self) -> Style:
        role = Theme.inline
        menu_bg = Theme.color("menu_bg")
        divider = Theme.bar_palette("divider").colors
        return Style.from_dict(
            {
                **Theme.tui_styles(),
                "prompt": role("accent", "bold"),
                # The comet fades into the rule it travels over, so both come from the palette.
                "queue.rule": f"fg:{divider['divider_rule']}",
                **{f"divider.glow{step}": color for step, color in enumerate(Theme.ramp("divider_glow", "divider_rule", self.GLOW_STEPS, kind="divider"))},
                "queue.hint": role("muted"),
                "quickhint": role("accent"),
                "quickhint.focused": Theme.selection(),
                "quickhint.sep": role("muted"),
                "image.attachment": role("accent", "bold"),
                "input.error": role("error"),
                # The command word of a `/` draft, tinted as it is typed: the accent once it names
                # a command, red once no spelling would accept it.
                "input.command": role("accent"),
                "input.command.unknown": role("error"),
                "divider.working": f"fg:{divider['divider_label']} bold",
                "approval": role("warning"),
                "approval.wait": role("accent_secondary"),
                "approval.action": role("warning"),
                "approval.action.focused": Theme.selection(),
                "approval.action.dim": role("muted"),
                "choice.title": role("accent", "bold"),
                "choice.selected": Theme.selection(),
                "choice.disabled": role("muted"),
                # A section header is set apart from the muted legend by weight, not a new color.
                "choice.header": role("muted", "bold"),
                "choice.number": role("muted"),
                "choice.preview": role("success", "italic"),
                "choice.explanation": role("accent_secondary"),
                # The preview's user messages take the same tone as the transcript's `• ` lines.
                "choice.user": role("user"),
                # The Ctrl-O browser's rows, coloured as the transcript colours the same call: the
                # tr.N key dim, the tool name in the tool colour, the arguments plain. A row that is
                # still running takes the working divider's tone instead of the dim key.
                "choice.meta": role("muted"),
                "choice.tool": role("tool"),
                "choice.live": role("accent_secondary", "bold"),
                "choice.output.ok": role("success", "bold"),
                "choice.output.fail": role("error", "bold"),
                "detail.title": role("accent", "bold") + f" bg:{Theme.color('user_bg')}",
                "detail.title.name": role("text") + " nobold",
                "detail.title.meta": role("muted") + " nobold",
                "detail.border": role("rule"),
                "detail.section": role("accent_secondary", "bold"),
                "detail.field": role("muted", "bold"),
                "detail.source": f"bg:{Theme.color('user_bg')}",
                "choice.status.connected": role("success", "bold"),
                "choice.status.connecting": role("success", "bold"),
                "choice.status.disconnected": role("warning", "bold"),
                "choice.status.disconnecting": role("warning", "bold"),
                "choice.status.error": role("error", "bold"),
                "choice.status.skipped": role("muted"),
                # A tab is not a list row: it keeps the accent it is drawn in and inverts it.
                "tab.active": role("accent", "bold", "reverse"),
                "tab.inactive": role("accent"),
                # The menu floats over the transcript and the input, so it sits on a surface of its
                # own; with the terminal's background it read as more text in the same place.
                "completion-menu": f"noreverse bg:{menu_bg}",
                "completion-menu.completion": f"noreverse bg:{menu_bg} {role('text')}",
                "completion-menu.completion.current": "noreverse " + Theme.selection(),
                "completion-menu.meta.completion": f"noreverse bg:{menu_bg} {role('menu_muted')}",
                "completion-menu.meta.completion.current": "noreverse " + Theme.selection(),
                "completion-menu.hint": f"noreverse bg:{menu_bg} {role('menu_muted')}",
                # prompt_toolkit's own scrollbar is a light-grey track under a dark thumb, fixed
                # colors that match no terminal theme: the track is the menu's surface, the thumb grey.
                "scrollbar.background": f"noreverse bg:{menu_bg}",
                "scrollbar.button": f"noreverse bg:{Theme.color('subtle')}",
                "bottom-toolbar": "noreverse bg:default fg:default",
                "bottom-toolbar.text": f"noreverse bg:default {role('text')}",
                "search-toolbar": "noreverse bg:default fg:default",
                "search-toolbar.prompt": role("accent"),
                "search-toolbar.text": role("text"),
            }
        )
