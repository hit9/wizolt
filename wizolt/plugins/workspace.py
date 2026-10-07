"""Provider-free authoring scope shared by standalone plugin commands.

An absent default config is valid for offline authoring. An explicitly selected missing or
malformed config is an error: silently falling back could modify the wrong user's choices.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from wizolt.base import Json
from wizolt.config import Config, ConfigFile
from wizolt.plugins.catalog import Installation, PluginCatalog
from wizolt.plugins.settings import PluginSettings


@dataclass(frozen=True)
class PluginWorkspace:
    cwd: str
    data: Json
    data_dir: str
    catalog: PluginCatalog
    # The config a loaded workspace actually read; empty when none was given and the usual
    # default is absent. A trial context should see the same paths the session would.
    config_path: str = ""

    @property
    def settings(self) -> PluginSettings:
        return PluginSettings(Config.table(self.data, "plugins"))

    @staticmethod
    def default_config() -> str | None:
        """The session's config when run from a wizolt agent's shell; otherwise the usual default."""
        return os.environ.get("WIZOLT_CONFIG") or None

    @staticmethod
    def default_project() -> str:
        """The session's directory, not the shell's current one, which an agent may change."""
        return os.environ.get("WIZOLT_SESSION_CWD") or str(Path.cwd())

    @classmethod
    def open(cls, config: str | None, project: str) -> "PluginWorkspace":
        exists = bool(config) or Path(ConfigFile.resolve_path(None)).exists()
        data = ConfigFile.load(config) if exists else {}
        cwd = str(Path(project).expanduser().resolve())
        data_dir = str(Path(Config.data_dir_from(data)).expanduser().resolve())
        return cls(cwd, data, data_dir, PluginCatalog.for_user(data_dir, ConfigFile.resolve_path(config)), ConfigFile.resolve_path(config) if exists else "")

    def installed(self, target: str) -> tuple[Installation | None, list[str]]:
        records, problems = self.catalog.read()
        path = str(Path(target).expanduser().resolve())
        return records.get(target) or next((item for item in records.values() if item.path == path), None), problems
