"""Configuration travels through the real worker path, including failed reload and rollback."""

import json

import pytest

from wizolt.config import Config
from wizolt.plugins.catalog import PluginCatalog
from wizolt.plugins.installation import PluginInstallations
from wizolt.plugins.runtime import PluginRuntime
from wizolt.plugins.settings import PluginSettings
from wizolt.plugins.testing import PluginTrial
from wizolt.sdk import Context, Plugin, PluginError
from wizolt.ui.cli.plugin_testing import main

SOURCE = '''SDK_VERSION = 1
def setup(plugin):
    plugin.configure({"type": "object", "properties": {"count": {"type": "integer", "minimum": 1}}, "additionalProperties": False}, defaults={"count": 2})
    plugin.field("count", lambda context: plugin.config["count"])
'''


def context():
    return Context("test", "main", "/tmp", "idle", 0, 0, "test", 0)


def test_sdk_settings_are_deeply_immutable_and_defaulted():
    plugin = Plugin("test", {"nested": {"items": [1, 2]}})
    plugin.configure({"type": "object"}, defaults={"count": 2})
    assert plugin.config["count"] == 2
    assert plugin.config["nested"]["items"] == (1, 2)
    with pytest.raises(TypeError):
        plugin.config["nested"]["new"] = 3
    with pytest.raises(AttributeError):
        plugin.config = {}


def test_config_validation_does_not_echo_values_or_fetch_remote_schemas():
    plugin = Plugin("test", {"count": "private-token"})
    with pytest.raises(PluginError, match="count: type") as raised:
        plugin.configure({"properties": {"count": {"type": "integer"}}})
    assert "private-token" not in str(raised.value)
    with pytest.raises(PluginError, match="local references"):
        plugin.configure({"$ref": "https://example.invalid/schema"})


async def test_reload_reads_config_atomically_and_rollback_restores_settings(tmp_path):
    source = tmp_path / "counter.py"
    source.write_text(SOURCE)
    config = tmp_path / "config.toml"
    config.write_text("[plugins.counter]\ncount = 3\n")
    runtime = PluginRuntime(context, PluginSettings(path=str(config)))
    try:
        await runtime.manage("enable", str(source))
        assert runtime.fields()["plugins.counter.count"] == 3
        config.write_text("[plugins.counter]\ncount = 5\n")
        await runtime.start_turn()
        await runtime.manage("reload", "counter")
        assert runtime.fields()["plugins.counter.count"] == 3
        await runtime.finish_turn()
        assert runtime.fields()["plugins.counter.count"] == 5
        config.write_text('[plugins.counter]\ncount = "private-token"\n')
        with pytest.raises(PluginError) as raised:
            await runtime.manage("reload", "counter")
        assert "private-token" not in str(raised.value)
        assert runtime.fields()["plugins.counter.count"] == 5
        await runtime.manage("rollback", "counter")
        assert runtime.fields()["plugins.counter.count"] == 3
        assert "settings" not in json.dumps(await runtime.manage("inspect", "counter"))
    finally:
        await runtime.close()


async def test_trial_uses_the_same_defaults_and_validation(tmp_path):
    source = tmp_path / "counter.py"
    source.write_text(SOURCE)
    report = await PluginTrial(context()).run(str(source))
    assert report.status == "passed"
    assert report.frames[0]["fields"] == {"count": 2}
    report = await PluginTrial(context(), settings=PluginSettings({"counter": {"count": 0}})).run(str(source), validate=True)
    assert report.status == "failed"
    assert "minimum" in report.error


def test_cli_validate_loads_plugin_config_without_provider(tmp_path, capsys):
    source = tmp_path / "counter.py"
    source.write_text(SOURCE)
    config = tmp_path / "config.toml"
    config.write_text('[plugins.counter]\ncount = "private-token"\n')
    assert main(["validate", str(source), "--config", str(config), "--project", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "failed"
    assert "private-token" not in output


@pytest.mark.parametrize("value", [[], "secret", {"date": object()}, {"number": float("inf")}])
def test_reject_invalid_tables_and_non_json_values(value):
    with pytest.raises(PluginError):
        PluginSettings({"example": value}).read("example")


def test_config_carries_plugin_tables():
    assert Config.from_dict({"plugins": {"example": {"count": 3}}}).plugins == {"example": {"count": 3}}


async def test_worker_dependencies_are_not_shadowed_by_workspace_files(tmp_path):
    (tmp_path / "attrs.py").write_text('raise RuntimeError("workspace imported")')
    source = tmp_path / "counter.py"
    source.write_text(SOURCE)
    from dataclasses import replace

    report = await PluginTrial(replace(context(), cwd=str(tmp_path))).run(str(source))
    assert report.status == "passed", report.error


async def test_installation_rejects_bad_settings_without_saving(tmp_path):
    source = tmp_path / "counter.py"
    source.write_text(SOURCE)
    catalog = PluginCatalog.for_project(str(tmp_path / "data"), str(tmp_path))
    manager = PluginInstallations(catalog, str(tmp_path), PluginSettings({"counter": {"count": 0}}))
    with pytest.raises(PluginError, match="minimum"):
        await manager.manage("enable", str(source))
    assert "counter" not in catalog.read()[0]
