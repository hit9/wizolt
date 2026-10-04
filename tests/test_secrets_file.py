"""Provider keys kept in secrets.toml beside the config, and the migration that moves them there."""

import os
import stat
import sys
from types import SimpleNamespace

import pytest

import wizolt.__main__ as cli
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


def test_migration_moves_config_keys_into_secrets_and_keeps_both_files_layout(tmp_path):
    config = write(
        tmp_path / "config.toml",
        ACTIVE_A + '[provider.a]\nurl = "u"  # endpoint\nkey = "sk-a"\n\n# second entry\n[provider.b]\nkey = "sk-b"\n[provider.c]\nkey = ""\n',
    )
    secrets = write(tmp_path / "secrets.toml", '# mine\nb = "sk-stale"\nother = ""\n')
    before = keys(config)

    assert ConfigFile.migrate_keys(str(config)) == ["a", "b"]
    assert config.read_text(encoding="utf-8") == ACTIVE_A + '[provider.a]\nurl = "u"  # endpoint\n\n# second entry\n[provider.b]\n[provider.c]\nkey = ""\n'
    assert secrets.read_text(encoding="utf-8") == '# mine\nb = "sk-b"\nother = ""\na = "sk-a"\n'
    assert keys(config) == before
    assert ConfigFile.migrate_keys(str(config)) == []


def test_migration_creates_a_private_secrets_file_and_keeps_the_configs_mode(tmp_path):
    config = write(tmp_path / "config.toml", '[provider]\nurl = "u"\nkey = "sk-flat"\n')
    config.chmod(0o640)

    assert ConfigFile.migrate_keys(str(config)) == ["default"]
    assert stat.S_IMODE(os.stat(tmp_path / "secrets.toml").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(config).st_mode) == 0o640
    assert keys(config) == {"default": "sk-flat"}


def test_migration_without_config_keys_writes_nothing(tmp_path):
    config = write(tmp_path / "config.toml", '[provider.a]\nkey = ""\n')

    assert ConfigFile.migrate_keys(str(config)) == []
    assert not (tmp_path / "secrets.toml").exists()


def test_migration_edits_a_symlinked_config_in_place_and_keeps_keys_out_of_its_checkout(tmp_path):
    dotfiles, home = tmp_path / "dotfiles", tmp_path / "home"
    dotfiles.mkdir()
    home.mkdir()
    target = write(dotfiles / "config.toml", ACTIVE_A + '[provider.a]\nkey = "sk-a"\n')
    (home / "config.toml").symlink_to(target)

    assert ConfigFile.migrate_keys(str(home / "config.toml")) == ["a"]
    assert (home / "config.toml").is_symlink()
    assert "sk-a" not in target.read_text(encoding="utf-8")
    assert not (dotfiles / "secrets.toml").exists()
    assert keys(home / "config.toml") == {"a": "sk-a"}


def test_a_declined_move_is_remembered_until_a_key_is_new_or_changed(tmp_path):
    config = write(tmp_path / "config.toml", f'[paths]\ndata_dir = "{tmp_path / "data"}"\n' + ACTIVE_A + '[provider.a]\nkey = "sk-a"\n')
    inline = ConfigFile.inline_keys(str(config))

    assert inline.keys == {"a": "sk-a"}
    assert not inline.declined
    inline.decline()
    assert ConfigFile.inline_keys(str(config)).declined
    assert "sk-a" not in (tmp_path / "data" / "kept-config-keys").read_text(encoding="utf-8")

    write(config, config.read_text(encoding="utf-8").replace("sk-a", "sk-rotated"))
    assert not ConfigFile.inline_keys(str(config)).declined
    write(config, config.read_text(encoding="utf-8") + '[provider.b]\nkey = "sk-b"\n')
    assert not ConfigFile.inline_keys(str(config)).declined


def test_no_config_means_no_inline_keys(tmp_path):
    assert ConfigFile.inline_keys(str(tmp_path / "missing.toml")).keys == {}


@pytest.fixture
def inline_config(tmp_path):
    return write(tmp_path / "config.toml", f'[paths]\ndata_dir = "{tmp_path / "data"}"\n' + ACTIVE_A + '[provider.a]\nkey = "sk-a"\n')


@pytest.fixture
def terminal(monkeypatch):
    """A terminal answering the prompt with the given replies; returns the prompts it was shown."""
    prompts = []

    def answer(*replies):
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        queue = list(replies)
        monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or queue.pop(0))
        return prompts

    return answer


@pytest.mark.parametrize("reply", ["", "y", "YES"])
def test_accepting_the_prompt_moves_the_keys_before_the_session_starts(inline_config, terminal, capsys, reply):
    prompts = terminal(reply)

    cli.offer_key_migration(str(inline_config))

    assert prompts == [f"Move them to {inline_config.parent / 'secrets.toml'}? [Y/n] "]
    assert ConfigFile.inline_keys(str(inline_config)).keys == {}
    assert keys(inline_config) == {"a": "sk-a"}
    assert "Moved keys for a" in capsys.readouterr().out


def test_declining_is_asked_once_then_only_reminded_until_a_key_changes(inline_config, terminal, capsys):
    prompts = terminal("n")
    cli.offer_key_migration(str(inline_config))
    assert len(prompts) == 1
    assert ConfigFile.inline_keys(str(inline_config)).keys == {"a": "sk-a"}

    cli.offer_key_migration(str(inline_config))
    assert len(prompts) == 1
    assert "run `wizolt --migrate-secrets`" in capsys.readouterr().err

    write(inline_config, inline_config.read_text(encoding="utf-8").replace("sk-a", "sk-rotated"))
    terminal("")
    cli.offer_key_migration(str(inline_config))
    assert len(prompts) == 2
    assert keys(inline_config) == {"a": "sk-rotated"}


def test_without_a_terminal_it_only_warns(inline_config, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("prompted without a terminal"))

    cli.offer_key_migration(str(inline_config))

    assert "holds API keys for a" in capsys.readouterr().err
    assert not ConfigFile.inline_keys(str(inline_config)).declined


def test_end_of_input_neither_moves_nor_remembers(inline_config, terminal, monkeypatch):
    terminal()
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError))

    cli.offer_key_migration(str(inline_config))

    info = ConfigFile.inline_keys(str(inline_config))
    assert info.keys == {"a": "sk-a"}
    assert not info.declined


@pytest.mark.parametrize("text", ["", "[provider.a]\nkey = \n"])
def test_no_keys_or_a_broken_config_starts_without_asking(tmp_path, monkeypatch, capsys, text):
    config = write(tmp_path / "config.toml", text)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("prompted"))

    cli.offer_key_migration(str(config))

    assert capsys.readouterr() == ("", "")


def test_the_migrate_flag_moves_keys_without_asking(inline_config, capsys):
    assert cli.main(["--migrate-secrets", "--config", str(inline_config)]) == 0
    assert keys(inline_config) == {"a": "sk-a"}
    assert ConfigFile.inline_keys(str(inline_config)).keys == {}

    assert cli.main(["--migrate-secrets", "--config", str(inline_config)]) == 0
    assert capsys.readouterr().out.splitlines() == [f"Moved keys for a to {inline_config.parent / 'secrets.toml'}", f"No keys to move in {inline_config}"]


def test_the_migrate_flag_reports_a_broken_config(tmp_path, capsys):
    config = write(tmp_path / "config.toml", "[provider.a]\nkey = \n")

    assert cli.main(["--migrate-secrets", "--config", str(config)]) == 2
    assert "ConfigError: invalid config" in capsys.readouterr().err
