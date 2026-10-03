"""Shell-facing plugin authoring and saved preferences; never guess a live wizolt process."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from wizolt.plugins.installation import PluginInstallations
from wizolt.plugins.workspace import PluginWorkspace


def main(argv: list[str]) -> int:
    if argv[:1] in (["test"], ["validate"]):
        from wizolt.ui.cli.plugin_testing import main as test_main

        return test_main(argv)
    parser = argparse.ArgumentParser(prog="wizolt plugin", description="Manage saved plugin preferences. Live activation uses PluginHotReload.")
    parser.add_argument("action", choices=("list", "inspect", "install", "enable", "disable", "test", "validate"))
    parser.add_argument("target", nargs="?", default="", help="Installed name, or .py path for install/enable")
    parser.add_argument("--config", default=None, help="Use the same config file as the running wizolt")
    parser.add_argument("--project", default=str(Path.cwd()), help="Project directory (defaults to current directory)")
    args = parser.parse_args(argv)
    if args.action != "list" and not args.target:
        parser.error("this action requires a plugin name or source path")
    try:
        workspace = PluginWorkspace.open(args.config, args.project)
        result = asyncio.run(PluginInstallations(workspace.catalog, workspace.cwd).manage(args.action, args.target))
    except Exception as error:  # noqa: BLE001 - commands return machine-readable errors, including bad configuration.
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
