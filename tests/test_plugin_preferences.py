"""Portable preferences and failure-safe config transactions, through their public boundary."""

import fcntl
import stat

import pytest

from wizolt.base import ConfigError
from wizolt.config import ConfigFile
from wizolt.plugins.catalog import Installation, PluginCatalog
from wizolt.plugins.preferences import PluginPreferences


@pytest.mark.parametrize("record", [[], [["enabled", True]]])
def test_array_is_not_an_installation_record(tmp_path, record):
    catalog = PluginCatalog.for_user(str(tmp_path))
    catalog.preferences.save("installations", "pet", record)
    records, errors = catalog.read()
    assert "pet" not in records
    assert errors == ["pet: installation must be a table"]


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


def test_runtime_cache_never_supplies_preferences_or_creates_config(tmp_path):
    catalog = PluginCatalog.for_user(str(tmp_path))
    catalog.directory.mkdir(parents=True)
    (catalog.directory / "sample.json").write_text("invalid cache")
    records, errors = catalog.read()
    assert not errors and "sample" not in records
    assert not catalog.preferences.path.exists()
    catalog.save(Installation("sample", "/sample.py", False))
    assert not catalog.read()[0]["sample"].enabled


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


def test_generated_interpreters_are_local_but_environment_choices_are_atomic(tmp_path):
    catalog = PluginCatalog.for_user(str(tmp_path / "machine"))
    python = str(catalog.directory / "environments" / "abc123" / "bin/python")
    catalog.save(Installation("example", "/example.py", python=python))
    config = catalog.preferences.path
    assert python not in config.read_text()
    assert catalog.read()[0]["example"].python == python
    # Copying a profile never launches the old computer's interpreter. Reinstall dependencies
    # on the destination; a missing local environment must not fall back to the host Python.
    other = PluginCatalog.for_user(str(tmp_path / "other"), str(config))
    assert other.read()[0]["example"].python == str(other.directory / "environments" / "abc123" / "bin/python")
    with open(str(config) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(ConfigError):
            catalog.save(Installation("example", "/example.py", python=python.replace("abc123", "def456")))
    assert catalog.read()[0]["example"].python == python


@pytest.mark.parametrize("environment", [False, 0, "../outside", "/absolute"])
def test_invalid_environment_does_not_become_a_host_interpreter(tmp_path, environment):
    catalog = PluginCatalog.for_user(str(tmp_path))
    catalog.preferences.save("installations", "example", {"path": "/example.py", "environment": environment})
    records, errors = catalog.read()
    assert "example" not in records
    assert errors == ["example: invalid dependency environment"]

