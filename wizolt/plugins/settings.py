"""Read only plugin settings on reload, leaving live provider/runtime settings untouched."""

import json
from dataclasses import dataclass, field

from wizolt.config import Config, ConfigFile
from wizolt.sdk import PluginError


@dataclass
class PluginSettings:
    """A settings source, not a shared mutable generation; each read returns fresh JSON data."""

    values: dict = field(default_factory=dict)
    path: str = ""

    async def call(self, name: str, service: str, arguments: dict) -> dict:
        """Own-name persistence only. No caller-supplied path or plugin identity is accepted.

        The SDK validates schema inside the worker before update. Python plugins are trusted
        code, not a sandbox; this boundary still forbids accidentally editing another table.
        """
        from wizolt.sdk.settings import check_values

        if service == "settings.read" and not arguments:
            return {"values": self.read(name)}
        if service != "settings.update" or set(arguments) != {"values", "reset", "expected"}:
            raise PluginError("Invalid settings operation")
        values, reset, expected = arguments["values"], arguments["reset"], arguments["expected"]
        if not isinstance(values, dict) or not isinstance(expected, dict) or not isinstance(reset, list) or any(not isinstance(key, str) for key in reset):
            raise PluginError("Invalid settings update")
        check_values(values)
        if not self.path:
            raise PluginError("Persistent settings require a config path")
        ConfigFile.patch_table(self.path, ("plugins", name), values, reset, expected=expected)
        return {}

    def read(self, name: str) -> dict:
        from pathlib import Path

        tables = Config.table(ConfigFile.load(self.path), "plugins") if self.path and Path(self.path).exists() else self.values
        value = tables.get(name, {})
        if not isinstance(value, dict):
            raise PluginError(f"plugins.{name} must be a table")
        try:
            # TOML also admits dates and non-finite floats. Reject those at this boundary,
            # not later during RPC serialization, without exposing the offending value.
            return json.loads(json.dumps(value, allow_nan=False))
        except (ValueError, TypeError):
            raise PluginError(f"plugins.{name} must contain finite JSON-compatible values") from None
