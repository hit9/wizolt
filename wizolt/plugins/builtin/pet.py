"""A quiet desk companion, using only the same public SDK as user-authored plugins.

Animation is a pure function of the host's monotonic clock. No timers, background jobs, random
state or model calls are needed; hidden components do no work and agent switches show the new
agent's state. Semantic roles deliberately leave every color choice to the active theme.
"""

from wizolt.sdk import Context, Panel, Plugin, Text

SDK_VERSION = 1

# A shared silhouette keeps animation from changing the component's height or alignment.
WORKING_FACES = ("• •", "› ‹", "• •", "− −")
TAILS = ("~", "∿", "~", "∾")
FRAMES_PER_SECOND = 2


def draw(context: Context) -> Panel:
    """Project a tiny sunglasses cat; its mood belongs to the visible agent, not the process."""
    frame = int(context.now * FRAMES_PER_SECOND)
    face, role, caption = "■ ■", "accent", "on standby"
    if context.status == "running":
        face, role, caption = WORKING_FACES[frame % len(WORKING_FACES)], "accent", "on it"
    elif context.status == "waiting":
        face, role, caption = "● ●", "warning", "your move"
    elif context.status == "failed":
        face, role, caption = "! !", "error", "a snag"
    elif context.status == "completed":
        role, caption = "success", "nailed it"
    elif context.status == "interrupted":
        face, role, caption = "− −", "muted", "taking five"
    tail = TAILS[frame % len(TAILS)] if context.status == "running" else "~"
    return Panel((Text(" /\\_/\\", role), Text(f"( {face} ){tail}  {caption}", role)))


def setup(plugin: Plugin) -> None:
    """Default enablement belongs to the installation catalog, never to plugin source code."""
    plugin.component("above_input", draw)
