"""Host plugin preferences share the user's config, never a plugin's settings table.

Readers take fresh snapshots; writers change one named value through the comment-preserving
config writer. A profile can therefore travel with its config without carrying runtime caches.
"""

import os
from pathlib import Path

from wizolt.config import ConfigFile
from wizolt.sdk import PluginError


class PluginPreferences:
    def __init__(self, path: Path):
        self.path = path

    def read(self, section: str) -> dict:
        data = ConfigFile.load(str(self.path)) if self.path.exists() else {}
        manager = data.get("plugin_manager", {})
        if not isinstance(manager, dict) or not isinstance(manager.get(section, {}), dict):
            raise PluginError(f"plugin_manager.{section} must be a table")
        return manager.get(section, {})

    def save(self, section: str, key: str, value) -> None:
        # Offline installation may precede --init-config. Create only an empty file;
        # provider defaults must not overwrite the user's eventual profile.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        ConfigFile.set_ui_value(str(self.path), ("plugin_manager", section), key, value)
