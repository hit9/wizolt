"""Bounded reads for plugin source admission, before any worker process exists."""

import os
import stat
from pathlib import Path

from wizolt.sdk import PluginError


def read_regular(path: str | Path, limit: int, *, directory: int | None = None) -> bytes:
    """Inspect the opened file, not a racy path stat; FIFOs and devices must never block load."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PluginError("Plugin source must be a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PluginError(f"Plugin source exceeds {limit / 1024:g} KiB")
    return data
