"""Copying text to the system clipboard through the platform's own command."""

from __future__ import annotations

import shutil
import subprocess
from typing import ClassVar


class Clipboard:
    """The first copy command present on PATH, run once per copy.

    A command's exit status says whether the copy happened. An OSC 52 escape cannot: a terminal
    that refuses it says nothing, and a caller would report a copy that never took place."""

    COMMANDS: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("pbcopy",),
        ("wl-copy",),
        ("xclip", "-selection", "clipboard"),
        ("xsel", "--clipboard", "--input"),
        ("clip.exe",),
    )
    TIMEOUT: ClassVar[float] = 2.0

    @classmethod
    def copy(cls, text: str) -> str | None:
        """None once `text` is on the clipboard, else why it is not."""
        for command in cls.COMMANDS:
            if shutil.which(command[0]) is None:
                continue
            try:
                # Copy commands may leave a process holding the selection; detached output keeps
                # that process from holding this call open.
                subprocess.run(command, input=text.encode(), check=True, timeout=cls.TIMEOUT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as error:
                return f"{command[0]} failed: {error}"
            return None
        return "no clipboard command found (" + ", ".join(command[0] for command in cls.COMMANDS) + ")"
