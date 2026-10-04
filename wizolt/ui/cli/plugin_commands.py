"""Shell-facing plugin authoring and saved preferences; never guess a live wizolt process."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import wizolt
from wizolt.plugins.installation import PluginInstallations
from wizolt.plugins.interception import InterceptionOrder
from wizolt.plugins.presenters import PresenterChoices
from wizolt.plugins.workspace import PluginWorkspace
from wizolt.sdk import PluginError
from wizolt.ui.cli.plugin_appearance import AppearanceContribution


def order_main(argv: list[str]) -> int:
    """Interception order: one user-level list, applied by the next reload in an agent."""
    parser = argparse.ArgumentParser(
        prog="wizolt plugin order", description="Order plugin interceptors; the first runs outermost. An agent applies it with Plugin(action=reload)."
    )
    parser.add_argument("action", choices=("list", "move", "reset"))
    parser.add_argument("name", nargs="?", default="", help="Installed plugin to move")
    anchor = parser.add_mutually_exclusive_group()
    anchor.add_argument("--before", default="", help="Place it just before this plugin")
    anchor.add_argument("--after", default="", help="Place it just after this plugin")
    parser.add_argument("--config", default=PluginWorkspace.default_config(), help="Config file (default: the calling agent's, else the usual config)")
    args = parser.parse_args(argv)
    if args.action == "move" and not (args.name and (args.before or args.after)):
        parser.error("move needs a plugin name and one of --before or --after")
    if args.action != "move" and (args.name or args.before or args.after):
        parser.error(f"{args.action} takes no plugin name or anchor")
    try:
        workspace = PluginWorkspace.open(args.config, PluginWorkspace.default_project())
        order = InterceptionOrder(workspace.catalog.preferences)
        order.load()
        installed, _ = workspace.catalog.read()
        if args.action == "move":
            anchor_name = args.before or args.after
            for name in (args.name, anchor_name):
                if name not in installed:
                    raise PluginError(f"Unknown installed plugin: {name}")
            if args.name == anchor_name:
                raise PluginError("A plugin cannot move relative to itself")
            names = [name for name in order.ordered({*installed, *order.names}) if name != args.name]
            names.insert(names.index(anchor_name) + bool(args.after), args.name)
            order.save(names)
        elif args.action == "reset":
            order.save([])
        effective = [name for name in order.ordered({*installed, *order.names}) if name in installed]
    except Exception as error:  # noqa: BLE001 - commands return machine-readable errors, including bad configuration.
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    result = {
        "order": effective,
        "saved": list(order.names),
        "note": "First is outermost. Unlisted plugins follow by name. Use Plugin(action=reload) to apply it in an existing agent.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def presenter_main(argv: list[str]) -> int:
    """Presenter choices: which plugin renders each site, applied by the next reload."""
    from wizolt.sdk.presentation import SITES

    parser = argparse.ArgumentParser(
        prog="wizolt plugin presenter",
        description="Choose which plugin renders a presentation site; an agent applies it with Plugin(action=reload).",
    )
    parser.add_argument("action", choices=("list", "choose", "reset"))
    parser.add_argument("site", nargs="?", default="", help="Presentation site: " + ", ".join(sorted(SITES)))
    parser.add_argument("name", nargs="?", default="", help="Installed plugin to choose for the site")
    parser.add_argument("--config", default=PluginWorkspace.default_config(), help="Config file (default: the calling agent's, else the usual config)")
    args = parser.parse_args(argv)
    if args.action == "choose" and not (args.site and args.name):
        parser.error("choose needs a site and a plugin name")
    if args.action == "list" and (args.site or args.name):
        parser.error("list takes no site or plugin name")
    if args.action == "reset" and args.name:
        parser.error("reset takes at most a site, not a plugin name")
    if args.site and args.site not in SITES:
        parser.error(f"unknown site {args.site!r}; choose {', '.join(sorted(SITES))}")
    try:
        workspace = PluginWorkspace.open(args.config, PluginWorkspace.default_project())
        choices = PresenterChoices(workspace.catalog.preferences)
        choices.load()
        records, _ = workspace.catalog.read()
        # Saved records cannot say which sites a plugin renders; ask the plugin. ``list`` asks
        # every enabled one, ``choose`` only its candidate, and ``reset`` asks none.
        registered = asyncio.run(_registrations(workspace, None if args.action == "list" else args.name or None)) if args.action != "reset" else {}
        if args.name and args.name not in records:
            raise PluginError(f"Unknown installed plugin: {args.name}")
        if args.name and not records[args.name].enabled:
            raise PluginError(f"{args.name} is disabled; enable it before choosing its presenter")
        if args.action == "choose":
            if args.site not in registered or args.name not in registered[args.site]:
                raise PluginError(f"{args.name} does not register the {args.site} presentation site")
            sites = {**choices.sites, args.site: args.name}
        elif args.action == "reset":
            sites = {site: name for site, name in choices.sites.items() if args.site and site != args.site}
        else:
            sites = dict(choices.sites)
        if args.action != "list":
            choices.save(sites)
    except Exception as error:  # noqa: BLE001 - commands return machine-readable errors, including bad configuration.
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    result = {
        "sites": {site: {"choice": choices.sites.get(site, ""), "registered": sorted(registered.get(site, ()))} for site in sorted(SITES)},
        "saved": dict(choices.sites),
        "note": "One presenter renders each site; overlapping matches keep the builtin rendering until you choose. Use Plugin(action=reload) to apply it in an existing agent.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


async def _registrations(workspace: PluginWorkspace, only: str | None) -> dict[str, list[str]]:
    """Launch each enabled installed plugin once and list the sites it registers."""
    from wizolt.plugins.loading import PluginSource
    from wizolt.plugins.process import PluginProcess
    from wizolt.plugins.protocol import Capabilities

    records, _ = workspace.catalog.read()
    registered: dict[str, list[str]] = {}
    for name, item in records.items():
        if not item.enabled or (only is not None and name != only):
            continue
        source = PluginSource.read(item.path)
        worker, description = await PluginProcess.start(source, python=item.python, cwd=workspace.cwd, config=workspace.settings.read(name))
        try:
            capabilities = Capabilities.decode(name, description)
        finally:
            await worker.close()
        for site in capabilities.presenters:
            registered.setdefault(site, []).append(name)
    return registered


def main(argv: list[str]) -> int:
    if argv[:1] in (["test"], ["validate"]):
        from wizolt.ui.cli.plugin_testing import main as test_main

        return test_main(argv)
    if argv[:1] == ["order"]:
        return order_main(argv[1:])
    if argv[:1] == ["presenter"]:
        return presenter_main(argv[1:])
    parser = argparse.ArgumentParser(prog="wizolt plugin", description="Manage saved plugin preferences. An agent applies them with Plugin(action=reload).")
    parser.add_argument("action", choices=("paths", "list", "inspect", "enable", "disable", "order", "presenter", "test", "validate"))
    parser.add_argument("target", nargs="?", default="", help="Installed name, .py file, or package directory; enable prepares declared dependencies")
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
            "interception_reference": reference / "INTERCEPTION.md",
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
