"""wizolt base: errors, text helpers, configuration, and shared data types."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum, auto
from itertools import groupby
from typing import Any, ClassVar, TypeVar

__version__ = "0.71.3"

_BlockingT = TypeVar("_BlockingT")
_get_cwidth: Callable[[str], int] | None = None


def _cwidth(text: str) -> int:
    """Display width of `text` in terminal cells.

    Imported lazily: `prompt_toolkit.utils` drags in the whole prompt_toolkit package (~70ms),
    and base is imported by every module. The first render that actually measures text pays the
    import once."""
    global _get_cwidth
    if _get_cwidth is None:
        from prompt_toolkit.utils import get_cwidth

        _get_cwidth = get_cwidth
    return _get_cwidth(text)


Json = dict[str, Any]
ToolArgs = list[Any]
TextFragments = list[tuple[str, str]]
TextRows = list[TextFragments]
_TextCell = tuple[str, str, int]


class Billing(str, Enum):
    """Where a request's usage lands: which session counter it is added to and whether it may
    overwrite that counter's last-request ctx/cache snapshot.

    Named values rather than two orthogonal booleans, because the three rows differ on both axes
    (see ModelClient._record_usage): MAIN on the main counter, COMPACTION on its own counter, and
    VISION on the main totals without touching the main last-request snapshot.
    """

    MAIN = "main"
    COMPACTION = "compaction"
    VISION = "vision"


HTTP_USER_AGENT = "wizolt/" + __version__


async def run_blocking(invoke: Callable[[], _BlockingT], *, commit: Callable[[_BlockingT], None] | None = None) -> _BlockingT:
    """Await one synchronous callable on the loop's executor, and never abandon it.

    `asyncio.to_thread` returns as soon as the *awaiter* is cancelled, leaving the worker running
    with whatever files, locks, and half-written state it holds -- which is how a maintenance sweep
    ends up mutating a session after shutdown said it was done. Cancellation here means: remember
    it, keep waiting for the worker, observe its outcome, and only then report cancellation upward.

    The same contract as `ToolRunner._run_in_executor`, without tool capacity or `request_stop()`;
    the two stay separate because their cancellation policies differ. Callers keep session, TUI,
    and registry mutation on the loop -- the worker computes or performs one atomic file operation
    and returns.

    `commit` exists for the transactional writers -- snapshot markers, edit receipts -- whose worker
    has an effect on disk that the loop must record even when cancellation is already pending. It
    runs on the loop after a successful worker, before the cancellation is re-raised, and it must be
    bounded, synchronous, and do no I/O: it installs values that were already validated. A commit
    that raises is an invariant failure, not a storage error, and is allowed to propagate."""

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, invoke)
    cancel_error: asyncio.CancelledError | None = None
    while not future.done():
        try:
            # `wait` rather than awaiting the future: it never raises the worker's own outcome, so
            # that outcome is still there to be read off the future below.
            await asyncio.wait({future})
        except asyncio.CancelledError as error:
            cancel_error = cancel_error or error
    if cancel_error is not None:
        # The worker's own failure is a cleanup detail once cancellation is under way; it must not
        # replace the cancellation the caller is unwinding on. Reading the future keeps that
        # exception observed rather than leaving it to the loop's exception handler.
        try:
            result = future.result()
        except BaseException:  # noqa: BLE001 - observed so it is not left unretrieved, then dropped for the cancellation.
            raise cancel_error from None
        if commit is not None:
            commit(result)
        raise cancel_error
    result = future.result()
    if commit is not None:
        commit(result)
    return result


MAX_TOOL_OUTPUT_TOKENS = 6_000
# Extension of the file a truncated tool result is materialized to, named after the result's key.
# ContextManager.materialize_output writes it; the session store's asset collector retains it.
TOOL_OUTPUT_ASSET_SUFFIX = ".txt"
# The append-only index of compacted-history exports, beside each segment's `history.N.md` in the
# same assets directory. wizolt.agent.history writes both; the session store's asset collector retains
# them, and the checkpoint names this index.
HISTORY_INDEX_ASSET = "history.md"
# Cap on the AGENTS.md (or CLAUDE.md fallback) content injected into every request's fixed
# prefix, per design/DESIGN.md's "bound the fixed prefix" rule; truncation happens in
# ContextManager.environment, so SystemInfo.detect returns the file verbatim.
MAX_AGENTS_MD_TOKENS = 8_000
MODEL_REQUEST_RETRIES = 5
# Retry pacing: exponential backoff with jitter; RETRY_MAX_DELAY also clamps provider Retry-After
# values so a single aberrant header cannot stall the CLI for minutes. The wider budget costs
# wall-clock time only, which is visible and interruptible (see model.request()); retransmitted
# request prefixes are cache hits, so tokens are nearly free.
RETRY_BASE_DELAY = 1.0  # seconds; delay = RETRY_BASE_DELAY * 2 ** attempt, then jittered 0.5x-1.5x
RETRY_MAX_DELAY = 30.0  # seconds; single-wait ceiling, also clamps Retry-After
# Assistant turns carry the provider's own reply verbatim under these keys — Responses output
# items and Anthropic content blocks — so tool loops can replay opaque reasoning the protocol
# requires back unmodified. They are wizolt's bookkeeping and never reach a request body.
RESPONSES_OUTPUT_KEY = "_responses_output"
ANTHROPIC_CONTENT_KEY = "_anthropic_content"
# Sources a provider-side search attached to one assistant message. Stored for rendering and resume,
# never replayed: the provider already carries its own search state in the echo keys above.
SEARCH_SOURCES_KEY = "_search_sources"
# Set when the provider ended a response without ending the turn, having paused a long server-side
# tool run. The message must be sent back unchanged to resume, so this travels with it as metadata.
PAUSED_TURN_KEY = "_paused_turn"
# Which endpoint issued the echo keys above: base URL, model, and a key fingerprint. The opaque
# halves of an echo — Responses `encrypted_content`, Anthropic thinking `signature` — are verified
# by their issuer, so replaying one anywhere else is rejected rather than ignored. Protocol alone
# cannot tell issuers apart: `/provider` can move between two Responses hosts, or between two
# entries on one host holding keys for different organizations. See ModelClient.provider_origin.
PROVIDER_ORIGIN_KEY = "_provider_origin"
PROVIDER_ECHO_KEYS = (RESPONSES_OUTPUT_KEY, ANTHROPIC_CONTENT_KEY, SEARCH_SOURCES_KEY, PAUSED_TURN_KEY, PROVIDER_ORIGIN_KEY)


def builtin_function_names(entries: Iterable[Json]) -> tuple[str, ...]:
    """Names of the builtin tools the provider calls back for instead of running entirely alone.

    These entries look like ordinary tool calls but use a client-echo handshake, so both the runner
    (to recognize the call) and the no-tools guard (to keep it) need the declared names."""
    names: list[str] = []
    for entry in entries:
        if entry.get("type") != "builtin_function":
            continue
        function = entry.get("function")
        name = function.get("name") if isinstance(function, dict) else ""
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def builtin_tool_label(name: str) -> str:
    """A display label for a tool the provider runs for itself.

    Protocols may suffix, prefix, or otherwise wrap a common operation name; normalization keeps
    those spellings readable as the same phase in the transcript."""
    return (name.lstrip("$").removesuffix("_call").replace("_", " ").strip() or "provider tool").title()


# Protocol-neutral metadata for lifecycle/context checkpoint messages. Provider adapters remove
# this key while preserving the canonical role/content pair in the conversation log.
SESSION_EVENT_KEY = "_session_event"
SUBAGENT_RECEIPTS_KEY = "_subagent_receipts"


# Image-delivery states of the active main route (REQUIREMENT-3 main-first image fallback).
# UNKNOWN routes optimistically receive raw images first; TEXT_ONLY_STATIC comes from the
# provider compatibility catalog; TEXT_ONLY_LEARNED is session-local evidence created when an
# eligible main request returns HTTP 400 for a request carrying a current-turn raw image.
IMAGE_ROUTE_UNKNOWN = "unknown"
IMAGE_ROUTE_TEXT_ONLY_STATIC = "text_only_static"
IMAGE_ROUTE_TEXT_ONLY_LEARNED = "text_only_learned"


@dataclass(frozen=True)
class ImageRouteNotice:
    """One gray routing notice for a text-only image delivery decision.

    `reason` says why the raw image was not delivered to the main model; `images` names
    the observed inputs; `described_by` optionally names the [vision] entry. Presentation
    only; never enters model context.
    """

    reason: str
    described_by: str = ""
    images: tuple[str, ...] = ()


SELECTION_BACK = object()
SELECTION_FREE_TEXT = object()
DISMISSED = "(The user dismissed the question without answering.)"

# The line prefixes a rendered Note body can open with (memory.py writes them). A fact about the
# Note format rather than about drawing, so it lives here: the renderer uses it to pick the
# per-line memory colors.
MEMORY_PREFIXES = ("goal:", "check:", "plan:", "known:")


class WizoltError(Exception): ...


class ConfigError(WizoltError): ...


class ModelError(WizoltError): ...


class ModelResponseTimeout(ModelError): ...


class ModelOutputTruncated(ModelError): ...


class MalformedToolCallError(ModelError): ...


class ModelStreamIncomplete(ModelError): ...


class ModelRequestRetry(WizoltError): ...


class ToolError(WizoltError):
    """A tool failure serialized back to the model.

    `recovery` carries structured recovery output (e.g. a fresh source view from a stale Edit)
    that the runner registers and renders on the main thread like successful output. Ordinary
    errors carry None; typed as object to keep base.py free of the source-value import.
    """

    def __init__(self, message: str = "", *, recovery: object = None):
        super().__init__(message)
        self.recovery = recovery


def split_lines(text: str) -> list[str]:
    """Canonical line model shared by Read, Edit, and persisted diffs: split on "\n" only, keeping
    the newline (like file.readlines()). str.splitlines(True) also breaks on \r, \v, \f, \x1c-\x1e,
    \x85, \u2028, \u2029, which would number lines differently than Read and desync its line numbers."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def oneline(text: str, limit: int) -> str:
    """Collapse whitespace and truncate to `limit` characters with an ellipsis."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def drop_nulls(value: object) -> object:
    """Recursively drop null values. Strict schemas express optional params as nullable, so callers
    may send explicit null for an omitted argument; in wizolt null means "absent"."""
    if isinstance(value, dict):
        return {key: drop_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [drop_nulls(item) for item in value]
    return value


class Text:
    @staticmethod
    def clean(text: str) -> str:
        return text.encode("utf-8", errors="replace").decode("utf-8")

    @classmethod
    def value(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls.clean(value)
        if isinstance(value, dict):
            return {cls.clean(str(key)): cls.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls.value(item) for item in value]
        return value

    @staticmethod
    def elapsed_since(started_at: float, *, precise: bool = False) -> str:
        raw = max(0.0, time.monotonic() - started_at) if started_at else 0.0
        if raw < 60:
            return f"{raw:.1f}s" if precise else f"{int(raw)}s"
        minutes, seconds = divmod(int(raw), 60)
        return f"{minutes}m{seconds:02d}s"

    @staticmethod
    def age(seconds: float) -> str:
        """Wall-clock age in the coarsest unit that still says something. `elapsed_since` measures a
        running turn from a monotonic clock; this reads a stored timestamp, where minutes rarely matter."""
        for unit, size in (("d", 86400.0), ("h", 3600.0), ("m", 60.0)):
            if seconds >= size:
                return f"{int(seconds // size)}{unit} ago"
        return "just now"

    @staticmethod
    def abbreviate_count(value: int) -> str:
        """A count in the coarsest unit that still says something: 1.0M, 1.5K, or the bare number.
        Shared so `/status`, the status bar, and a worker's usage line all abbreviate alike."""
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(value)

    @staticmethod
    def clip_width(text: str, width: int) -> str:
        width = max(0, width)
        if _cwidth(text) <= width:
            return text
        ellipsis = "." * min(3, width)
        available = width - _cwidth(ellipsis)
        clipped = []
        used = 0
        for char in text:
            char_width = max(0, _cwidth(char))
            if used + char_width > available:
                break
            clipped.append(char)
            used += char_width
        return "".join(clipped).rstrip() + ellipsis

    @staticmethod
    def _styled_row(prefix: TextFragments, cells: list[_TextCell]) -> TextFragments:
        row = list(prefix)
        for style, group in groupby(cells, key=lambda cell: cell[0]):
            text = "".join(char for _, char, _ in group)
            if row and row[-1][0] == style:
                row[-1] = (style, row[-1][1] + text)
            else:
                row.append((style, text))
        return row

    @staticmethod
    def wrap_styled(
        prefix: TextFragments,
        continuation: TextFragments,
        content: TextFragments,
        width: int | None = None,
    ) -> TextRows:
        logical_lines: list[list[_TextCell]] = [[]]
        for style, text in content:
            for char in text:
                if char == "\n":
                    logical_lines.append([])
                else:
                    logical_lines[-1].append((style, char, _cwidth(char)))

        rows: TextRows = []
        row_prefix = prefix
        for logical in logical_lines:
            start = 0
            remaining_width = sum(cell_width for _, _, cell_width in logical)
            while True:
                prefix_width = sum(_cwidth(text) for _, text in row_prefix)
                available = max(1, width - prefix_width) if width else None
                if available is None or remaining_width <= available:
                    rows.append(Text._styled_row(row_prefix, logical[start:]))
                    break
                used = 0
                fit = start
                while fit < len(logical) and used + logical[fit][2] <= available:
                    used += logical[fit][2]
                    fit += 1
                fit = max(start + 1, fit)
                whitespace = max((index for index in range(start, fit) if logical[index][1].isspace()), default=-1)
                cut = whitespace if whitespace > start else fit
                rows.append(Text._styled_row(row_prefix, logical[start:cut]))
                end = cut + 1 if whitespace > start else cut
                # Advance through consumed cells only. Rescanning/copying the remaining tail
                # on every row makes a single minified line quadratic and blocks the TUI.
                remaining_width -= sum(cell_width for _, _, cell_width in logical[start:end])
                start = end
                row_prefix = continuation
            row_prefix = continuation
        return rows


@dataclass
class ModelUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_prompt_tokens: int = 0
    last_prompt_tokens: int = 0
    last_prompt_budget: int = 0
    last_cached_prompt_tokens: int = 0
    last_cache_write_prompt_tokens: int = 0

    def context_percent(self, fallback: int = 0) -> int:
        """How full the last request's prompt was, as a percentage of that request's budget.

        The provider-reported pair describes one real request, so a changed max_context_tokens only
        shows up after the next request rather than re-scoring the last one. `fallback` (the
        session's own estimate, state.context_percent) stands in before any request exists.
        """
        if not self.last_prompt_tokens or not self.last_prompt_budget:
            return fallback
        return min(100, self.last_prompt_tokens * 100 // self.last_prompt_budget)

    @staticmethod
    def field(usage: Any, *paths: str) -> int:
        """First present dotted path in `usage` (dict keys or attributes) as an int, else 0."""
        for path in paths:
            raw = usage
            for key in path.split("."):
                raw = raw.get(key) if isinstance(raw, dict) else getattr(raw, key, None)
                if raw is None:
                    break
            else:
                return int(raw or 0)
        return 0

    def add(self, usage: Any, budget: int | None = None, *, touch_last: bool = True) -> None:
        """Add one completed request to the totals, and unless `touch_last` is False make it the
        new last-request snapshot (the status bar's ctx/cache reading). Non-main-model requests
        (vision observations) stay in the totals but must not move that snapshot."""
        self.calls += 1
        prompt_tokens = self.field(usage, "prompt_tokens", "input_tokens")
        completion_tokens = self.field(usage, "completion_tokens", "output_tokens")
        # fmt: off
        cached_tokens = self.field(usage, "prompt_cache_hit_tokens", "cached_tokens", "cache_read_input_tokens", "prompt_tokens_details.cached_tokens", "input_tokens_details.cached_tokens")
        cache_write_tokens = self.field(
            usage,
            "cache_creation_input_tokens",
            "prompt_tokens_details.cache_write_tokens",
            "input_tokens_details.cache_write_tokens",
        )
        # fmt: on
        # OpenAI-shaped usage counts cache hits inside `prompt_tokens`, but Anthropic's
        # `input_tokens` is only what was neither read from nor written to the cache. Fold the cache
        # legs back in so the prompt total means the same thing for every provider; otherwise a
        # cached Anthropic request reports a hit ratio far above 100% and a tiny token total.
        if not self.field(usage, "prompt_tokens"):
            prompt_tokens += self.field(usage, "cache_read_input_tokens") + self.field(usage, "cache_creation_input_tokens")
        total_tokens = self.field(usage, "total_tokens") or prompt_tokens + completion_tokens
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += total_tokens
        self.cached_prompt_tokens += cached_tokens
        self.cache_write_prompt_tokens += cache_write_tokens
        if touch_last:
            self.last_prompt_tokens = prompt_tokens
            if budget is not None:
                self.last_prompt_budget = budget
            self.last_cached_prompt_tokens = cached_tokens
            self.last_cache_write_prompt_tokens = cache_write_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    args: ToolArgs
    # A malformed-argument error captured while parsing the call. Deferred so it surfaces as a
    # tool result the model can correct from, instead of aborting the whole turn at parse time.
    error: str = ""
    # The named arguments as the model sent them, before `args` reshaped them. Shell hooks receive
    # this, so a hook written against Claude Code reads `tool_input.command` as it expects.
    payload: Json | None = field(default=None, compare=False)

    def hook_input(self) -> Json:
        return self.payload if isinstance(self.payload, dict) else {"args": self.args}

    def paths(self) -> list[str]:
        """The file paths the call names: `path`, or each of Read's `files`."""
        payload = self.hook_input()
        entries = [payload, *(item for item in payload.get("files") or [] if isinstance(item, dict))]
        return [path for entry in entries if isinstance(path := entry.get("path"), str) and path]


class LogEdge(Enum):
    NONE = ""
    BRANCH = "├"
    CONTINUE = "│"
    END = "└"


class LogRole(Enum):
    TOOL = auto()
    AUTO = auto()
    META = auto()
    OUTPUT = auto()
    ERROR = auto()
    WARNING = auto()
    SUCCESS = auto()
    MUTED = auto()
    DIFF = auto()
    FIELD = auto()
    CODE = auto()


@dataclass(frozen=True)
class ApprovalView:
    """The full text behind a call, for the read-only viewer its confirmation offers.

    A tool that commits to something longer than one log line returns one of these from
    `approval_view()`, and the runner uses it twice: to render the clipped, syntax-highlighted
    excerpt inside the approval block, and to feed the viewer opened by the confirm-time `v` key.
    The same view is what the post-hoc Ctrl-O browser shows, which is the only way to read the
    text under yolo -- there is no confirmation prompt there to press `v` at.

    `label` names it in the viewer title and the action row ("order", "script"). `section`
    names the rule over the text itself -- "command" for a Bash call's command, "log" for a
    job's -- falling back to `label`'s first word when empty. `lexer` is a
    pygments lexer name; empty means the text is prose and renders as markdown. The one reserved
    value is `diff`, which is not read as a lexer: it selects the diff renderer, so a diff keeps
    the transcript's syntax highlighting and red/green bands. `rows` are the
    header fields shown above the text. `result` is what the call returned, shown below the text
    when the view is opened after the fact; it is empty at a confirmation prompt, where the call
    has not run yet.
    """

    label: str
    text: str
    lexer: str = ""
    rows: list[tuple[str, str]] = field(default_factory=list)
    result: str = ""
    section: str = ""


@dataclass(frozen=True)
class LogLine:
    label: str
    text: str = ""
    role: LogRole = LogRole.OUTPUT
    edge: LogEdge = LogEdge.NONE
    meta: str = ""
    syntax: str = ""

    def text_prefix(self) -> str:
        edge = "" if self.edge is LogEdge.NONE else self.edge.value + " "
        separator = "  " if self.edge is LogEdge.NONE else " "
        return edge + self.label + (separator if self.label and self.text else "")


@dataclass
class LogBlock:
    INDENT: ClassVar[str] = "  "
    # The indent unit of a nested region, drawn instead of blank spacing so the call that opened
    # the region stays connected to everything logged inside it. Exactly as wide as INDENT: a rail
    # replaces spacing, never adds a column, so turning one on cannot shift the tree.
    RAIL: ClassVar[str] = "│ "
    items: list[LogLine | LogBlock]
    # True on a wrapper whose whole subtree was logged by an enclosing tool call (a ToolScript's
    # nested calls). Every line below the region's own root lines then carries the rail; see
    # margin_units and ToolRunner.emit.
    gutter: bool = False

    @classmethod
    def hierarchy(cls, root: LogLine | None, children: list[LogLine]) -> LogBlock:
        items: list[LogLine | LogBlock] = [root] if root else []
        if children:
            items.append(cls(list(children)))
        return cls(items)

    @property
    def has_children(self) -> bool:
        return any(isinstance(item, LogBlock) for item in self.items)

    @classmethod
    def margin(cls, level: int) -> str:
        return cls.INDENT * level

    @classmethod
    def prefix(cls, level: int, edge: LogEdge = LogEdge.NONE) -> str:
        return cls.margin(level) + ((edge.value + " ") if edge is not LogEdge.NONE else "")

    @classmethod
    def margin_units(cls, level: int, rails: tuple[int, ...] = ()) -> list[tuple[bool, str]]:
        """A line's indent, unit by unit, each flagged as a rail or as plain spacing.

        A nested region's rail sits at the unit its own root lines draw an edge in, so the root's
        `│` and the rail below it land in one column and the region reads as a single bracket. The
        root lines themselves are shorter than that unit and are unaffected."""
        return [(index in rails, cls.RAIL if index in rails else cls.INDENT) for index in range(level)]

    def walk(self, parent_level: int = 0):
        for line, level, _ in self.walk_rows(parent_level):
            yield line, level

    def walk_rows(self, parent_level: int = 0, rails: tuple[int, ...] = ()):
        """Every line with its depth and the rail units its margin carries.

        A gutter block claims the unit one past its own items' level: that is where the lines it
        contains draw their edges, so deeper lines rail in the same column instead of floating free
        under them."""
        level = parent_level + 1
        if self.gutter:
            rails = (*rails, level + 1)
        for item in self.items:
            if isinstance(item, LogLine):
                yield item, level, rails
            else:
                yield from item.walk_rows(level, rails)

    def __str__(self) -> str:
        rows = []
        for line, level, rails in self.walk_rows():
            margin = "".join(text for _, text in self.margin_units(level, rails))
            prefix = margin + line.text_prefix()
            continuation = margin + " " * _cwidth(line.text_prefix())
            rows.extend(Text.wrap_styled([("", prefix)], [("", continuation)], [("", line.text + line.meta)]))
        return "\n".join("".join(text for _, text in row) for row in rows)


@dataclass
class TurnBox:
    CONTENT_LEVEL: ClassVar[int] = 1
    SEPARATOR: ClassVar[str] = ""
    messages: list[Json]

    @classmethod
    def group(cls, messages: list[Json]) -> list[TurnBox]:
        boxes: list[TurnBox] = []
        current: list[Json] = []
        for message in messages:
            current.append(message)
            if message.get("role") == "assistant" and not message.get("tool_calls"):
                boxes.append(cls(current))
                current = []
        if current:
            boxes.append(cls(current))
        return boxes
