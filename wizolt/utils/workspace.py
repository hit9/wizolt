"""Where a working directory sits inside its project."""

from __future__ import annotations

import os


class Workspace:
    """A working directory and the project it belongs to: the top of the enclosing git work tree,
    or the directory itself outside one.

    The search stops at a repository on purpose: outside one there is no project boundary, and
    climbing to `/` would treat whatever a home directory happens to hold as the project's own."""

    def __init__(self, cwd: str):
        self.cwd = os.path.abspath(cwd)
        self.root = self._find_root(self.cwd)

    @staticmethod
    def _find_root(start: str) -> str:
        current = start
        while True:
            if os.path.exists(os.path.join(current, ".git")):  # a directory, or a worktree's file
                return current
            parent = os.path.dirname(current)
            if parent == current:
                return start
            current = parent

    def path_levels(self) -> list[str]:
        """The directories from the root down to the working directory, root first."""
        levels = [self.root]
        if self.cwd != self.root:
            for part in os.path.relpath(self.cwd, self.root).split(os.sep):
                levels.append(os.path.join(levels[-1], part))
        return levels

    def levels_above(self, path: str) -> list[str]:
        """The directories from `path`'s own folder up to, not including, the root; none when the
        file lies outside the project."""
        directory = os.path.dirname(os.path.abspath(path))
        if os.path.commonpath([directory, self.root]) != self.root:
            return []
        levels = []
        while directory != self.root:
            levels.append(directory)
            directory = os.path.dirname(directory)
        return levels

    def relative(self, path: str) -> str:
        """`path` as the project names it, relative to the root."""
        return os.path.normpath(os.path.relpath(path, self.root))

    def from_cwd(self, path: str) -> str:
        """`path` as a Read from the working directory would name it: `./AGENTS.md`, `../AGENTS.md`."""
        relative = os.path.relpath(path, self.cwd)
        return relative if relative.startswith("..") else "./" + relative

    def files_on_path(self, names: tuple[str, ...]) -> list[str]:
        """At each level from the root down to the working directory, the first of `names` that is
        a file there; root first, so the nearest one comes last."""
        found = []
        for level in self.path_levels():
            path = next((candidate for name in names if os.path.isfile(candidate := os.path.join(level, name))), None)
            if path is not None:
                found.append(path)
        return found
