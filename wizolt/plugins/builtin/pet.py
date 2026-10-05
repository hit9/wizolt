"""A small cat above your input that reacts to what the agent is doing.

A quiet desk companion, using only the same public SDK as user-authored plugins. Animation is a pure function of the host's monotonic clock. No timers, background jobs, random
state or model calls are needed; disabled plugins do no work and agent switches show the new
agent's state. Semantic roles deliberately leave every color choice to the active theme.
"""

from wizolt.sdk import Context, Line, Panel, Plugin, Text

SDK_VERSION = 1

# Every face has three cells, so animation never changes the component's width or alignment.
# The mood colors only the outline; the face keeps the theme's text contrast, the caption recedes.
MOODS = {
    "running": ("accent", "on it"),
    "waiting": ("warning", "your move"),
    "failed": ("error", "a snag"),
    "completed": ("success", "nailed it"),
    "interrupted": ("muted", "taking five"),
}
FACES = {"waiting": "●ω●", "failed": "×ω×", "completed": "^ω^", "interrupted": "−ω−"}
WORKING_FACES = ("•ω•",) * 7 + ("−ω−",)  # An occasional blink, not a flicker.
TAILS = ("~", "∿")
FRAMES_PER_SECOND = 2


def draw(context: Context) -> Panel:
    """Project a tiny sunglasses cat; its mood belongs to the visible agent, not the process."""
    frame = int(context.now * FRAMES_PER_SECOND)
    role, caption = MOODS.get(context.status, ("accent", "on standby"))
    running = context.status == "running"
    face = WORKING_FACES[frame % len(WORKING_FACES)] if running else FACES.get(context.status, "■ω■")
    tail = TAILS[frame % len(TAILS)] if running else "~"
    return Panel(
        (
            Text(" /\\_/\\", role),
            Line((Text("( ", role), Text(face), Text(" )" + tail, role), Text("  " + caption, "muted"))),
        )
    )


def setup(plugin: Plugin) -> None:
    """Default enablement belongs to the installation catalog, never to plugin source code."""
    plugin.configure(
        {"type": "object", "properties": {"slot": {"enum": ["above_divider", "above_input", "below_input"]}}, "additionalProperties": False},
        defaults={"slot": "above_input"},
    )
    plugin.component(plugin.config["slot"], draw)
