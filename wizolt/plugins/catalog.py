"""Durable user installations, separate from an agent's active plugin generations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path

from wizolt.base import ConfigError
from wizolt.plugins.preferences import PluginPreferences
from wizolt.sdk import PluginError


@dataclass(frozen=True)
class Installation:
    """A user startup preference, not evidence that any existing agent has activated it."""

    name: str
    path: str
    enabled: bool = True
    python: str = ""


class PluginCatalog:
    """User installation choices in the config, separate from live worker generations.

    Catalog writes describe future agent startup, not live activation. Same-plugin concurrent
    writes use the config transaction; installing another plugin retains unrelated settings.
    """

    def __init__(self, directory: Path, defaults: tuple[Installation, ...] = (), preferences: PluginPreferences | None = None):
        self.directory = directory
        self.defaults = {item.name: item for item in defaults}
        self.preferences = preferences or PluginPreferences(directory.parent.parent / "config.toml")

    @classmethod
    def for_user(cls, data_dir: str, config_path: str = "") -> PluginCatalog:
        """Installation and layout follow the user; cwd only supplies execution context."""
        root = Path(data_dir).expanduser().resolve()
        directory = root / "plugins" / ".state"
        bundled = Path(__file__).parent / "builtin"
        return cls(
            directory,
            tuple(Installation(path.stem, str(path), enabled=False) for path in sorted(bundled.glob("*.py"))),
            PluginPreferences(Path(config_path).expanduser() if config_path else root / "config.toml"),
        )

    def read(self) -> tuple[dict[str, Installation], list[str]]:
        """Report damaged records individually so one plugin cannot prevent project startup."""
        # Bundled sources are installation defaults, not a second execution path. A user's
        # saved enable/disable choice overlays them, including across application upgrades.
        records = dict(self.defaults)
        problems = []
        try:
            self.preferences.import_existing(self.directory)
            saved = self.preferences.read("installations")
        except (OSError, ValueError, ConfigError) as error:
            # Failed reads are not evidence of a user's disable choice. In particular,
            # bundled defaults must never retire a healthy live generation on reload.
            return {}, [str(error)]
        for name, data in saved.items():
            try:
                if name in self.defaults and isinstance(data, dict):
                    data = {"path": self.defaults[name].path, **data}
                item = Installation(name=name, **data)
                if not isinstance(item.path, str) or not isinstance(item.python, str) or type(item.enabled) is not bool:
                    raise PluginError("invalid installation record")
                if default := self.defaults.get(item.name):
                    # Upgrades can relocate package data. Preserve the saved preference, but
                    # resolve bundled source from this installation rather than an old venv.
                    item = replace(item, path=default.path)
                records[item.name] = item
            except (OSError, ValueError, TypeError) as error:
                records.pop(name, None)
                problems.append(f"{name}: {error}")
        return records, problems

    def save(self, item: Installation) -> None:
        """Replace one installation table without rewriting unrelated profile choices."""
        if not item.name.isidentifier() or not item.name.isascii():
            raise PluginError("Invalid plugin name")
        self.preferences.import_existing(self.directory)
        data = asdict(item)
        data.pop("name")
        if item.name in self.defaults:
            data.pop("path")
        if not item.python:
            data.pop("python")
        self.preferences.save("installations", item.name, data)
