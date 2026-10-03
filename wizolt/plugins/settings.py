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

    def read(self, name: str) -> dict:
        tables = Config.table(ConfigFile.load(self.path), "plugins") if self.path else self.values
        value = tables.get(name, {})
        if not isinstance(value, dict):
            raise PluginError(f"plugins.{name} must be a table")
        try:
            # TOML also admits dates and non-finite floats. Reject those at this boundary,
            # not later during RPC serialization, without exposing the offending value.
            return json.loads(json.dumps(value, allow_nan=False))
        except (ValueError, TypeError):
            raise PluginError(f"plugins.{name} must contain finite JSON-compatible values") from None
