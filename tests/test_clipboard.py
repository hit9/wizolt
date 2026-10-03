"""System clipboard copies report what actually happened."""

import shutil

import pytest

from wizolt.utils.clipboard import Clipboard

CAT = shutil.which("cat")


def install(directory, name, body):
    script = directory / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)


@pytest.fixture
def path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    return tmp_path


def test_the_first_available_command_receives_the_text(path):
    install(path, "xsel", f'{CAT} > "{path}/xsel.out"\n')
    install(path, "wl-copy", f'{CAT} > "{path}/copied"\n')

    assert Clipboard.copy('format = "{model} ⚡"') is None
    assert (path / "copied").read_text() == 'format = "{model} ⚡"'
    assert not (path / "xsel.out").exists()


def test_a_failing_command_is_reported_rather_than_claimed(path):
    install(path, "pbcopy", "exit 1\n")

    assert Clipboard.copy("text").startswith("pbcopy failed:")


def test_no_command_says_which_were_looked_for(path):
    assert Clipboard.copy("text") == "no clipboard command found (pbcopy, wl-copy, xclip, xsel, clip.exe)"
