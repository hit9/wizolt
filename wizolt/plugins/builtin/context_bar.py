"""A bar above your input showing what fills the context window, by category, with a legend.

```text
████████████████▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 37%
system prompt 9.0k · system tools 14k · …               74k/200k
```

(In the terminal each category has its own theme color.)

Each colored segment is one part of the next request, in its order: tool definitions, system
prompt, memory files (AGENTS.md), skills, MCP servers, the summary `/compact` left, and the
conversation. The percentage is the status bar's `ctx`; it warns from 80%.

## Use

- Enable **context_bar** in `/plugins`. It makes no model calls.
- Move it: set `[plugins.context_bar] slot` to `above_divider`, `above_input` (default) or
  `below_input`, then reload it in `/plugins`.
- Compact (`/compact`) or start a new session when the bar is nearly full.
"""

# Uses only the public SDK. `window.parts` are the host's local per-category estimates in request
# order; `window.used` over `window.budget` is the reported fill the status bar's ctx divides, so
# the bar spans the budget and its percentage is that same number. Estimates that fall short of
# the reported fill leave the difference as `other`; estimates that exceed it are drawn
# proportionally inside it. The percentage rides on the bar so it survives a one-row allocation.
# Colors are semantic theme roles, so the active theme owns every choice. Drawing is pure: no
# model calls, timers or state between repaints.

import zlib

from wizolt.sdk import Context, Line, Panel, Plugin, Text

SDK_VERSION = 1

# The host's categories, in request order. Searched over every bundled theme (CIELAB distance):
# no two categories share a color in any of them, neighbouring segments (other included) stay at
# least 35 apart, and the alarm roles (warning, error) are left to the percentage. A narrow
# palette may still bring two segments that never touch close together (forest's greens).
ROLES = {
    "system tools": "syntax_number",
    "system prompt": "status_context",
    "memory files": "status_services",
    "skills": "tool",
    "mcp servers": "status_agent",
    "summary": "syntax_assign",
    "messages": "syntax_builtin",
    "other": "muted",
}
# A category the host adds later still gets a stable color: same name, same role.
FALLBACK_ROLES = ("accent", "accent_secondary", "status_agent", "status_cache", "divider_glow")
MIN_BAR = 12  # Below this the percentage is dropped rather than squeezing the bar further.


def role(name: str) -> str:
    return ROLES.get(name) or FALLBACK_ROLES[zlib.crc32(name.encode()) % len(FALLBACK_ROLES)]


def compact(tokens: float) -> str:
    """900, 12k, 1.8M: short enough that the legend does not jump between repaints."""
    for scale, suffix in ((1_000_000, "M"), (1000, "k")):
        if tokens >= scale:
            size = tokens / scale
            return f"{size:.1f}{suffix}" if size < 10 else f"{size:.0f}{suffix}"
    return str(int(tokens))


def allocate(weights: list[int], width: int) -> list[int]:
    """Split ``width`` cells by largest remainder; a nonempty weight keeps at least one cell
    while another can spare it, so a category that just appeared never vanishes from the bar."""
    total = sum(weights)
    if width <= 0 or total <= 0:
        return [0] * len(weights)
    exact = [weight * width / total for weight in weights]
    cells = [int(value) for value in exact]
    for index, weight in enumerate(weights):
        donor = max(range(len(cells)), key=cells.__getitem__)
        if weight and not cells[index] and cells[donor] > 1:
            cells[donor], cells[index] = cells[donor] - 1, 1
    for index in sorted(range(len(cells)), key=lambda index: exact[index] - cells[index], reverse=True)[: width - sum(cells)]:
        cells[index] += 1
    return cells


def legend(parts: list[tuple[str, int]], totals: str, columns: int) -> Line:
    """Name the drawn categories in bar order, cut with an ellipsis, totals right-aligned."""
    room = columns - len(totals) - 1
    if room < 16:  # Too little legend to be worth it: keep only the totals.
        # The narrowest terminals cannot even fit the totals: the percentage alone is the one
        # number that still fits, so it is what stays.
        shown = totals if len(totals) <= columns else totals.rsplit(" ", 1)[-1]
        return Line((Text(shown[:columns].rjust(columns), "muted"),))
    spans: list[Text] = []
    width = 0
    for index, (name, tokens) in enumerate(parts):
        entry, gap = f"{name} {compact(tokens)}", 3 if spans else 0
        # Leave room for " · …" unless this is the last entry.
        if width + gap + len(entry) + (0 if index == len(parts) - 1 else 4) > room:
            spans += [Text(" · " if spans else "", "muted"), Text("…", "muted")]
            width += gap + 1
            break
        spans += [Text(" · ", "muted")] if spans else []
        spans.append(Text(entry, role(name)))
        width += gap + len(entry)
    spans.append(Text(" " * max(1, columns - width - len(totals)) + totals, "muted"))
    return Line(tuple(spans))


def draw(context: Context) -> Panel:
    """One agent's window; an agent with no window yet takes no rows at all."""
    window = context.window
    parts = [(name, tokens) for name, tokens in window.parts if tokens > 0]
    estimate = sum(tokens for _, tokens in parts)
    total = next((value for value in (window.budget, window.limit, estimate) if value > 0), 0)
    if not total:
        return Panel(())
    filled = min(total, window.used or estimate)
    if window.used > estimate:
        parts.append(("other", window.used - estimate))
    columns = context.layout.columns if context.layout else context.columns
    # Rounded down and capped, exactly as the status bar's ctx is.
    shown = min(100, filled * 100 // total)
    percent = f"{shown}%"
    width = columns - len(percent) - 1
    if width < MIN_BAR:
        percent, width = "", columns
    # The filled span is the reported fill; the parts share it in proportion, the rest is free.
    filled_cells = min(width, max(1, round(width * filled / total))) if parts else 0
    cells = [*allocate([tokens for _, tokens in parts], filled_cells), width - filled_cells]
    spans = [Text("█" * count, role(name)) for (name, _), count in zip(parts, cells, strict=False) if count]
    spans += [Text("░" * cells[-1], "rule")] if cells[-1] else []
    spans += [Text(" " + percent, "warning" if shown >= 80 else "text")] if percent else []
    rows: list[Text | Line] = [Line(tuple(spans))]
    if parts and (context.layout is None or context.layout.rows >= 2):
        drawn = [part for part, count in zip(parts, cells, strict=False) if count]
        rows.append(legend(drawn, f"{compact(filled)}/{compact(total)}" + ("" if percent else f" {shown}%"), columns))
    return Panel(tuple(rows))


def setup(plugin: Plugin) -> None:
    """Default enablement belongs to the installation catalog, never to plugin source code."""
    plugin.configure(
        {"type": "object", "properties": {"slot": {"enum": ["above_divider", "above_input", "below_input"]}}, "additionalProperties": False},
        defaults={"slot": "above_input"},
    )
    plugin.component(plugin.config["slot"], draw)
