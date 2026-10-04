"""Persistent edits exercise real workers, without implicitly replacing live configuration."""

import json

import pytest

from wizolt.config import ConfigFile
from wizolt.plugins.runtime import PluginRuntime
from wizolt.plugins.settings import PluginSettings
from wizolt.sdk import Context, PluginError


@pytest.fixture
async def editable(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('# profile\n[plugins.editable]\ncount = 3 # retained\n[unrelated]\nx = "yes"\n')
    source = tmp_path / "editable.py"
    source.write_text('''import json
SDK_VERSION = 1
def setup(p):
    p.configure({"type": "object", "properties": {"count": {"type": "integer", "minimum": 1}, "items": {"type": "array"}}, "additionalProperties": False}, defaults={"count": 2})
    async def edit(ctx, args):
        result = await p.settings.update(args.get("values", {}), reset=args.get("reset", []))
        return json.dumps({"saved": result["count"], "live": p.config["count"]})
    p.command("edit", "Edit", edit)
''')
    runtime = PluginRuntime(lambda: Context("s", "main", str(tmp_path), "idle", 0, 0, "test", 0), PluginSettings(path=str(config)))
    await runtime.manage("enable", str(source))
    try:
        yield runtime, config
    finally:
        await runtime.close()


async def test_update_and_reset_preserve_comments_and_generation(editable):
    runtime, config = editable
    result = json.loads(await runtime.invoke("editable", "command", "edit", {"values": {"count": 7, "items": [{"a": True}]}}))
    assert result == {"saved": 7, "live": 3}
    assert '# profile' in config.read_text() and '# retained' in config.read_text()
    assert ConfigFile.load(str(config))["unrelated"] == {"x": "yes"}
    await runtime.manage("reload", "editable")
    result = json.loads(await runtime.invoke("editable", "command", "edit", {"reset": ["count"]}))
    assert result == {"saved": 2, "live": 7}
    assert "count" not in ConfigFile.load(str(config))["plugins"]["editable"]


@pytest.mark.parametrize("arguments", [{"values": {"count": 0}}, {"values": {"other": 1}}, {"values": {"count": None}}, {"values": {"count": 5}, "reset": ["count"]}])
async def test_invalid_updates_do_not_touch_config(editable, arguments):
    runtime, config = editable
    before = config.read_bytes()
    with pytest.raises(PluginError):
        await runtime.invoke("editable", "command", "edit", arguments)
    assert config.read_bytes() == before


async def test_update_refuses_concurrent_change(editable, monkeypatch):
    runtime, config = editable
    original = runtime.settings.call

    async def concurrent(name, service, arguments):
        result = await original(name, service, arguments)
        if service == "settings.read":
            ConfigFile.set_ui_value(str(config), ("plugins", "editable"), "count", 9)
        return result

    monkeypatch.setattr(runtime.settings, "call", concurrent)
    with pytest.raises(PluginError, match="concurrently"):
        await runtime.invoke("editable", "command", "edit", {"values": {"count": 5}})
    assert ConfigFile.load(str(config))["plugins"]["editable"]["count"] == 9
