"""Plugin declarations flow through real theme/picker/format boundaries, without host imports."""

import json
from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document
from tui_harness import loop as make_loop

from wizolt.sdk import PluginError
from wizolt.ui.cli.appearance import AppearancePicker, direct
from wizolt.ui.cli.plugin_testing import main
from wizolt.ui.render import Theme


@pytest.fixture(autouse=True)
def palette(monkeypatch):
    monkeypatch.setattr(Theme, "_plugins", {})
    monkeypatch.setattr(Theme, "_mode", "dark")
    monkeypatch.setattr(Theme, "_custom", {})
    monkeypatch.setattr(Theme, "_bar_themes", {"statusbar": "inherit", "divider": "inherit"})


def source(path, *, accent="#6699aa", label="DIY"):
    path.write_text(
        'SDK_VERSION = 1\ndef setup(p):\n'
        f'    p.theme("night", {{"base": "dark", "colors": {{"accent": "{accent}"}}}})\n'
        f'    p.preset("statusbar", "compact", "{label} {{model}}")\n'
        '    p.preset("divider", "line", "{spinner} {label}")\n'
    )
    return str(path)


async def test_plugin_appearance_picker_completion_reload_disable_and_reenable(tmp_path):
    loop = make_loop(tmp_path)
    runtime = loop.session.plugins
    path = tmp_path / "look.py"
    try:
        await runtime.manage("enable", source(path))
        name = "plugins.look.compact"
        assert "plugins.look.night" in Theme.choices()
        assert "statusbar.format" in direct(loop, "statusbar", name)
        picker = AppearancePicker(loop, 0)
        assert name in picker.presets("statusbar")
        assert picker.selected["statusbar"] == name
        assert picker.source("statusbar") == "preset:" + name
        picker.restore()
        completions = [item.text for item in loop.input_completer.get_completions(Document("/theme statusbar plugins."), None)]
        assert name in completions
        loop.session.settings.theme = "plugins.look.night"
        loop.plugin_appearance.activate()
        assert Theme.color("accent") == "#6699aa"
        source(path, accent="#77aabb", label="NEW")
        await runtime.manage("reload", "look")
        layout = loop.presentation.status_bar.layout
        assert layout.presets["statusbar"][name] == "NEW {model}"
        assert Theme.color("accent") == "#77aabb"
        await runtime.manage("disable", "look")
        assert name not in layout.presets["statusbar"]
        assert layout.sources["statusbar"] == "preset:" + name
        assert Theme.name() == "dark"
        await runtime.manage("enable", "look")
        assert Theme.name() == "plugins.look.night"
        assert layout.sources["statusbar"] == "preset:" + name
    finally:
        await runtime.close()


async def test_bad_appearance_candidate_keeps_previous_and_background_palette_isolated(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "child").mkdir()
    root = make_loop(tmp_path / "root")
    child = make_loop(tmp_path / "child")
    root.agents_frontend = child.agents_frontend = SimpleNamespace(current=SimpleNamespace(loop=root))
    path = tmp_path / "look.py"
    try:
        await root.session.plugins.manage("enable", source(path))
        root.session.settings.theme = "plugins.look.night"
        root.plugin_appearance.activate()
        await child.session.plugins.manage("enable", source(path, accent="#aaccee"))
        assert Theme.color("accent") == "#6699aa"
        child.session.settings.theme = "plugins.look.night"
        child.plugin_appearance.activate()
        assert Theme.color("accent") == "#aaccee"
        root.plugin_appearance.activate()
        assert Theme.color("accent") == "#6699aa"
        with pytest.raises(PluginError, match="accent"):
            await root.session.plugins.manage("enable", source(path, accent="not-a-color"))
        assert Theme.color("accent") == "#6699aa"
    finally:
        await root.session.plugins.close()
        await child.session.plugins.close()


def test_offline_validation_rejects_bad_appearance_and_accepts_plugin_theme(tmp_path, capsys):
    path = tmp_path / "look.py"
    source(path)
    assert main(["test", str(path), "--theme", "plugins.look.night", "--project", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["theme"] == "plugins.look.night"
    source(path, label="{missing_field}")
    assert main(["validate", str(path), "--project", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


async def test_configured_plugin_preset_is_resolved_after_startup_load(tmp_path):
    loop = make_loop(tmp_path)
    loop.session.config.ui = {"statusbar": {"format": "preset:plugins.look.compact"}}
    loop.configure_theme()
    try:
        await loop.session.plugins.manage("enable", source(tmp_path / "look.py"))
        layout = loop.presentation.status_bar.layout
        fragments = layout.render("statusbar", {"model": "test"}, 80, Theme.bar_styles)
        assert "DIY test" in "".join(text for _, text in fragments)
    finally:
        await loop.session.plugins.close()
