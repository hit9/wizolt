"""Statusbar and divider previews and config saves for the appearance picker."""

from __future__ import annotations

import shutil
import time
from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import StyleAndTextTuples

from wizolt.base import ConfigError
from wizolt.config import ConfigFile
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
        for index, (label, running, queued) in enumerate((("Idle", False, 0), ("Running", True, 0), ("Queued", True, 2))):
            if index:
                result.append(("", "\n"))  # a blank line between examples, so each reads on its own
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
