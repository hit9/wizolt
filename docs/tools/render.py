"""Rebuild documentation SVGs from wizolt's renderers and fixed, offline examples.

Run from the repository: uv run python docs/tools/render.py --help
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
import tomllib
from html import escape
from pathlib import Path
from unittest.mock import patch

# Resolve the checkout rather than depending on the caller's working directory or installation.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.renderer import print_formatted_text
from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from wizolt.agent.engine import Agent
from wizolt.base import LogBlock, LogEdge, LogLine, LogRole
from wizolt.config import Config
from wizolt.session import QueuedInput, Session
from wizolt.ui.bars import BarLayout
from wizolt.ui.cli.appearance import DIFF_STYLE_SAMPLE, AppearancePicker
from wizolt.ui.cli.loop import CommandLoop
from wizolt.ui.render import MessageBlock, Theme, UiPrinter
from wizolt.ui.tui import InputMode, TuiApp

DOCS = ROOT / "docs"
WIDTH = 76
NOW = 20.0
SAMPLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">
<title>Rendered UI sample</title>
<style>
.{unique_id}-matrix {{font-family:monospace;font-size:{char_height}px;line-height:{line_height}px}}
{styles}
</style>
<defs><clipPath id="{unique_id}-clip-terminal">
<rect width="{terminal_width}" height="{terminal_height}"/>
</clipPath>{lines}</defs>
<rect width="{width}" height="{height}" rx="8" fill="#282c34"/>
<text x="18" y="26" fill="#aebac9" font-family="sans-serif" font-size="12" letter-spacing="2">RENDERED UI SAMPLE</text>
<g transform="translate({terminal_x}, {terminal_y})" clip-path="url(#{unique_id}-clip-terminal)">
{backgrounds}<g class="{unique_id}-matrix">{matrix}</g></g>
</svg>"""
# The virtual terminal's own colors; wizolt themes leave its background alone.
TERMINAL_THEME = TerminalTheme(
    (40, 44, 52),
    (171, 178, 191),
    [(40, 44, 52), (224, 108, 117), (152, 195, 121), (229, 192, 123), (97, 175, 239), (198, 120, 221), (86, 182, 194), (171, 178, 191)],
    [(92, 99, 112), (224, 108, 117), (152, 195, 121), (229, 192, 123), (97, 175, 239), (198, 120, 221), (86, 182, 194), (255, 255, 255)],
)


def example(name: str) -> dict:
    """Read a marked TOML example, so its picture cannot quietly diverge from its instructions."""
    source = (DOCS / "appearance-reference.md").read_text(encoding="utf-8")
    match = re.search(r"<!-- figure: " + re.escape(name) + r" -->\s*```toml\n(.*?)\n```", source, re.DOTALL)
    if match is None:
        raise ValueError(f"Missing TOML example: {name}")
    return tomllib.loads(match[1])


class Illustrations:
    def __init__(self, session: Session, output: Path, selected: list[str]):
        self.session, self.output, self.selected = session, output, selected
        self.loop = CommandLoop(Agent(session, output_fn=lambda _: None), input_fn=lambda _: "", output_fn=lambda _: None)
        # Display the usual path, but never read or write a personal config.
        session.config.path = os.path.expanduser("~/.wizolt/config.toml")
        self.printer = UiPrinter()
        self.printer.color = True
        self.values = self.loop.presentation.status_bar.values()
        self.values.update(
            provider="anthropic",
            model="claude-sonnet",
            reasoning="medium",
            running=False,
            elapsed=12.0,
            activity="working",
            rate="42 tok/s",
            spinner="● ",
            label="working (12s · 42 tok/s)",
            **{"context.percent": 37, "cache.percent": 88, "mcp.count": 2, "mcp.label": "2", "skills.count": 3, "queue.total": 0},
        )

    def styled(self, fragments, width: int = WIDTH) -> Text:
        buffer = io.StringIO()
        terminal = Vt100_Output(buffer, lambda: Size(rows=30, columns=width), default_color_depth=ColorDepth.DEPTH_24_BIT)
        print_formatted_text(terminal, fragments, self.loop.view.style())
        text = Text.from_ansi(buffer.getvalue().replace("\r", ""))
        # Drop modal padding, keeping colors and the spaces inside an actual background band.
        return text[: len(text.plain.rstrip())]

    def label(self, text: str) -> Text:
        return Text(text, style=Theme.rich_color("muted"))

    def message(self, text: str, role: str = "", indent: int = 1) -> Text:
        return Text.from_ansi(MessageBlock(self.printer, text, role, indent, False).ansi(WIDTH))

    def log(self, label: str, detail: str, output: str = "") -> Text:
        children = [LogLine(output, role=LogRole.OUTPUT, edge=LogEdge.END)] if output else []
        block = LogBlock.hierarchy(LogLine(label, detail, LogRole.TOOL), children)
        return self.styled(self.printer.log_segments(block, WIDTH))

    def bar(self, settings: dict, *, kind: str = "statusbar", width: int = 72, values: dict | None = None) -> Text:
        layout = BarLayout()
        if problems := layout.load(settings, Theme.bar_styles):
            raise ValueError("; ".join(problems))
        ramp = tuple(reversed(Theme.ramp("divider_glow", "divider_rule", 16)))
        return self.styled(layout.render(kind, {**self.values, **(values or {})}, width, Theme.bar_styles, ramp=ramp), width)

    def save(self, name: str, rows: list[Text], width: int = WIDTH) -> None:
        if name not in self.selected:
            return
        console = Console(width=width, record=True, file=io.StringIO(), color_system="truecolor", theme=Theme.rich_theme())
        for row in rows:
            console.print(row, soft_wrap=True)
        # Stable identifiers make a second run byte-identical, rather than churning SVG ids.
        options = {} if name in {"appearance-picker", "appearance-input", "appearance-input-editor"} else {"code_format": SAMPLE_SVG}
        console.save_svg(str(self.output / f"{name}.svg"), title="wizolt", theme=TERMINAL_THEME, unique_id=name, **options)

    def appearance_picker(self) -> None:
        picker = AppearancePicker(self.loop, NOW - 12)
        try:
            self.save("appearance-picker", [self.styled(picker.fragments())])
            picker.handle_key("h")
            picker.handle_key("j")
            self.save("appearance-input", [self.styled(picker.fragments())])
            picker.handle_key("e")
            self.save("appearance-input-editor", [self.styled(picker.fragments())])
        finally:
            picker.restore()

    def appearance_colors(self) -> None:
        rows = []
        for name in ("gruvbox-dark", "one-dark", "desert", "tokyonight", "catppuccin-dark", "kanagawa"):
            Theme.set_mode(name)
            rows.extend(
                [
                    Text(name, style=Theme.rich_color("accent") + " bold"),
                    self.styled(UiPrinter.syntax_segments('def greet(name):\n    return f"Hello, {name}!"', "python", Theme.fg("text"))),
                    Text(""),
                ]
            )
        self.save("appearance-colors", rows[:-1], width=44)

    def appearance_diff(self) -> None:
        self.save("appearance-diff", [self.styled(self.printer.diff_segments(DIFF_STYLE_SAMPLE, row_width=72))])

    def appearance_statusbars(self) -> None:
        rows = []
        for name in ("default", "minimal", "split", "blocks", "vim"):
            rows.extend([self.label(name), self.bar({"statusbar": {"format": "preset:" + name}}), Text("")])
        self.save("appearance-statusbars", rows[:-1])

    def appearance_dividers(self) -> None:
        rows = []
        for name in ("comet", "frame", "rail"):
            rows.extend([self.label(name), self.bar({"divider": {"format": "preset:" + name}}, kind="divider", values={"running": True}), Text("")])
        self.save("appearance-dividers", rows[:-1])

    def custom_theme(self) -> None:
        data = example("custom-theme")
        path = Path(self.session.config.data_dir) / "themes"
        path.mkdir()
        # This is a fixture in the temporary data directory, never the user's themes folder.
        source = (DOCS / "appearance-reference.md").read_text(encoding="utf-8")
        match = re.search(r"<!-- figure: custom-theme -->\s*```toml\n(.*?)\n```", source, re.DOTALL)
        assert match is not None
        (path / "my-theme.toml").write_text(match[1], encoding="utf-8")
        if problems := Theme.load_custom(str(path)):
            raise ValueError("; ".join(problems))
        rows = []
        for name in (data["base"], "my-theme"):
            Theme.set_mode(name)
            rows.extend([self.label(name), self.message("Tighten the tokenizer", "user", 0), self.log("Edit", 'path="parser.py"'), Text("")])
        self.save("appearance-custom-theme", rows[:-1])

    def custom_statusbar(self) -> None:
        rows = []
        for label, name in (
            ("1. Just the model", "statusbar-model"),
            ("2. Add context usage", "statusbar-context"),
            ("3. Put context on the right", "statusbar-aligned"),
        ):
            rows.extend([self.label(label), self.bar(example(name)["ui"]), Text("")])
        self.save("appearance-custom-statusbar", rows[:-1])

    def custom_width(self) -> None:
        settings = example("statusbar-optional")["ui"]
        rows = [self.label("Wide pane · 72 columns"), self.bar(settings), Text(""), self.label("Small pane · 24 columns"), self.bar(settings, width=24)]
        self.save("appearance-custom-width", rows)

    def custom_divider(self) -> None:
        settings = example("divider-centered")["ui"]
        rows = []
        for label, running in (("Idle", False), ("Working", True)):
            rows.extend([self.label(label), self.bar(settings, kind="divider", values={"running": running}), Text("")])
        self.save("appearance-custom-divider", rows[:-1])

    def custom_sweep(self) -> None:
        settings = {"divider": {**example("sweep-normalized")["ui"]["divider"], "format": "[divider_rule]{fill:─}[/]"}}
        rows = []
        for seconds in (0.0, 1.5, 3.0, 4.5, 6.0):
            rows.extend([self.label(f"{seconds:.1f}s"), self.bar(settings, kind="divider", values={"running": True, "elapsed": seconds}), Text("")])
        self.save("appearance-custom-sweep", rows[:-1])

    def first_turn(self) -> None:
        diff = "@@ -10,3 +10,4 @@\n def tokenize(text):\n-    first = text[0]\n+    if not text:\n+        return []\n     return text.split()\n"
        app = TuiApp()
        app.input_mode = InputMode.APPROVAL
        app.set_approval_form([("Approve", ""), ("Refuse", "n")])
        self.save(
            "getting-started-turn",
            [
                self.message("Fix the tokenizer crash on empty input", "user", 0),
                self.log("Read", "parser.py"),
                self.log("Edit", 'path="parser.py"'),
                self.styled(self.printer.diff_segments(diff, row_width=72)),
                self.styled(app.approval_form_fragments()),
                Text(""),
                self.log("Bash", "uv run pytest -q", "41 passed in 2.10s"),
                Text(""),
                self.message("Empty input now returns an empty list. The tests pass."),
                self.styled([(Theme.fg("rule"), "─" * 72 + "\n"), ("class:prompt", "> "), (Theme.fg("text"), "▏")]),
            ],
        )

    def followups(self) -> None:
        app = self.loop.presentation.tui = TuiApp()
        app.set_running("working")
        self.loop.presentation.status_bar.started_at = NOW - 12
        self.session.pending_user_inputs.extend(
            [
                QueuedInput("Also update the tests"),
                QueuedInput("Then review the README", next_turn=True),
            ]
        )
        try:
            _, waiting = self.loop.view.followup_fragments()
            self.save(
                "usage-followups",
                [
                    self.message("Refactor the parser", "user", 0),
                    self.log("Read", "parser.py"),
                    Text(""),
                    self.styled(waiting),
                    Text(""),
                    self.styled(app.status_fragments() + [(Theme.fg("text"), "▏")]),
                ],
            )
        finally:
            self.session.pending_user_inputs.clear()
            self.loop.presentation.tui = None

    def diagram(self, name: str, title: str, steps: list[tuple[str, str]], note: str) -> None:
        """Quiet, transparent flows using the documentation's typography and palette."""
        if name not in self.selected:
            return
        height = 50 + len(steps) * 102
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="760" height="{height}" viewBox="0 0 760 {height}" role="img">',
            f"<title>{escape(title)}</title>",
            "<style>text{font-family:Georgia,serif;fill:#c9ccd1}.detail{font-size:18px;fill:#a2a9b3}.heading{font-size:23px;fill:#e8eaed}.number{font-family:monospace;font-size:14px;fill:#8b929c}</style>",
        ]
        for index, (label, detail) in enumerate(steps):
            y = 12 + index * 102
            parts.extend(
                [
                    f'<text x="16" y="{y + 25}" class="number">{index + 1:02}</text>',
                    f'<text x="58" y="{y + 26}" class="heading">{escape(label)}</text>',
                    f'<text x="58" y="{y + 56}" class="detail">{escape(detail)}</text>',
                ]
            )
            if index < len(steps) - 1:
                parts.append(f'<path d="M24 {y + 42} v53 m-4 -4 l4 4 4 -4" fill="none" stroke="#59616c" stroke-width="1"/>')
        parts.append(f'<path d="M58 {height - 45} H728" stroke="#2c3138"/>')
        parts.append(f'<text x="58" y="{height - 16}" class="detail" font-style="italic">{escape(note)}</text></svg>')
        (self.output / f"{name}.svg").write_text("\n".join(parts) + "\n", encoding="utf-8")

    def compaction(self) -> None:
        self.diagram(
            "context-compaction",
            "Make room without starting over",
            [
                ("Before: older conversation + recent messages", "A long conversation fills the model's window."),
                ("After: short summary + recent messages", "About eight recent messages stay unchanged."),
            ],
            "Earlier conversation remains available in history files.",
        )

    def caching(self) -> None:
        self.diagram(
            "context-cache",
            "Reuse the unchanged beginning",
            [
                ("Previous request", "Shared instructions + conversation so far"),
                ("Next request", "Same beginning + your new message and recent results"),
            ],
            "The provider can reuse the shared prefix; new input adds work.",
        )

    def skills(self) -> None:
        self.diagram(
            "skills-workflow",
            "Write once, use when needed",
            [
                ("Install a skill", "Put instructions in .wizolt/skills/release-notes/SKILL.md"),
                ("Invoke /release-notes", "Or let the agent load it when it fits the task."),
                ("Follow the instructions", "Read commits → group changes → draft release notes"),
            ],
            "Only the short description is shown until the skill is used.",
        )

    def hooks(self) -> None:
        self.diagram(
            "hooks-workflow",
            "Format after a successful edit",
            [
                ("Review and approve", "The agent proposes a change to parser.py."),
                ("Apply the edit, then run your hook", "PostToolUse runs: ruff format --quiet ."),
                ("Continue the task", "The agent waits for the formatter before continuing."),
            ],
            "A hook is your command, triggered by an event you choose.",
        )

    def worker(self) -> None:
        self.diagram(
            "worker-handoff",
            "A focused second opinion",
            [
                ("Main conversation → approved order", "Plan the refactor; ask a worker to review parser.py."),
                ("Worker → report", "Read the files, check the tests, return findings."),
                ("Main conversation continues", "Use the report to continue your original task."),
            ],
            "Both share the workspace; each has its own conversation.",
        )


RECIPES = {
    "appearance-picker": "appearance_picker",
    "appearance-input": "appearance_picker",
    "appearance-input-editor": "appearance_picker",
    "appearance-colors": "appearance_colors",
    "appearance-diff": "appearance_diff",
    "appearance-statusbars": "appearance_statusbars",
    "appearance-dividers": "appearance_dividers",
    "appearance-custom-theme": "custom_theme",
    "appearance-custom-statusbar": "custom_statusbar",
    "appearance-custom-width": "custom_width",
    "appearance-custom-divider": "custom_divider",
    "appearance-custom-sweep": "custom_sweep",
    "getting-started-turn": "first_turn",
    "usage-followups": "followups",
    "context-compaction": "compaction",
    "context-cache": "caching",
    "worker-handoff": "worker",
    "skills-workflow": "skills",
    "hooks-workflow": "hooks",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list available figure names")
    parser.add_argument("--figure", action="append", choices=RECIPES, help="draw selected figures; repeat to select more (default: all)")
    parser.add_argument("--output-dir", type=Path, default=DOCS / "_static", help="destination for SVG files (default: docs/_static)")
    parser.add_argument("--theme", choices=sorted(name for name, palette in Theme.BUILTIN.items() if palette.appearance == "dark"), default="gruvbox-dark")
    args = parser.parse_args()
    if args.list:
        print("\n".join(RECIPES))
        return
    # Only this child process changes its environment. The developer's terminal is untouched.
    os.environ.pop("NO_COLOR", None)
    os.environ.pop("PROMPT_TOOLKIT_COLOR_DEPTH", None)
    os.environ["COLORTERM"] = "truecolor"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="wizolt-docs-") as folder,
        patch("shutil.get_terminal_size", return_value=os.terminal_size((WIDTH, 30))),
        patch("time.monotonic", return_value=NOW),
    ):
        session = Session(cwd=folder, config=Config(data_dir=folder))
        try:
            selected = args.figure or list(RECIPES)
            figures = Illustrations(session, args.output_dir, selected)
            recipes = dict.fromkeys(RECIPES[name] for name in selected)
            with patch("wizolt.ui.cli.appearance.picker_height", return_value=20):
                for recipe in recipes:
                    Theme.set_mode(args.theme)
                    Theme.set_diff_style("auto")
                    session.settings.theme = args.theme
                    getattr(figures, recipe)()
        finally:
            session.close()
    print(f"Rendered {len(selected)} figures to {args.output_dir}")


if __name__ == "__main__":
    main()
