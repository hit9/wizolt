"""`/status`: one snapshot of the selected agent, shown in tabs inside one frame.

`StatusSnapshot.collect` reads everything at once, so a report never mixes two moments or two
agents. Rendering only lays that snapshot out. Interactively, `StatusView` shows one tab at a time,
Overview first; elsewhere `StatusReport` prints every tab as a section of the same frame, laid out
again when the pane resizes and plain text without color.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.dimension import to_dimension
from prompt_toolkit.utils import get_cwidth

from wizolt.base import LogBlock, ModelUsage, Text, TextFragments, TextRows, TurnBox
from wizolt.config import compaction_provider_config
from wizolt.formats import clip
from wizolt.sdk import Panel
from wizolt.ui.cli.modals import picker_height
from wizolt.ui.cli.update import UpdateChecker
from wizolt.ui.render import Theme, UiPrinter, WidthDependent
from wizolt.ui.tui import TUI_MODAL_PENDING, TabbedViewState, TuiApp

if TYPE_CHECKING:
    from wizolt.ui.cli import CommandLoop

DOCS_URL = "https://wizolt.readthedocs.io"
TABS = ("Overview", "Progress", "Context", "Usage", "Session")
# A plan step's marker and theme role; `doing` takes the accent the running agent wears.
STEP_MARKS = {"done": ("✓", "success"), "doing": ("●", "accent"), "blocked": ("✕", "error"), "todo": ("○", "muted")}
KEYS = "h/l tabs · j/k scroll · Esc close"
# The statusbar's context pressure thresholds (`ui.bars.pressure`).
WARNING_PERCENT, ERROR_PERCENT = 70, 90
# Values wear existing theme roles: counts the color numbers have in tool arguments, cache and
# context what the statusbar uses for them, agent states what `/agents` uses.
NUMBER_ROLE = "syntax_number"
STATE_ROLES = {"running": "accent", "waiting for input": "warning", "failed": "error"}  # other states stay muted
UNAVAILABLE = {"none yet", "n/a"}
FRAME_WIDTH = 84
OVERVIEW_METER = 24


class Row(NamedTuple):
    label: str
    value: str


@dataclass(frozen=True)
class Section:
    title: str  # "" when the tab's own name says it
    rows: tuple[Row, ...]
    numeric: bool = False  # right-align the values

    def entries(self) -> Group:
        if not self.numeric:
            return self.title, [Entry(row.label, text(row.value)) for row in self.rows]
        return self.title, [Entry(row.label, [figure(row.value, "status_cache" if "cache" in row.label else NUMBER_ROLE)], True) for row in self.rows]


@dataclass(frozen=True)
class ContextReading:
    """How full the window is, and what the next request is estimated to hold.

    `used`/`budget` are the provider's report of the last request when there is one (`reported`),
    else the session's estimate -- the statusbar's reading. `parts` estimate the next request
    locally; they are a different measurement and are never scaled to match `used`."""

    used: int
    budget: int
    percent: int
    reported: bool
    window: int
    threshold: int
    parts: tuple[tuple[str, int], ...]

    @property
    def level(self) -> str:
        """The statusbar's context color, until the reading nears or passes the threshold."""
        return "error" if self.percent >= ERROR_PERCENT else "warning" if self.percent >= WARNING_PERCENT else "status_context"

    def reading(self) -> TextFragments:
        used = ("" if self.reported else "~") + Text.abbreviate_count(self.used)
        level = Theme.fg(self.level, "bold")
        return [(level, used), words(" / "), figure(Text.abbreviate_count(self.budget)), words(" · "), (level, f"{self.percent}%")]


class Route(NamedTuple):
    """Where requests go, kept in parts so each can wear the statusbar's color for it."""

    provider: str
    model: str
    api: str
    effort: str


class GroupCounts(NamedTuple):
    total: int
    running: int
    waiting: int


@dataclass(frozen=True)
class StatusSnapshot:
    agent: str
    state: str
    route: Route
    goal: str
    context: ContextReading
    totals: ModelUsage  # a copy, so the report never sees a later request
    group: GroupCounts | None  # None for an agent working alone
    usage: tuple[Section, ...]
    activity: Section
    configuration: Section
    plugins: tuple[Panel, ...] = ()
    # The agent's own note, as it last wrote it: (status, text) steps, facts it keeps, its check.
    plan: tuple[tuple[str, str], ...] = ()
    known: tuple[str, ...] = ()
    check: str = ""

    @classmethod
    def collect(cls, loop: CommandLoop) -> StatusSnapshot:
        session = loop.session
        # Rebuild the estimate after resume; context_fill prefers the last actual request.
        loop.agent.context.update_current_tokens(session.system_prompt)
        fill = session.context_fill()
        provider = session.config.provider
        context = ContextReading(
            used=fill["used"],
            budget=fill["budget"],
            percent=fill["percent"],
            reported=bool(session.usage.last_prompt_tokens and session.usage.last_prompt_budget),
            window=provider.context_token_limit(session.settings.max_context_tokens),
            threshold=session.request_token_budget(),
            parts=tuple(loop.agent.context.breakdown(session.system_prompt)),
        )
        usage = [Section("All requests", usage_rows(session.usage), numeric=True)]
        if session.compaction_usage.calls:
            # Summaries may run on another entry, priced apart; name the model they ran on. Their
            # cache rows are the only evidence that a summary rode the conversation's cached prefix.
            model = compaction_provider_config(session.config).model or "(no model)"
            usage.append(Section(f"Compaction · {model}", usage_rows(session.compaction_usage), numeric=True))
        counts = session.subagents.counts if session.subagents is not None else None
        return cls(
            agent=session.agent_name,
            state=str(loop.presentation.status_bar.values()["agent.state"]),
            route=Route(session.config.active_provider, provider.model or "(empty)", session.policy.resolve(provider).api, provider.reasoning),
            goal=session.state.goal,
            context=context,
            totals=replace(session.usage),
            group=GroupCounts(counts.total, counts.running, counts.waiting) if counts and counts.total > 1 else None,
            usage=tuple(usage),
            activity=Section("Activity", activity_rows(loop), numeric=True),
            configuration=Section("", configuration_rows(loop)),
            plugins=tuple(session.plugins.panels("status")) if session.plugins is not None else (),
            plan=tuple((item.status, item.text) for item in session.state.plan_items(session.state.plan)),
            known=tuple(session.state.known),
            check=session.state.check,
        )


def usage_rows(usage: ModelUsage) -> tuple[Row, ...]:
    """Cumulative counts. Ratios are unavailable, not zero, before a request reports them."""
    if not usage.calls:
        return (Row("requests", "none yet"),)

    def ratio(part: int, whole: int) -> str:
        # Whole percents, like the statusbar's and the Overview's readings.
        return f"{part * 100 / whole:.0f}%" if whole else "n/a"

    rows = [
        Row("requests", str(usage.calls)),
        Row("input", Text.abbreviate_count(usage.prompt_tokens)),
        Row("output", Text.abbreviate_count(usage.completion_tokens)),
        Row("cache read", ratio(usage.cached_prompt_tokens, usage.prompt_tokens)),
        Row("last cache read", ratio(usage.last_cached_prompt_tokens, usage.last_prompt_tokens)),
    ]
    if usage.cache_write_prompt_tokens:
        rows.append(Row("cache write", Text.abbreviate_count(usage.cache_write_prompt_tokens)))
    return tuple(rows)


def activity_rows(loop: CommandLoop) -> tuple[Row, ...]:
    session = loop.session
    mcp = session.mcp
    jobs = len(session.running_jobs())
    counts = [
        ("messages", len(session.messages)),
        ("this turn", session.state.turn_messages),
        ("tool results", len(session.tool_results)),
        ("jobs", f"{jobs} running of {len(session.jobs)}" if session.jobs else 0),
        ("mcp servers", sum(mcp.connected(config.name) for config in mcp.parse_configs()) if mcp else 0),
        ("skills", len(session.skills.skills) if session.skills else 0),
        ("hooks", len(session.shell_hooks.active(session)) if session.shell_hooks else 0),
        ("known facts", len(session.state.known)),
        ("compactions", session.state.compaction_count),
    ]
    # Messages always show; the rest only once there is something to count.
    rows = [Row(label, str(value)) for label, value in counts if value or label == "messages"]
    # Group rows count every agent sharing this session's group, not just the selected one; an
    # agent working alone has only its subagent allowance to show.
    group = session.subagents
    if group is not None:
        counts = group.counts
        if counts.total > 1:
            rows += [Row("group agents", str(counts.total)), Row("group running", str(counts.running)), Row("group waiting", str(counts.waiting))]
        rows.append(Row("group subagents", f"{group.active}/{group.limit} running" if group.limit else "off"))
    return tuple(rows)


def configuration_rows(loop: CommandLoop) -> tuple[Row, ...]:
    session = loop.session
    rows = [Row("workspace", session.cwd), Row("session", session.uid)]
    if session.subagents is not None and (parent := session.subagents.entry(session.uid).parent):
        rows.append(Row("parent", parent))
    rows.append(Row("permissions", "yolo: no confirmations" if session.settings.yolo else "confirm risky actions"))
    rows.append(Row("limits", f"{session.settings.max_steps} steps · {session.settings.max_parallel_tools} parallel tools"))
    info = session.system_info
    global_path = info.agents_md_global_path if info is not None else ""
    global_exists = bool(global_path and os.path.isfile(global_path))
    if session.settings.agents_md:
        sources = []
        if info is not None:
            sources.extend(file.display for file in info.agents_md_project)
            if info.agents_md_global_display:
                sources.append("global active" if global_exists else "global active (file removed)")
            else:
                sources.append("global next session" if global_exists else "global missing")
        rows.append(Row("agents.md", f"on ({'; '.join(sources)})"))
    else:
        rows.append(Row("agents.md", f"off (global {'present' if global_exists else 'missing'})"))
    update = UpdateChecker(session.data_path(), loop.presentation.update).status_line().removeprefix("update: ")
    if update not in {"current", "unknown"}:
        rows.append(Row("update", update))
    # The command reference lives in the documentation, so every report ends with where to read it.
    rows.append(Row("docs", DOCS_URL))
    return tuple(rows)


class StatusTabs:
    """Each tab's rows for a snapshot, at the width inside the frame."""

    def __init__(self, snapshot: StatusSnapshot) -> None:
        self.snapshot = snapshot
        # One label column for every tab, so the value column never jumps between tabs or between
        # the sections of a printed report; the widest label anywhere sets it.
        sections = [row.label for section in (*snapshot.usage, snapshot.activity, snapshot.configuration) for row in section.rows]
        parts = [name for name, tokens in snapshot.context.parts if tokens]
        fixed = ["agent", "model", "goal", "context", "usage", "agents", "used", "total", "over by", "compacts at", "window"]
        fixed += ["progress", "plan", "known", "check"]
        self.column = label_column([*sections, *parts, *fixed])

    def rows(self, tab: str, width: int) -> TextRows:
        tabs = {"Overview": self.overview, "Progress": self.progress, "Context": self.context, "Usage": self.usage, "Session": self.session}
        return tabs[tab](width)

    def progress(self, width: int) -> TextRows:
        """The note the agent keeps for itself: what it is after, how far along, what it knows."""
        snapshot = self.snapshot
        if not (snapshot.goal or snapshot.plan or snapshot.known or snapshot.check):
            return [[words("No plan yet. The agent writes its goal, steps and checks here as it works.")]]
        entries = [Entry("goal", text(snapshot.goal) if snapshot.goal else [words("none yet")])]
        if snapshot.plan:
            counts = {status: sum(step == status for step, _ in snapshot.plan) for status in STEP_MARKS}
            summary = [figure(str(counts["done"])), words(f" of {len(snapshot.plan)} done")]
            for status in ("doing", "blocked"):
                if counts[status]:
                    summary += [words(" · "), figure(str(counts[status]), STEP_MARKS[status][1]), words(f" {status}")]
            # Like Overview's meter: it takes what the counts leave, and gives way when narrow.
            cells = min(OVERVIEW_METER, len(snapshot.plan) * 4, width - self.column - 2 - width_of(summary))
            if cells >= 6:
                filled = round(counts["done"] * cells / len(snapshot.plan))
                summary = [(Theme.fg("success"), "█" * filled), (Theme.fg("subtle"), "░" * (cells - filled)), ("", "  "), *summary]
            entries.append(Entry("progress", summary))
            for index, (status, step) in enumerate(snapshot.plan):
                mark, role = STEP_MARKS.get(status, STEP_MARKS["todo"])
                # Finished steps recede; the live and blocked ones keep full weight.
                entries.append(Entry("plan" if index == 0 else "", [(Theme.fg(role), mark + " "), (Theme.fg("muted" if status == "done" else "text"), step)]))
        entries += [Entry("known" if index == 0 else "", [words("• "), *text(fact)]) for index, fact in enumerate(snapshot.known)]
        if snapshot.check:
            entries.append(Entry("check", text(snapshot.check)))
        return table([("", entries)], width, self.column)

    def overview(self, width: int) -> TextRows:
        """What most visits want: who, on what, how full, how much so far."""
        snapshot, context, route = self.snapshot, self.snapshot.context, self.snapshot.route
        entries = [
            Entry(
                "agent",
                [(Theme.fg("status_agent", "bold"), snapshot.agent), words(" · "), (Theme.fg(STATE_ROLES.get(snapshot.state, "muted")), snapshot.state)],
            ),
            # The statusbar's colors for the same parts, so the two read alike.
            Entry(
                "model",
                [
                    (Theme.fg("status_provider"), route.provider),
                    words(" / "),
                    (Theme.fg("status_model", "bold"), route.model),
                    words(" · "),
                    (Theme.fg("text"), route.api),
                    words(" · effort "),
                    (Theme.fg("status_reason"), route.effort),
                ],
            ),
        ]
        if snapshot.goal:
            entries.append(Entry("goal", text(snapshot.goal)))
        reading = context.reading()
        # The meter draws the reading beside it, not the estimated parts the Context tab breaks
        # down: the two can differ. It takes what the reading leaves, and gives way when narrow.
        cells = min(OVERVIEW_METER, width - self.column - 1 - width_of(reading))
        filled = round(min(100, context.percent) * cells / 100)
        meter = [(Theme.fg(context.level), "█" * filled), (Theme.fg("subtle"), "░" * (cells - filled))]
        entries += [Entry("context", [*meter, ("", " "), *reading] if cells >= 6 else reading), Entry("usage", self.traffic())]
        if (group := snapshot.group) is not None:
            agents = [figure(str(group.total)), words(" · "), figure(str(group.running), "accent"), words(" running · ")]
            agents += [figure(str(group.waiting), "warning" if group.waiting else NUMBER_ROLE), words(" waiting")]
            entries.append(Entry("agents", agents))
        return table([("", entries)], width, self.column)

    def traffic(self) -> TextFragments:
        """The usage totals in one line: counts, then the share served from cache."""
        totals = self.snapshot.totals
        if not totals.calls:
            return [words("no requests yet")]
        line = [figure(str(totals.calls)), words(" request" + ("s" if totals.calls > 1 else "") + " · ")]
        line += [figure(Text.abbreviate_count(totals.prompt_tokens)), words(" in · "), figure(Text.abbreviate_count(totals.completion_tokens)), words(" out")]
        if totals.prompt_tokens:
            line += [words(" · "), figure(f"{totals.cached_prompt_tokens * 100 / totals.prompt_tokens:.0f}%", "status_cache"), words(" cached")]
        return line

    def context(self, width: int) -> TextRows:
        """The reading over a stacked bar of the estimated parts, measured against the compaction
        threshold; a row per part, then the limits behind both."""
        context, muted = self.snapshot.context, Theme.fg("muted")
        # Unsupported parts are left out rather than listed as a measured zero.
        parts = [(name, tokens) for name, tokens in context.parts if tokens]
        estimated = sum(tokens for _, tokens in parts)
        threshold = max(1, context.threshold)
        source = "last request, reported" if context.reported else "estimated"
        reading = [*context.reading(), (muted, f"  {source}")]
        counts = [Text.abbreviate_count(tokens) for _, tokens in parts]
        count_width = max(map(len, counts), default=0)
        over = estimated > context.threshold
        bar_width = max(1, width - self.column)
        # A part too small for a cell is not in the bar; its square stays quiet to say so.
        colors = self.colors(bar_width)
        estimate = [
            Entry(
                name,
                [
                    (colors.get(name, Theme.fg("subtle")), "■ "),
                    figure(f"{count:>{count_width}}"),
                    (muted, f"  {tokens * 100 / threshold:>3.0f}%" if tokens * 100 >= threshold else "   <1%"),
                ],
            )
            for (name, tokens), count in zip(parts, counts, strict=True)
        ]
        estimate.append(Entry("total", [figure("~" + Text.abbreviate_count(estimated))]))
        if over:
            estimate.append(Entry("over by", [(Theme.fg("error", "bold"), Text.abbreviate_count(estimated - context.threshold))]))
        # The threshold turns as the reading does: it is what that warning is about.
        limits = [
            Entry("compacts at", [figure(Text.abbreviate_count(context.threshold), context.level if context.percent >= WARNING_PERCENT else NUMBER_ROLE)]),
            Entry("window", [figure(Text.abbreviate_count(context.window))]),
        ]
        used = [Entry("used", reading), Entry("", self.bar(bar_width))]
        return table([("", used), ("Next request · estimated", estimate), ("Limits", limits)], width, self.column)

    def usage(self, width: int) -> TextRows:
        return table([section.entries() for section in (*self.snapshot.usage, self.snapshot.activity)], width, self.column)

    def session(self, width: int) -> TextRows:
        from wizolt.ui.cli.plugins import PluginView

        rows = table([self.snapshot.configuration.entries()], width, self.column)
        if self.snapshot.plugins:
            rows += [[], [(Theme.fg("accent"), "Plugins")]]
            for panel in self.snapshot.plugins:
                rows.extend(PluginView.render([Panel((row,))], width, 1) for row in panel.rows)
        return rows

    def segments(self, width: int) -> list[tuple[str, int]]:
        """The parts drawn at `width`, with their cells. Each takes the cells its cumulative share
        rounds to, so a part too small for a cell gets none instead of distorting the others."""
        threshold = max(1, self.snapshot.context.threshold)
        segments: list[tuple[str, int]] = []
        start = total = 0
        for name, tokens in self.snapshot.context.parts:
            total += tokens
            end = min(width, round(total * width / threshold))
            if end > start:
                segments.append((name, end - start))
                start = end
        return segments

    def colors(self, width: int) -> dict[str, str]:
        """Styles for the parts drawn at `width`, from a generated series (`Theme.series`) in bar
        order, so each part differs most from its neighbors. A part too small for a cell is not
        drawn and takes no color."""
        drawn = [name for name, _ in self.segments(width)]
        return {name: "fg:" + color for name, color in zip(drawn, Theme.series(len(drawn)), strict=True)}

    def bar(self, width: int) -> TextFragments:
        colors = self.colors(width)
        fragments: TextFragments = [(colors[name], "█" * cells) for name, cells in self.segments(width)]
        used = sum(cells for _, cells in self.segments(width))
        if used < width:
            fragments.append((Theme.fg("subtle"), "░" * (width - used)))
        return fragments


class Entry(NamedTuple):
    label: str
    value: TextFragments
    numeric: bool = False


Group = tuple[str, Sequence[Entry]]  # a heading ("" for none) and its entries


def text(value: str) -> TextFragments:
    return [(Theme.fg("text"), value)]


def figure(value: str, role: str = NUMBER_ROLE) -> tuple[str, str]:
    """A measured value in its theme role; an unavailable one stays quiet."""
    return Theme.fg("muted" if value in UNAVAILABLE else role), value


def words(value: str) -> tuple[str, str]:
    return Theme.fg("muted"), value


def label_column(labels: Sequence[str]) -> int:
    """Where a tab's values start: past its longest label, and two spaces."""
    return max(map(get_cwidth, labels), default=0) + 2


def table(groups: Sequence[Group], width: int, column: int = 0) -> TextRows:
    """A tab's entries in one label column and one value column; numbers right-aligned to the
    widest of them, long values wrapped under their own column. A heading opens each named group.
    `column` fixes where values start for every tab alike; 0 sizes it to this table's labels."""
    entries = [entry for _, group in groups for entry in group]
    column = column or label_column([entry.label for entry in entries])
    number = max((width_of(entry.value) for entry in entries if entry.numeric), default=0)
    rows: TextRows = []
    for heading, group in groups:
        if rows:
            rows.append([])
        if heading:
            rows.append([(Theme.fg("text", "bold"), heading)])
        for entry in group:
            value = [("", " " * (number - width_of(entry.value))), *entry.value] if entry.numeric else entry.value
            rows += Text.wrap_styled([(Theme.fg("muted"), entry.label.ljust(column))], [("", " " * column)], value, width)
    return rows


def frame(body: TextRows, width: int, *, footer: str = "") -> TextRows:
    """`body` inside the report's border, `width` columns in all; `footer` sits in the bottom edge."""
    # A structure line, not an accent: quiet like the keys in its own edge, so color stays on the
    # tabs and the values.
    border = Theme.fg("muted")
    inside = max(1, width - 4)
    rows: TextRows = [[(border, "╭" + "─" * (width - 2) + "╮")]]
    for row in body:
        row = clip(row, inside)
        rows.append([(border, "│ "), *row, ("", " " * max(0, inside - width_of(row))), (border, " │")])
    keys = f" {Text.clip_width(footer, width - 6)} " if footer else ""
    rows.append([(border, "╰" + "─" * max(0, width - 3 - get_cwidth(keys))), (Theme.fg("muted"), keys), (border, "─╯")])
    return rows


def frame_width(width: int) -> int:
    """The frame's columns for `width` available ones: its borders and padding take four, so it
    keeps one cell inside however narrow the pane, and never outgrows what it is given."""
    return max(5, min(width, FRAME_WIDTH))


@dataclass(frozen=True)
class StatusReport(WidthDependent):
    """Every tab as a section of one frame: the transcript's copy of a report."""

    snapshot: StatusSnapshot

    MARGIN: ClassVar[str] = LogBlock.margin(TurnBox.CONTENT_LEVEL)

    def text(self, width: int = 80) -> str:
        return "".join(fragment[1] for fragment in self.fragments(width))

    @classmethod
    def of(cls, loop: CommandLoop) -> StatusReport:
        return cls(StatusSnapshot.collect(loop))

    def fragments(self, width: int) -> StyleAndTextTuples:
        outer = frame_width(width - len(self.MARGIN))
        tabs = StatusTabs(self.snapshot)
        body: TextRows = []
        for tab in TABS:
            if body:  # Overview needs no heading: it opens the report
                body += [[], [(Theme.fg("accent", "bold"), tab)]]
            body += tabs.rows(tab, outer - 4)
        return [fragment for row in frame(body, outer) for fragment in (("", self.MARGIN), *row, ("", "\n"))]


class StatusView:
    """The interactive report: one tab at a time in the frame, Overview first. Every tab keeps the
    tallest tab's height, so switching never moves the frame."""

    def __init__(self, loop: CommandLoop, snapshot: StatusSnapshot) -> None:
        self.loop = loop
        self.tabs = StatusTabs(snapshot)
        self.state = TabbedViewState(TABS)

    def size(self) -> tuple[int, int]:
        """Columns and rows for this frame, from the live app when there is one."""
        width, height = shutil.get_terminal_size((80, 24)).columns, picker_height()
        tui = self.loop.presentation.tui
        if tui is not None and tui.app is not None:
            size = tui.app.output.get_size()
            width, height = size.columns, TuiApp.modal_rows(size.rows)
            if tui.modal_window is not None:
                height = min(height, to_dimension(tui.modal_window.height).max)
        return width, height

    def fragments(self) -> StyleAndTextTuples:
        width, height = self.size()
        outer = frame_width(width - 2)
        inside = outer - 4
        tallest = max(len(self.tabs.rows(tab, inside)) for tab in TABS)
        # Two rows are the frame's edges, two the tab row and the gap beneath it.
        room = max(1, min(tallest, height - 4))
        rows = self.tabs.rows(TABS[self.state.tab], inside)
        self.state.scroll = min(self.state.scroll, max(0, len(rows) - room))
        visible = rows[self.state.scroll : self.state.scroll + room]
        body = [UiPrinter.tab_segments(TABS, self.state.tab), [], *visible, *([[]] * (room - len(visible)))]
        return [fragment for row in frame(body, outer, footer=KEYS) for fragment in (("", "  "), *row, ("", "\n"))]

    def handle_key(self, key: str, data: str = "") -> Any:
        if key in {"escape", "q", "c-c", "enter"}:
            return None
        if key in {"h", "left", "s-tab", "l", "right", "tab"}:
            self.state.switch(-1 if key in {"h", "left", "s-tab"} else 1)
        elif key in {"j", "down", "k", "up"}:
            self.state.scroll_by(1 if key in {"j", "down"} else -1)
        elif (number := data or key).isdigit() and 1 <= int(number) <= len(TABS):
            self.state.tab, self.state.scroll = int(number) - 1, 0
        return TUI_MODAL_PENDING


def width_of(row: TextFragments) -> int:
    return sum(get_cwidth(text) for _, text in row)
