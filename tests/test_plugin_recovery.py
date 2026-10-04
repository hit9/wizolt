"""The recovery flag must skip plugin imports without rewriting enabled preferences."""

import os
import subprocess
import sys

from wizolt.plugins.catalog import Installation, PluginCatalog


def test_no_plugins_flag_skips_broken_plugin_and_keeps_saved_choice(tmp_path):
    marker = tmp_path / "imported"
    plugin = tmp_path / "broken.py"
    plugin.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('broken plugin imported')\n")
    data = tmp_path / "data"
    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\ndata_dir = "{data}"\n[provider]\nactive = "test"\n[provider.test]\nurl = "http://127.0.0.1:9/v1"\nkey = "test"\nmodel = "test-model"\n')
    catalog = PluginCatalog.for_user(str(data), str(config))
    catalog.save(Installation("broken", str(plugin), True))
    # Disable unrelated remote maintenance in this subprocess, not the actual plugin loader.
    script = "from wizolt.ui.cli.update import UpdateChecker; from wizolt.providers.sync import CatalogRuntime; from wizolt.__main__ import main; UpdateChecker.load_cached=lambda self:False; CatalogRuntime.refresh_due=lambda self:False; main()"
    result = subprocess.run([sys.executable, "-c", script, "--no-plugins", "--config", str(config)], input="/exit\n", text=True,
                            capture_output=True, timeout=15, check=False, env={key: value for key, value in os.environ.items() if key != "WIZOLT_NO_PLUGINS"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    assert catalog.read()[0]["broken"].enabled
    assert "broken plugin imported" not in result.stdout + result.stderr
