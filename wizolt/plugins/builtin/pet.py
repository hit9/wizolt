"""A pocket-sized pixel pet beside your prompt. Choose a cat, bear, owl, bunny, fish or robot with /pet.

The two-row pet sits quietly while you type. As the agent works its tail, ears, fins or antenna
move; when it needs you, the eyes open wide. A short caption appears for attention and errors.

## Use

- Enable **pet** in `/plugins`. Type `/pet` to preview the six companions and save a choice.
- Set `[plugins.pet] motion = "walk"` for a slow stroll while working, or `caption = false`
  to hide status text. Reload pet after editing the config.
- The `slot` setting accepts `above_divider`, `above_input` (default) or `below_input`.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from wizolt.sdk import Context, Event, Line, Panel, Plugin, PluginError, Text
from wizolt.sdk.views import Choice

SDK_VERSION = 1


@dataclass(frozen=True)
class Pet:
    """Two poses in the same cell grid. Asterisks mark the eyes."""

    coat: str
    crowns: tuple[str, str]
    body: str

    @property
    def width(self) -> int:
        return max(len(self.body), *(len(row) for row in self.crowns))


PETS = {
    "cat": Pet("accent", ("▟▙▄▟▙ ▗", "▟▙▄▟▙ ▖"), "▜*█*██▛"),
    "bear": Pet("syntax_builtin", ("▗▛███▜▖", "▗▛███▀▖"), " ▜*█*▛ "),
    "owl": Pet("accent_secondary", ("▙*█*▟", "▙*▟*▟"), "▝▜█▛▘"),
    "bunny": Pet("status_services", (" ▟▌▟▌ ", " ▟▌▜▖ "), "▗*█*█▙"),
    "fish": Pet("syntax_string", (" ▄▟█▄ ▟", " ▄▜█▄ ▗"), "▝*███▀▜"),
    "robot": Pet("info", (" ▄▟▌▄ ", " ▄▜▌▄ "), "▐*██*▌"),
}
DEFAULT_PET = "cat"
# Only actionable states put text beside the sprite. Other moods read through its eyes.
EYES = {"waiting": "▂", "completed": "▀", "failed": "▖", "interrupted": "▃"}
CAPTIONS = {"waiting": ("your move", "warning"), "failed": ("a snag", "error")}


def _preview(pet: Pet) -> str:
    return (pet.crowns[0] + "\n" + pet.body).replace("*", "▄")


def _draw(context: Context, pet: Pet, walked: float, captioned: bool) -> Panel:
    columns = context.layout.columns if context.layout else context.columns
    height = context.layout.rows if context.layout else 2
    if columns <= 0 or height <= 0:
        return Panel(())
    # Keep the face legible when the host grants just one row or a few columns.
    eye = EYES.get(context.status, "▄")
    if context.status in ("", "idle") and int(context.now * 4) % 20 == 19:
        eye = "▆"
    if columns < pet.width or height == 1:
        return Panel((Text(("▐" + eye + "▌")[:columns], pet.coat),))

    span = max(0, columns // 3 - pet.width)
    offset = span - abs(int(walked) % (2 * span) - span) if span else 0
    step = int(context.now * 1.5) % 2 if context.status == "running" else 0

    def row(art: str) -> Text:
        return Text(" " * offset + art.replace("*", eye).ljust(pet.width), pet.coat)

    top = [row(pet.crowns[step])]
    if captioned and context.status in CAPTIONS:
        caption, role = CAPTIONS[context.status]
        if offset + pet.width + 2 + len(caption) <= columns:
            top.append(Text("  " + caption, role))
    return Panel((Line(tuple(top)), row(pet.body)))


def setup(plugin: Plugin) -> None:
    plugin.configure(
        {
            "type": "object",
            "properties": {
                "slot": {"enum": ["above_divider", "above_input", "below_input"]},
                "pet": {"enum": list(PETS)},
                "motion": {"enum": ["perch", "walk"]},
                "caption": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        defaults={"slot": "above_input", "pet": DEFAULT_PET, "motion": "perch", "caption": True},
    )
    chosen = plugin.config["pet"]
    walked = 0.0
    if plugin.config["motion"] == "walk":
        sampled: float | None = None

        async def observe(event: Event) -> None:
            nonlocal walked, sampled
            now = event.context.now if event.context.status == "running" else None
            if now is not None and sampled is not None:
                walked += min(1.0, max(0.0, now - sampled)) * 3
            sampled = now

        plugin.on("tick", observe)

    async def pick(context: Context, arguments: Mapping[str, object]) -> str:
        nonlocal chosen
        picked = await plugin.ui.select(
            "Choose a companion",
            items=tuple(Choice(name, name, _preview(pet)) for name, pet in PETS.items()),
            default=chosen,
        )
        if picked is None:
            return "Pet unchanged"
        chosen = picked
        try:
            await plugin.settings.update({"pet": picked})
        except PluginError as error:
            return f"Pet: {picked} for this session; saving it failed ({error})"
        return f"Pet: {picked}, saved under [plugins.pet]"

    plugin.command("pet", "Choose a pixel companion", pick)
    plugin.component(plugin.config["slot"], lambda context: _draw(context, PETS[chosen], walked, plugin.config["caption"]))
