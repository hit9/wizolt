"""Statusbar and divider selection: reversible previews, cascade navigation and config saves."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import shutil
import time
from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import StyleAndTextTuples

from wizolt.base import ConfigError
from wizolt.config import ConfigFile
from wizolt.ui.bars import PRESETS
from wizolt.ui.cli.modals import choice_application
from wizolt.ui.render import Theme

# Keep the picker focused; older preset names still work in config files.
DIVIDER_CHOICES = ("comet", "capsule", "frame", "rail", "powerline")
SWEEP_CHOICES = ("none", "comet", "ripple", "aurora")

if TYPE_CHECKING:
    from wizolt.ui.cli.loop import CommandLoop


def select_layout(loop: CommandLoop, kind: str, source: str) -> str:
    layout = loop.presentation.status_bar.layout
    problems = layout.configure({kind: source}, Theme.bar_styles)
    if problems:
        return "\n".join(problems)
    section, key = ("divider", "sweep") if kind == "sweep" else (kind, "format")
    previous = loop.session.config.ui.get(section, {})
    raw = dict(previous) if isinstance(previous, dict) else {}
    raw[key] = source
    loop.session.config.ui[section] = raw
    if loop.presentation.tui is not None:
        loop.presentation.tui.invalidate()
    result = f"{section}.{key}: {source}"
    if loop.session.config.path:
        try:
            ConfigFile.set_ui_value(loop.session.config.path, ("ui", section), key, source)
        except (OSError, ValueError, ConfigError) as error:
            result += f"\nApplied for this session; not saved: {error}"
        else:
            result += " (saved)"
    return result


def preview(loop: CommandLoop, kind: str, started: float) -> StyleAndTextTuples:
    bar = loop.presentation.status_bar
    width = max(1, shutil.get_terminal_size((80, 20)).columns - 8)
    values = bar.values()
    result: StyleAndTextTuples = []
    # The real statusbar already previews the selection at its actual terminal width.
    if kind != "statusbar":
        elapsed = max(0.0, time.monotonic() - started)
        ramp = tuple(reversed(Theme.ramp("divider_glow", "divider_rule", 16)))
        for label, running, queued in (("Idle", False, 0), ("Running", True, 0), ("Queued", True, 2)):
            values.update(
                running=running,
                elapsed=elapsed if running else 0,
                activity="working" if running else "",
                rate="42 tok/s" if running else "",
                spinner="● " if running else "",
                label=(f"working ({int(elapsed)}s · 42 tok/s)" + (" [ 2 queued ]" if queued else "")) if running else "",
                **{"queue.total": queued, "queue.followup": queued, "queue.next_turn": 0},
            )
            result.extend([(Theme.fg("muted"), label + " (preview)\n")])
            result.extend(bar.layout.render("divider", values, width, Theme.bar_styles, ramp=ramp))
            result.append(("", "\n"))
    for problem in (*bar.layout.errors, *((bar.layout.sweep.error,) if bar.layout.sweep.error else ())):
        result.append((Theme.fg("error"), "\n" + problem))
    return result


async def pick_layout(loop: CommandLoop, kind: str) -> str | None:
    tui = loop.presentation.tui
    assert tui is not None
    bar = loop.presentation.status_bar
    original_layout = bar.layout
    # A cancelled preview restores the object, not a re-parse under a possibly different theme.
    # Compiled templates are immutable; the source map and sweep error belong to the preview.
    layout = copy.copy(original_layout)
    layout.sources = dict(original_layout.sources)
    layout.sweep = copy.copy(original_layout.sweep)
    layout.errors = []
    original = layout.sources[kind]
    bar.layout = layout
    presets = DIVIDER_CHOICES if kind == "divider" else SWEEP_CHOICES if kind == "sweep" else tuple(PRESETS[kind])
    current = original[7:] if original.startswith("preset:") and original[7:] in presets else "custom"
    choices = presets + (("custom",) if current == "custom" else ())
    current_label = f"current ({original[7:]})" if original.startswith("preset:") else "custom (current)"
    started = time.monotonic()

    def apply(name: str) -> None:
        layout.configure({kind: original if name == "custom" else "preset:" + name}, Theme.bar_styles)
        tui.invalidate()

    async def animate() -> None:
        while True:
            await asyncio.sleep(0.05)
            tui.invalidate()

    timer = asyncio.create_task(animate()) if kind != "statusbar" else None
    try:
        chosen = await choice_application(
            loop,
            "Statusbar" if kind == "statusbar" else "Divider › " + ("Sweep" if kind == "sweep" else "Layout"),
            choices,
            {"custom": current_label},
            current,
            set(),
            preview_fn=lambda _name: preview(loop, kind, started),
            on_focus=apply,
        )
    finally:
        bar.layout = original_layout
        tui.invalidate()
        if timer is not None:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
    if not isinstance(chosen, str):
        return None
    return select_layout(loop, kind, original if chosen == "custom" else "preset:" + chosen)


async def statusbar_command(loop: CommandLoop, args: str) -> str | None:
    args = args.strip()
    if args:
        return select_layout(loop, "statusbar", "preset:" + args)
    if loop.presentation.tui is None or not loop.interactive_input:
        return "Statusbar presets: " + ", ".join(PRESETS["statusbar"]) + ". Use /statusbar NAME."
    return await pick_layout(loop, "statusbar")


async def divider_command(loop: CommandLoop, args: str) -> str | None:
    args = args.strip()
    if args:
        return select_layout(loop, "divider", "preset:" + args)
    if loop.presentation.tui is None or not loop.interactive_input:
        return (
            "Divider layouts: "
            + ", ".join(DIVIDER_CHOICES)
            + ". Sweeps: "
            + ", ".join(SWEEP_CHOICES)
            + ". Configure ui.divider.sweep or open /divider interactively."
        )
    layout_result = await pick_layout(loop, "divider")
    if layout_result is None:
        return None
    sweep_result = await pick_layout(loop, "sweep")
    return layout_result + ("\n" + sweep_result if sweep_result else "")
