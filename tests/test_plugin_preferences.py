"""Portable preferences and failure-safe config transactions, through their public boundary."""

import fcntl
import json
import stat

import pytest

from wizolt.base import ConfigError
from wizolt.config import ConfigFile
from wizolt.plugins.catalog import Installation, PluginCatalog
from wizolt.plugins.preferences import PluginPreferences


def test_preferences_keep_config_comments_symlinks_and_plugin_settings(tmp_path):
    source = tmp_path / "dotfiles.toml"
    source.write_text('# My profile\n[plugins.pet]\nstyle = "quiet" # keep this\n')
    source.chmod(0o600)
    link = tmp_path / "config.toml"
    link.symlink_to(source)
    catalog = PluginCatalog.for_user(str(tmp_path / "data"), str(link))
    catalog.save(Installation("pet", "ignored-builtin-source", False))
    catalog.preferences.save("shortcuts", "bindings", {"f6": "pet.show"})
    catalog.preferences.save("layout", "above_input", {"order": ["pet.above_input"], "gaps": {"pet.above_input": 2}})
    assert link.is_symlink()
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert '# My profile' in source.read_text() and '# keep this' in source.read_text()
    assert ConfigFile.load(str(link))["plugins"] == {"pet": {"style": "quiet"}}
    # Only the config is copied to another profile; cached installation state is absent.
    copied = tmp_path / "other.toml"
    copied.write_text(source.read_text())
    other = PluginCatalog.for_user(str(tmp_path / "another-machine"), str(copied))
    assert not other.read()[0]["pet"].enabled
    assert other.preferences.read("shortcuts")["bindings"] == {"f6": "pet.show"}
    assert other.preferences.read("layout")["above_input"]["gaps"] == {"pet.above_input": 2}


def test_existing_preferences_are_copied_once_without_removing_originals(tmp_path):
    catalog = PluginCatalog.for_user(str(tmp_path))
    catalog.directory.mkdir(parents=True)
    old = catalog.directory / "sample.json"
    old.write_text(json.dumps({"name": "sample", "path": "/sample.py", "enabled": True}))
    layout = catalog.directory / "layout"
    layout.mkdir()
    (layout / "above_input.json").write_text('["sample.above_input"]')
    records, errors = catalog.read()
    assert not errors and records["sample"].enabled
    assert old.exists()
    assert catalog.preferences.read("layout")["above_input"]["order"] == ["sample.above_input"]
    catalog.save(Installation("sample", "/sample.py", False))
    assert not catalog.read()[0]["sample"].enabled  # Old enabled=true never reappears.


def test_busy_config_refuses_write_without_blocking_or_losing_other_preferences(tmp_path):
    path = tmp_path / "config.toml"
    preferences = PluginPreferences(path)
    preferences.save("shortcuts", "bindings", {"f6": "a.open"})
    before = path.read_bytes()
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(ConfigError, match="another process"):
            preferences.save("layout", "above_input", {"order": []})
    assert path.read_bytes() == before
    preferences.save("layout", "above_input", {"order": []})
    assert preferences.read("shortcuts")["bindings"] == {"f6": "a.open"}


def test_recovery_startup_does_not_import_old_preferences(tmp_path, monkeypatch):
    catalog = PluginCatalog.for_user(str(tmp_path))
    catalog.directory.mkdir(parents=True)
    (catalog.directory / "broken.json").write_text("broken")
    monkeypatch.setenv("WIZOLT_NO_PLUGINS", "1")
    catalog.read()
    assert not catalog.preferences.path.exists()
