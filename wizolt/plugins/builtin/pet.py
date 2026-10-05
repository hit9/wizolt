"""A rainbow pet that strolls along your prompt line while the agent works. Pick cat, dog, rabbit, frog, dragon or robot with /pet.

The pose is a pure function of the host's monotonic clock, so there are no timers, background
tasks or randomness. Only the walked distance is state: it credits monotonic time while the
agent's status is `running`, which parks the pet exactly where it stopped instead of teleporting
it to wherever a status-dependent `now * speed` would land. Hues come from the active theme's
semantic roles, so the rainbow follows the user's theme instead of hard-coded ANSI colors.

Which pet walks is `[plugins.pet] pet`; `/pet` opens the host's own picker, previews what each
one looks like and saves the pick. A pet is data, not a code path: one `PETS` entry shares the
moods, the walk and the rainbow with every other pet. Uses only the public SDK; no model calls.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from wizolt.sdk import Context, Event, Line, Panel, Plugin, PluginError, Text
from wizolt.sdk.views import Choice

SDK_VERSION = 1

# Seven bands that read as a rainbow in every bundled theme, dark or light.
RAINBOW = ("error", "warning", "success", "accent", "info", "status_model", "status_yolo")
# The stride is credited per second of `running` time; every other status only changes the pose.
WALK_SPEED = 7.0  # cells per second of working time
TURN_AT = 3  # the pet turns around once its body reaches the first third of the row
MAX_SAMPLE_STEP = 1.0  # seconds one refresh may credit, so a stalled host cannot lurch the pet
# Cells every pet keeps behind its face: one puff, one tail, one gap before the body.
TRAIL = 3
# status -> (eye frames, tail frames, caption, caption role, animation frames per second)
MOODS: dict[str, tuple[tuple[str, ...], tuple[str, ...], str, str, float]] = {
    "running": (("o.o", "o.o", ">.<", "o.o"), ("∿", "~", "≈", "~"), "on it", "accent", 4.0),
    "waiting": (("O.O", "O.o", "o.O"), ("∿",), "your move", "warning", 2.0),
    "completed": (("^.^", "^.^", "^-^"), ("∿", "~"), "nailed it", "success", 3.0),
    "failed": (("x.x", "x_x"), ("~",), "a snag", "error", 1.0),
    "interrupted": (("-.-", "u.u"), ("~",), "taking five", "muted", 1.0),
    "idle": (("o.o", "o.o", "-.-", "o.o"), ("~", "∿"), "strolling", "muted", 1.5),
}
RESTING: tuple[tuple[str, ...], tuple[str, ...], str, str, float] = (("o.o", "-.-"), ("~",), "hello", "muted", 1.0)
PUFFS = (" ", "·", " ", "˙")
EYE_WIDTH = len(RESTING[0][0])  # every mood draws its eyes this wide, so crowns stay aligned


@dataclass(frozen=True)
class Pet:
    """A two-row silhouette: `crown` sits above the eyes and `wrap` frames them.

    The crown spans exactly the wrapped eyes, so a new pet is one `PETS` entry, never a branch.
    """

    crown: str
    wrap: tuple[str, str]

    @property
    def head(self) -> int:
        """Cells from the first wrap glyph through the last: what the crown has to span."""
        return len(self.wrap[0]) + EYE_WIDTH + len(self.wrap[1])

    @property
    def width(self) -> int:
        """Cells a whole body occupies, trail included, so neither row ever jitters."""
        return self.head + TRAIL

    def face(self, eyes: str) -> str:
        """The eyes inside their wrap, `head` cells wide."""
        return f"{self.wrap[0]}{eyes}{self.wrap[1]}"


# One crown per pet, every crown as wide as the face below it.
PETS: dict[str, Pet] = {
    "cat": Pet("/\\_/\\", ("(", ")")),
    "dog": Pet("u   u", ("(", ")")),
    "rabbit": Pet(" /\\/\\", ("(", ")")),
    "frog": Pet("O   O", ("(", ")")),
    "dragon": Pet("^   ^", ("(", ")")),
    "robot": Pet("|___|", ("[", "]")),
}
DEFAULT_PET = "cat"


@dataclass
class Chosen:
    """The pet on screen, by name; `/pet` replaces it in place, so no reload is needed."""

    name: str = DEFAULT_PET

    @property
    def pet(self) -> Pet:
        return PETS[self.name]


class Stroll:
    """Distance walked so far: working time only, so the pet rests whenever the agent does."""

    def __init__(self, speed: float) -> None:
        self.speed = speed
        self.walked = 0.0
        self.sampled: float | None = None

    def advance(self, now: float, working: bool) -> None:
        """Credit the time since the previous sample, but only to a working agent.

        Crediting elapsed time rather than repaints keeps the stride the same at any refresh
        rate, and holding `walked` while the agent is idle means the pet resumes from where it
        stopped instead of jumping.
        """
        if working and self.sampled is not None:
            self.walked += min(max(now - self.sampled, 0.0), MAX_SAMPLE_STEP) * self.speed
        self.sampled = now


def _placement(context: Context, walked: float, sprite: int) -> tuple[int, int, int]:
    """Sprite offset, facing and rainbow band: a triangle wave over the walked distance."""
    span = max(0, context.columns // TURN_AT - sprite)
    if span == 0:
        return 0, 1, int(walked)
    phase = walked % (2 * span)
    if phase <= span:
        return int(phase), 1, int(walked)
    return int(2 * span - phase), -1, int(walked)


def _caption_left(columns: int, left: int, facing: int, sprite: int, caption: str) -> int:
    """Keep the caption one cell off the tail, crossing to the front only to stay on screen."""
    width = len(caption)
    behind = left - width - 1 if facing > 0 else left + sprite + 1
    if 0 <= behind and behind + width <= columns:
        return behind
    front = left + sprite + 1 if facing > 0 else left - width - 1
    if 0 <= front and front + width <= columns:
        return front
    return min(max(behind, 0), max(0, columns - width))


def _spans(cells: Sequence[str], roles: Sequence[str]) -> tuple[Text, ...]:
    """One span per run of equal roles, which keeps the per-cell rainbow cheap to render."""
    runs: list[tuple[str, list[str]]] = []
    for char, role in zip(cells, roles):
        if runs and runs[-1][0] == role:
            runs[-1][1].append(char)
        else:
            runs.append((role, [char]))
    return tuple(Text("".join(chars), role) for role, chars in runs)


def _row(placements: Sequence[tuple[int, str, str | None]], band: int) -> Line:
    """Stamp text runs onto one row; a `None` role paints that cell with the rainbow band."""
    width = max((offset + len(text) for offset, text, _ in placements), default=0)
    cells, roles = [" "] * width, ["text"] * width
    for offset, text, role in placements:
        for index, char in enumerate(text):
            cells[offset + index] = char
            roles[offset + index] = role or RAINBOW[(band + offset + index) % len(RAINBOW)]
    return Line(_spans(cells, roles))


def _pose(pet: Pet, eyes: str, tail: str, puff: str, facing: int) -> tuple[str, str]:
    """Crown row and body row for one facing: tail and puff trail on the far side of the face."""
    if facing > 0:
        return " " * TRAIL + pet.crown, f"{puff}{tail} {pet.face(eyes)}"
    return pet.crown + " " * TRAIL, f"{pet.face(eyes)} {tail}{puff}"


def _preview(pet: Pet) -> str:
    """The sprite the picker's detail pane shows: the resting pose, facing the walk's direction."""
    return "\n".join(_pose(pet, RESTING[0][0], RESTING[1][0], " ", 1))


def _draw(context: Context, pet: Pet, walked: float, captioned: bool) -> Panel:
    """Project the pet; its mood belongs to the visible agent, not to the worker process."""
    eyes, tails, caption, caption_role, rate = MOODS.get(context.status, RESTING)
    frame = int(context.now * rate)
    eye, tail = eyes[frame % len(eyes)], tails[frame % len(tails)]
    puff = PUFFS[frame % len(PUFFS)] if context.status == "running" else " "
    left, facing, band = _placement(context, walked, pet.width)
    top, body = _pose(pet, eye, tail, puff, facing)
    placements: list[tuple[int, str, str | None]] = [(left, top, None)]
    if captioned:
        placements.append((_caption_left(context.columns, left, facing, pet.width, caption), caption, caption_role))
    return Panel((_row(placements, band), _row(((left, body, None),), band)))


def _check_art() -> None:
    """Fail on load: a crown of the wrong span, or eyes that changed width, misaligns the head."""
    if any(len(eyes) != EYE_WIDTH for mood in (*MOODS.values(), RESTING) for eyes in mood[0]):
        raise PluginError(f"Every mood must draw its eyes in {EYE_WIDTH} cells")
    for name, pet in PETS.items():
        if len(pet.crown) != pet.head:
            raise PluginError(f"Pet {name!r}: crown spans {len(pet.crown)} cells, expected {pet.head}")


def setup(plugin: Plugin) -> None:
    """Default enablement belongs to the installation catalog, never to plugin source code."""
    _check_art()
    plugin.configure(
        {
            "type": "object",
            "properties": {
                "slot": {"enum": ["above_divider", "above_input", "below_input"]},
                "pet": {"enum": list(PETS)},
                "caption": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        defaults={"slot": "above_input", "pet": DEFAULT_PET, "caption": True},
    )
    stroll = Stroll(WALK_SPEED)
    chosen = Chosen(plugin.config["pet"])

    async def observe(event: Event) -> None:
        stroll.advance(event.context.now, event.context.status == "running")

    async def pick(context: Context, arguments: Mapping[str, object]) -> str:
        """Supply the pets; the host owns the window, keys, theme and cancellation.

        The pick is saved as this plugin's own override, which a later generation reads back. It
        applies here too, because `plugin.config` stays the snapshot this generation loaded.
        """
        picked = await plugin.ui.select(
            "Which pet strolls the prompt line?",
            items=tuple(Choice(name, name, _preview(pet)) for name, pet in PETS.items()),
            default=chosen.name,
        )
        if picked is None:
            return "Pet unchanged"
        chosen.name = picked
        try:
            await plugin.settings.update({"pet": picked})
        except PluginError as error:
            return f"Pet: {picked} for this session; saving it failed ({error})"
        return f"Pet: {picked}, saved under [plugins.pet]"

    plugin.on("tick", observe)
    plugin.command("pet", "Choose which pet strolls the prompt line", pick)
    plugin.component(plugin.config["slot"], lambda context: _draw(context, chosen.pet, stroll.walked, plugin.config["caption"]))
