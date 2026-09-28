"""The project a working directory belongs to."""

from __future__ import annotations

import os


def project_root(cwd: str) -> str:
    """The top of the git work tree holding cwd, or cwd itself outside one.

    The walk stops at a repository on purpose: outside one there is no project boundary, and
    climbing to `/` would treat whatever a home directory happens to hold as the project's own."""
    start = current = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(current, ".git")):  # a directory, or a worktree's file
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return start
        current = parent
