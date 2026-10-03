"""Durable project installations, separate from an agent's active plugin generations."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from wizolt.sdk import PluginError


@dataclass(frozen=True)
class Installation:
    """A project startup preference, not evidence that any existing agent has activated it."""

    name: str
    path: str
    enabled: bool = True
    launch: str = ""


class PluginCatalog:
    """One atomic file per plugin avoids lost updates between independent session processes.

    Catalog writes describe future agent startup, not live activation. Same-plugin concurrent
    writes use last-writer-wins; installing another plugin never replaces this plugin's record.
    """

    def __init__(self, directory: Path, defaults: tuple[Installation, ...] = ()):
        self.directory = directory
        self.defaults = {item.name: item for item in defaults}

    def read(self) -> tuple[dict[str, Installation], list[str]]:
        """Report damaged records individually so one plugin cannot prevent project startup."""
        # Bundled sources are installation defaults, not a second execution path. A user's
        # saved enable/disable choice overlays them, including across application upgrades.
        records = dict(self.defaults)
        problems = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                item = Installation(**data)
                if item.name != path.stem or not isinstance(item.path, str) or not isinstance(item.launch, str) or type(item.enabled) is not bool:
                    raise PluginError("invalid installation record")
                if default := self.defaults.get(item.name):
                    # Upgrades can relocate package data. Preserve the saved preference, but
                    # resolve bundled source from this installation rather than an old venv.
                    item = replace(item, path=default.path)
                records[item.name] = item
            except (OSError, ValueError, TypeError) as error:
                problems.append(f"{path.name}: {error}")
        return records, problems

    def save(self, item: Installation) -> None:
        """Replace one complete record; readers must never see a partially written JSON file."""
        if not item.name.isidentifier() or not item.name.isascii():
            raise PluginError("Invalid plugin name")
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".install-")
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(asdict(item), output, indent=2)
            os.replace(temporary, self.directory / f"{item.name}.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
