"""Offline authoring commands: no agent, provider, installation catalog or live UI required."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
from dataclasses import asdict
from pathlib import Path

from wizolt.config import Config
from wizolt.plugins.protocol import Capabilities
from wizolt.plugins.testing import PluginTrial, Stimulus
from wizolt.plugins.workspace import PluginWorkspace
from wizolt.sdk import Context, Value, Viewport
from wizolt.ui.bars import FIELDS, BarLayout
from wizolt.ui.cli.plugin_appearance import AppearanceContribution
from wizolt.ui.cli.plugin_preview import PreviewExporter
from wizolt.ui.cli.plugin_scenarios import ScriptedDialogs
from wizolt.ui.render import Theme


def read_input(path: Path, limit: int) -> bytes:
    """Read a trial fixture. Unlike plugin source admission, a pipe or symlink is fine here:
    `--facts <(...)` and `/dev/stdin` are ordinary ways to hand a command a small input."""
    with open(path, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} exceeds {limit // 1024} KiB")
    return data


def preset_previews(exporter: PreviewExporter, name: str, presets: dict[str, dict[str, str]], frame: dict, index: int) -> list[dict]:
    """Render each contributed bar format with this frame's plugin fields.

    Host fields take representative preview values; the plugin's own fields are the sampled
    ones, so a format that reacts to them shows what the user will see.
    """
    context = frame["context"]
    running = context["status"] == "running"
    values: dict[str, Value] = dict.fromkeys(FIELDS, 0)
    values.update(
        {
            "provider": "preview",
            "model": context["model"],
            "reasoning": "medium",
            "agent.name": context["agent_name"],
            "agent.id": context["agent_id"],
            "agent.state": context["status"],
            "agents.count": 1,
            "context.percent": round(context["context_percent"]),
            "mcp.label": "mcp 0",
            "plugins.count": 1,
            "running": running,
            "elapsed": context["elapsed"],
            "activity": "working" if running else "",
            "spinner": "●" if running else "",
            "label": "working" if running else "",
            "rate": "",
        }
    )
    values.update({f"plugins.{name}.{key}": value for key, value in frame["fields"].items()})
    layout, previews = BarLayout(), []
    for kind, choices in presets.items():
        layout.presets[kind].update(choices)
        for choice in choices:
            problems = layout.configure({kind: "preset:" + choice}, Theme.bar_styles)
            fragments = [] if problems else layout.render(kind, values, context["columns"], Theme.bar_styles)
            # render() falls back to the default bar on a runtime error; a preview must not.
            if problems or layout.errors:
                raise ValueError("; ".join(problems or layout.errors))
            previews.append({**exporter.draw(fragments, f"{kind} {choice}", context["columns"], index), "preset": choice})
    return previews


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wizolt plugin", description="Validate or try trusted plugin Python without activating it.")
    parser.add_argument("action", choices=("validate", "test"))
    parser.add_argument("path", help="Installed name, .py file, or package directory")
    parser.add_argument("--config", default=PluginWorkspace.default_config(), help="Config file (default: the calling agent's, else the usual config)")
    parser.add_argument("--project", default=PluginWorkspace.default_project(), help="Project (default: the calling agent's, else the current directory)")
    parser.add_argument("--width", type=int, default=80, help="Terminal columns, 10–240")
    parser.add_argument("--height", type=int, default=24, help="Terminal rows, 8–100; controls the live component height budget")
    parser.add_argument("--theme", default="", help="Built-in or custom theme; defaults to the selected config theme")
    parser.add_argument("--status", default="idle", help="Agent state supplied to callbacks")
    parser.add_argument("--context-percent", type=float, default=21)
    parser.add_argument("--facts", type=Path, help="JSON Context overrides for offline fixtures (usage, window, etc.)")
    parser.add_argument("--times", type=float, nargs="+", default=[0], help="Animation times in seconds; at most 12 frames")
    parser.add_argument("--timeout", type=float, default=5, help="Deadline for each worker call, in seconds (0–60)")
    parser.add_argument("--event", action="append", default=[], help="Explicit lifecycle event; repeat to send several")
    parser.add_argument("--call", default="", help="Explicit command:NAME or tool:NAME after events")
    parser.add_argument("--arguments", default="{}", help="JSON arguments for --call")
    parser.add_argument("--interactions", type=Path, help="JSON scripted view expectations and replies; settings writes use a temporary config")
    parser.add_argument("--output", type=Path, help="Parent directory for a fresh preview bundle (default: system temporary directory)")
    parser.add_argument("--font", default="", help="PNG font file; choose one covering your plugin's characters")
    args = parser.parse_args(argv)
    if not 10 <= args.width <= 240 or not 0 < args.timeout <= 60:
        parser.error("width must be 10–240 and timeout must be greater than 0 and at most 60")
    if not 8 <= args.height <= 100:
        parser.error("height must be 8–100")
    if not 1 <= len(args.times) <= 12 or any(not math.isfinite(value) or value < 0 for value in args.times):
        parser.error("provide 1–12 finite, nonnegative animation times")
    if not math.isfinite(args.context_percent) or not 0 <= args.context_percent <= 100:
        parser.error("context-percent must be between 0 and 100")
    try:
        arguments = json.loads(args.arguments)
    except ValueError as error:
        parser.error(str(error))
    if not isinstance(arguments, dict):
        parser.error("arguments must be a JSON object")
    stimuli = [Stimulus("event", name) for name in args.event]
    if args.call:
        kind, separator, name = args.call.partition(":")
        if not separator or kind not in ("command", "tool") or not name:
            parser.error("call must be command:NAME or tool:NAME")
        stimuli.append(Stimulus(kind, name, arguments))
    if args.action == "validate" and (stimuli or args.interactions):
        parser.error("use test to execute events or actions")
    try:
        workspace = PluginWorkspace.open(args.config, args.project)
        dialogs = ScriptedDialogs(json.loads(read_input(args.interactions, 256 * 1024)) if args.interactions else [], args.width, args.height, args.timeout)
        Theme.project_plugins({})
        requested_theme = args.theme or Config.table(workspace.data, "runtime").get("theme", "dark")
        problems = Theme.configure(
            "dark" if requested_theme.startswith("plugins.") else requested_theme,
            str(Path(workspace.data_dir) / "themes"),
            Config.table(workspace.data, "ui").get("themes"),
        )
        if Theme.canonical(requested_theme) is None and not requested_theme.startswith("plugins."):
            raise ValueError(f"Unknown theme: {requested_theme}")
        installed, catalog_problems = workspace.installed(args.path)
        python = installed.python if installed else ""
        if installed:
            args.path = installed.path
        context = Context(
            "preview", "main", workspace.cwd, args.status, args.context_percent, 0, "preview-model", 0, args.width, viewport=Viewport(args.width, args.height)
        )
        if args.facts:
            facts = json.loads(read_input(args.facts, 256 * 1024))
            if not isinstance(facts, dict):
                raise ValueError("facts must be a JSON object")
            # A fixture supplies agent facts, not host IO settings. Viewport and working
            # directory remain the explicit --width / --project values used by the preview.
            context = Context.decode(
                {**asdict(context), **facts, "cwd": workspace.cwd, "columns": args.width, "viewport": asdict(context.viewport), "layout": None}
            )
    except Exception as error:  # noqa: BLE001 - configuration failures are structured feedback too.
        print(json.dumps({"status": "failed", "stage": "configuration", "error": str(error)}, ensure_ascii=False))
        return 1
    trial = PluginTrial(context, timeout=args.timeout, python=python, settings=workspace.settings)
    trial.validate = AppearanceContribution.validate
    trial.host_service = dialogs.call if args.interactions else None
    report = asdict(
        asyncio.run(
            trial.run(
                args.path,
                validate=args.action == "validate",
                times=tuple(args.times),
                stimuli=tuple(stimuli),
            )
        )
    )
    report["interactions"] = dialogs.trace
    if report["status"] == "passed":
        try:
            dialogs.finish()
        except ValueError as error:
            report.update(status="failed", stage="interactions", error=str(error))
    contribution = AppearanceContribution({}, {})
    if report["status"] == "passed":
        contribution = AppearanceContribution.compile(Capabilities.decode(report["name"], report["capabilities"]))
        Theme.project_plugins(contribution.themes)
        if Theme.canonical(requested_theme) is None:
            report.update(status="failed", stage="configuration", error=f"Unknown theme: {requested_theme}")
            report["frames"] = []
        else:
            Theme.set_mode(Theme.resolve(requested_theme))
    report["previews"] = []
    report["theme"] = Theme.name()
    report["warnings"] = [*problems, *catalog_problems]
    if report["frames"] or dialogs.trace:
        try:
            if args.output:
                args.output.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="wizolt-plugin-", dir=args.output)).resolve()
            exporter = PreviewExporter(directory, font=args.font, height=args.height)
            for index, item in enumerate(dialogs.trace):
                if "fragments" in item:
                    report["previews"].append(exporter.draw(item["fragments"], "interaction", args.width, index))
            for index, frame in enumerate(report["frames"]):
                report["previews"].extend(exporter.export(frame, index))
                report["previews"].extend(preset_previews(exporter, report["name"], contribution.presets, frame, index))
            report["report"] = str(directory / "report.json")
            (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except (ValueError, OSError) as error:
            report.update(status="failed", stage="export", error=str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1
