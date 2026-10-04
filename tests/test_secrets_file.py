"""Provider keys kept in secrets.toml beside the config, and the migration that moves them there."""

import os
import stat

import pytest

from wizolt.base import ConfigError
from wizolt.config import Config, ConfigFile


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


ACTIVE_A = '[provider]\nactive = "a"\n'


def keys(path):
    config = Config.from_dict(ConfigFile.load(str(path)))
    return {name: provider.key for name, provider in config.providers.items()}


def test_keys_come_from_the_secrets_file_beside_the_config(tmp_path):
    config = write(tmp_path / "config.toml", '[provider]\nactive = "a"\n[provider.a]\nurl = "u"\n[provider.b]\nurl = "u"\n')
    write(tmp_path / "secrets.toml", 'a = "sk-a"\nb = "sk-b"\n')

    assert keys(config) == {"a": "sk-a", "b": "sk-b"}


def test_a_flat_provider_block_takes_the_key_under_its_active_name(tmp_path):
    config = write(tmp_path / "config.toml", '[provider]\nurl = "u"\nmodel = "m"\n')
    write(tmp_path / "secrets.toml", 'default = "sk-flat"\n')

    assert keys(config) == {"default": "sk-flat"}


def test_a_key_written_in_the_config_wins_and_an_empty_one_falls_back(tmp_path):
    config = write(tmp_path / "config.toml", ACTIVE_A + '[provider.a]\nkey = "sk-inline"\n[provider.b]\nkey = ""\n[provider.c]\n')
    write(tmp_path / "secrets.toml", 'a = "sk-old"\nb = "sk-b"\n')

    assert keys(config) == {"a": "sk-inline", "b": "sk-b", "c": ""}


def test_without_a_secrets_file_the_config_loads_as_before(tmp_path):
    config = write(tmp_path / "config.toml", ACTIVE_A + '[provider.a]\nkey = "sk-inline"\n')

    assert keys(config) == {"a": "sk-inline"}


@pytest.mark.parametrize(
    ("secrets", "message"),
    [
        ('ghost = "sk"\n', "`ghost` is not a provider entry"),
        ("[a]\nkey = 'sk'\n", "`a` must be a string"),
        ("a = 1\n", "`a` must be a string"),
        ("a = \n", "invalid config"),
    ],
)
def test_the_secrets_file_holds_only_keys_of_configured_entries(tmp_path, secrets, message):
    config = write(tmp_path / "config.toml", "[provider.a]\n")
    write(tmp_path / "secrets.toml", secrets)

    with pytest.raises(ConfigError, match=message):
        ConfigFile.load(str(config))


def test_an_empty_placeholder_for_an_unknown_entry_is_not_an_error(tmp_path):
    config = write(tmp_path / "config.toml", ACTIVE_A + "[provider.a]\n")
    write(tmp_path / "secrets.toml", 'default = ""\n')

    assert keys(config) == {"a": ""}


def test_init_writes_a_keyless_config_and_a_private_secrets_file(tmp_path):
    path, created = ConfigFile.init(str(tmp_path / "config.toml"))
    secrets = tmp_path / "secrets.toml"

    assert created
    assert "key" not in ConfigFile.read_toml(path)["provider"]["default"]
    assert stat.S_IMODE(os.stat(secrets).st_mode) == 0o600
    write(secrets, secrets.read_text(encoding="utf-8").replace('default = ""', 'default = "sk-new"'))
    assert keys(path) == {"default": "sk-new"}


def test_init_keeps_an_existing_config_and_secrets_file(tmp_path):
    config = write(tmp_path / "config.toml", "[provider.a]\n")
    secrets = write(tmp_path / "secrets.toml", 'a = "sk"\n')

    assert ConfigFile.init(str(config)) == (str(config), False)
    assert secrets.read_text(encoding="utf-8") == 'a = "sk"\n'


def test_a_symlinked_config_reads_secrets_beside_the_link_not_its_target(tmp_path):
    dotfiles, home = tmp_path / "dotfiles", tmp_path / "home"
    dotfiles.mkdir()
    home.mkdir()
    write(dotfiles / "config.toml", ACTIVE_A + "[provider.a]\n")
    write(dotfiles / "secrets.toml", 'a = "sk-checkout"\n')
    write(home / "secrets.toml", 'a = "sk-home"\n')
    (home / "config.toml").symlink_to(dotfiles / "config.toml")

    assert keys(home / "config.toml") == {"a": "sk-home"}
