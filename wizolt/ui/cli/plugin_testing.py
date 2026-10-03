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
from wizolt.plugins.testing import PluginTrial, Stimulus
from wizolt.plugins.workspace import PluginWorkspace
from wizolt.sdk import Context
from wizolt.ui.cli.plugin_preview import PreviewExporter
from wizolt.ui.render import Theme


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wizolt plugin", description="Validate or try trusted plugin Python without activating it.")
    parser.add_argument("action", choices=("validate", "test"))
    parser.add_argument("path", help="Plugin .py source")
    parser.add_argument("--config", default=None, help="Config file for installed plugin names")
    parser.add_argument("--project", default=str(Path.cwd()), help="Project directory for installed plugin names")
    parser.add_argument("--width", type=int, default=80, help="Terminal columns, 10–240")
    parser.add_argument("--height", type=int, default=24, help="Terminal rows, 8–100; controls the live component height budget")
    parser.add_argument("--theme", default="", help="Built-in or custom theme; defaults to the selected config theme")
    parser.add_argument("--status", default="idle", help="Agent state supplied to callbacks")
    parser.add_argument("--context-percent", type=float, default=21)
    parser.add_argument("--times", type=float, nargs="+", default=[0], help="Animation times in seconds; at most 12 frames")
    parser.add_argument("--timeout", type=float, default=5, help="Deadline for each worker call, in seconds (0–60)")
    parser.add_argument("--event", action="append", default=[], help="Explicit lifecycle event; repeat to send several")
    parser.add_argument("--call", default="", help="Explicit command:NAME or tool:NAME after events")
    parser.add_argument("--arguments", default="{}", help="JSON arguments for --call")
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
    if args.action == "validate" and stimuli:
        parser.error("use test to execute events or actions")
    try:
        workspace = PluginWorkspace.open(args.config, args.project)
        requested_theme = args.theme or Config.table(workspace.data, "runtime").get("theme", "dark")
        problems = Theme.configure(requested_theme, str(Path(workspace.data_dir) / "themes"), Config.table(workspace.data, "ui").get("themes"))
        if Theme.canonical(requested_theme) is None:
            raise ValueError(f"Unknown theme: {requested_theme}")
        installed, catalog_problems = workspace.installed(args.path)
        python = installed.python if installed else ""
        if installed:
            args.path = installed.path
    except Exception as error:  # noqa: BLE001 - configuration failures are structured feedback too.
        print(json.dumps({"status": "failed", "stage": "configuration", "error": str(error)}, ensure_ascii=False))
        return 1
    context = Context("preview", "main", workspace.cwd, args.status, args.context_percent, 0, "preview-model", 0, args.width)
    report = asdict(
        asyncio.run(
            PluginTrial(context, timeout=args.timeout, python=python).run(
                args.path,
                validate=args.action == "validate",
                times=tuple(args.times),
                stimuli=tuple(stimuli),
            )
        )
    )
    report["previews"] = []
    report["theme"] = Theme.name()
    report["warnings"] = [*problems, *catalog_problems]
    if report["frames"]:
        try:
            if args.output:
                args.output.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="wizolt-plugin-", dir=args.output)).resolve()
            exporter = PreviewExporter(directory, font=args.font, height=args.height)
            for index, frame in enumerate(report["frames"]):
                report["previews"].extend(exporter.export(frame, index))
            report["report"] = str(directory / "report.json")
            (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except (ValueError, OSError) as error:
            report.update(status="failed", stage="export", error=str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1
