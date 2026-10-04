"""Host plugin preferences share the user's config, never a plugin's settings table.

Readers take fresh snapshots; writers change one named value through the comment-preserving
config writer. A profile can therefore travel with its config without carrying runtime caches.
"""

import json
import os
from pathlib import Path

from wizolt.base import Json
from wizolt.config import ConfigFile
from wizolt.plugins.files import read_regular
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

    def save(self, section: str, key: str, value, *, expected: Json | None = None) -> None:
        # Offline installation may precede --init-config. Create only an empty file;
        # provider defaults must not overwrite the user's eventual profile.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        ConfigFile.set_ui_value(str(self.path), ("plugin_manager", section), key, value, expected=expected)

    def import_existing(self, directory: Path) -> None:
        """Copy early-alpha preferences once; leave originals intact for recovery.

        Presence of plugin_manager makes the config authoritative, including deliberately
        empty sections. Recovery startup must not perform any automatic writes.
        """
        if os.environ.get("WIZOLT_NO_PLUGINS") or not directory.exists():
            return
        data = ConfigFile.load(str(self.path)) if self.path.exists() else {}
        if "plugin_manager" in data:
            return
        installations = {}
        for path in sorted(directory.glob("*.json")):
            record = json.loads(read_regular(path, 64 * 1024))
            if not isinstance(record, dict) or record.get("name") != path.stem:
                raise PluginError(f"Cannot import installation {path}")
            installations[path.stem] = {key: value for key, value in record.items() if key != "name"}
        layout = {}
        for path in sorted((directory / "layout").glob("*.json")):
            value = json.loads(read_regular(path, 64 * 1024))
            layout[path.stem] = {"order": value} if isinstance(value, list) else value
        shortcuts = directory / "shortcuts" / "bindings.json"
        manager = {}
        if installations:
            manager["installations"] = installations
        if layout:
            manager["layout"] = layout
        if shortcuts.exists():
            manager["shortcuts"] = {"bindings": json.loads(read_regular(shortcuts, 64 * 1024))}
        if manager:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(descriptor)
            ConfigFile.set_ui_value(str(self.path), (), "plugin_manager", manager, overwrite=False)
