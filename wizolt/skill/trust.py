"""Which repositories the user lets run commands from their own skills.

A cloned repository controls its `.claude/skills` and `.wizolt/skills`. Reading instructions from
them is like reading its AGENTS.md; a skill that runs commands when it loads (and, later, hooks
and pre-approved tools) acts before anyone reviews it. Such project skills wait for this decision.
Stored per user, never in the repository: a repository cannot trust itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile


class ProjectTrust:
    """The trust decision for one project root, persisted in the user's data directory."""

    FILE_NAME = "trusted-projects.json"

    def __init__(self, store: str, root: str):
        self.store = store
        self.root = os.path.realpath(root)

    def granted(self) -> bool:
        return self.root in self._roots()

    def grant(self) -> None:
        self._write(self._roots() | {self.root})

    def revoke(self) -> None:
        self._write(self._roots() - {self.root})

    def _roots(self) -> set[str]:
        try:
            with open(self.store, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return set()  # a missing or damaged file trusts nothing, which is the safe reading
        roots = data.get("projects") if isinstance(data, dict) else None
        return {root for root in roots if isinstance(root, str)} if isinstance(roots, list) else set()

    def _write(self, roots: set[str]) -> None:
        directory = os.path.dirname(self.store)
        os.makedirs(directory, exist_ok=True)
        # Replace, never rewrite in place: a crash mid-write must not drop every other decision.
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".trusted-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"projects": sorted(roots)}, handle, indent=2)
            os.replace(temporary, self.store)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
