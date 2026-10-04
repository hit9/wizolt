"""Shell-facing plugin authoring and saved preferences; never guess a live wizolt process."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import wizolt
from wizolt.plugins.installation import PluginInstallations
from wizolt.plugins.workspace import PluginWorkspace
from wizolt.ui.cli.plugin_appearance import AppearanceContribution


def main(argv: list[str]) -> int:
    if argv[:1] in (["test"], ["validate"]):
        from wizolt.ui.cli.plugin_testing import main as test_main

        return test_main(argv)
    parser = argparse.ArgumentParser(prog="wizolt plugin", description="Manage saved plugin preferences. An agent applies them with Plugin(action=reload).")
    parser.add_argument("action", choices=("paths", "list", "inspect", "install", "enable", "disable", "test", "validate"))
    parser.add_argument("target", nargs="?", default="", help="Installed name, .py file, or package directory for install/enable")
    parser.add_argument("--config", default=PluginWorkspace.default_config(), help="Config file (default: the calling agent's, else the usual config)")
    parser.add_argument("--project", default=PluginWorkspace.default_project(), help="Project (default: the calling agent's, else the current directory)")
    args = parser.parse_args(argv)
    if args.action == "paths":
        if args.target:
            parser.error("paths does not take a plugin name")
        # Resolve this executable's package, not cwd, another Python interpreter, or a guessed
        # checkout. Keep discovery available even when config is missing or broken.
        package = Path(wizolt.__file__).resolve().parent
        reference = package / "skill" / "builtin" / "plugin-workshop"
        paths = {
            "source": package,
            "sdk": package / "sdk",
            "skill": reference / "SKILL.md",
            "api_reference": reference / "SDK.md",
            "appearance_reference": reference / "APPEARANCE.md",
            "ui_reference": reference / "UI.md",
            "testing_reference": reference / "TESTING.md",
        }
        print(json.dumps({name: str(path) for name, path in paths.items()}, ensure_ascii=False, indent=2))
        return 0
    if args.action != "list" and not args.target:
        parser.error("this action requires a plugin name or source path")
    try:
        workspace = PluginWorkspace.open(args.config, args.project)
        installations = PluginInstallations(workspace.catalog, workspace.cwd, workspace.settings)
        installations.validate = AppearanceContribution.validate
        result = asyncio.run(installations.manage(args.action, args.target))
    except Exception as error:  # noqa: BLE001 - commands return machine-readable errors, including bad configuration.
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
