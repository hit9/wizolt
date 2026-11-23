"""Build a standalone preset comparison from wizolt's real, offline renderers.

Run: uv run --no-sync python docs/tools/theme_preview.py
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from render import NOW, TERMINAL_THEME, WIDTH, Illustrations, powerline_paths
from rich.console import Console
from rich.terminal_theme import DEFAULT_TERMINAL_THEME, TerminalTheme
from rich.text import Text

from wizolt.base import LogBlock, LogEdge, LogLine, LogRole
from wizolt.config import Config
from wizolt.session import Session
from wizolt.ui.bars import DIVIDER_PRESETS, STATUS_PRESETS
from wizolt.ui.cli.appearance import DIFF_STYLE_SAMPLE
from wizolt.ui.render import Theme

CAPTIONS = {
    "dark": ("Terminal dark", "Terminal cyan with wheat-colored paths."),
    "light": ("Terminal light", "Cyan prompts and chestnut paths on pale gray."),
    "slate": ("Slate", "Dusty rose prompts with glacier-blue paths."),
    "forest": ("Forest", "Sage prompts and clay-colored paths."),
    "sand": ("Sand", "Sandstone gold with sea-glass paths."),
    "plum": ("Plum", "Mulberry pink paired with muted jade."),
    "paper": ("Paper", "Terracotta and blue ink on warm paper."),
    "gruvbox-dark": ("Gruvbox Dark", "Soft orange and faded blue with olive syntax."),
    "solarized-dark": ("Solarized Dark", "Ochre prompts and turquoise paths."),
    "dracula": ("Dracula", "Soft orchid prompts and ice-blue paths."),
    "papercolor-light": ("PaperColor Light", "Berry and deep blue with olive syntax."),
    "papercolor-dark": ("PaperColor Dark", "Antique gold and sky blue on charcoal."),
}


def html_text(text: Text, terminal: TerminalTheme) -> str:
    console = Console(width=WIDTH, record=True, file=io.StringIO(), color_system="truecolor")
    console.print(text, soft_wrap=True, end="")
    # SVG preserves terminal cell widths and fills whole rows, including Chinese text.
    # Plain inline HTML backgrounds leave gaps between browser text line boxes.
    template = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {terminal_width} {terminal_height}">
<style>{styles}\n.{unique_id}-matrix {{font-family:monospace;font-size:{char_height}px;line-height:{line_height}px}}</style>
<defs>{lines}</defs>{backgrounds}<g class="{unique_id}-matrix">{matrix}</g></svg>"""
    return powerline_paths(console.export_svg(code_format=template, theme=terminal))


def sample(figures: Illustrations, terminal: TerminalTheme) -> dict:
    message = figures.message("Fix the empty-input crash in parser.py", "user", 0)
    patch_text = "--- a/parser.py\n+++ b/parser.py\n" + DIFF_STYLE_SAMPLE
    edit = LogBlock.hierarchy(
        LogLine("Edit", "parser.py", LogRole.TOOL),
        [LogLine("", line, LogRole.DIFF, LogEdge.CONTINUE) for line in patch_text.splitlines()],
    )
    rows = [
        message,
        Text(""),
        figures.message("Check the tokenizer, then handle empty input.", "assistant"),
        figures.styled(
            figures.printer.log_segments(
                LogBlock([LogLine("Read", "parser.py, tests/test_parser.py", LogRole.TOOL, meta=" → tr.1–tr.2", syntax="tool-args")]), WIDTH
            )
        ),
        figures.styled([(Theme.fg("preview"), "  Checking how the parser handles an empty token…")]),
        figures.styled(figures.printer.log_segments(edit, WIDTH)),
        figures.styled(
            [
                (Theme.fg("success"), "  12 passed"),
                (Theme.fg("muted"), " · "),
                (Theme.fg("warning"), "1 skipped"),
            ]
        ),
        Text(""),
        figures.styled([("class:choice.selected", "  /theme  "), (Theme.fg("muted"), " Choose a color theme")]),
    ]
    joined = Text("\n").join(row[:-1] if row.plain.endswith("\n") else row for row in rows)
    bars = {name: html_text(figures.bar({"statusbar": {"format": "preset:" + name}}, width=WIDTH), terminal) for name in STATUS_PRESETS}
    dividers = {}
    for name in DIVIDER_PRESETS:
        line = figures.bar(
            {"divider": {"format": "preset:" + name}},
            kind="divider",
            width=WIDTH,
            values={"running": True, "label": "working (12s · 42 tok/s)", "queue.total": 0, "elapsed": 12.0},
        )
        dividers[name] = html_text(line, terminal)
    prompt = Text("> ", style=Theme.rich_color("accent")) + Text("Type a message...", style=Theme.rich_color("muted"))
    prompt.append(" " * (WIDTH - prompt.cell_len))
    prompt.stylize("on " + Theme.rich_color("user_bg"))
    padding = Text(" " * WIDTH, style="on " + Theme.rich_color("user_bg"))
    return {
        "body": html_text(joined, terminal),
        "bars": bars,
        "dividers": dividers,
        "input": html_text(Text("\n").join([padding, prompt, padding]), terminal),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "_build" / "previews" / "theme-preview.html")
    parser.add_argument("--fragment", action="store_true", help="Render just the comparison for the Sphinx page")
    args = parser.parse_args()
    for key in ("NO_COLOR", "PROMPT_TOOLKIT_COLOR_DEPTH"):
        os.environ.pop(key, None)
    os.environ["COLORTERM"] = "truecolor"
    payload = {}
    with (
        tempfile.TemporaryDirectory(prefix="wizolt-color-preview-") as folder,
        patch("time.monotonic", return_value=NOW),
        patch("shutil.get_terminal_size", return_value=os.terminal_size((WIDTH, 30))),
    ):
        session = Session(cwd=folder, config=Config(data_dir=folder))
        try:
            figures = Illustrations(session, Path(folder), [])
            figures.values.update(yolo=True, provider="zai", model="glm-5.3", reasoning="high")
            for base, palette in Theme.BUILTIN.items():
                name, note = CAPTIONS.get(base, (base, ""))
                light = palette.appearance == "light"
                bg, fg = ((245, 244, 240), (53, 58, 64)) if light else ((40, 44, 52), (207, 211, 218))
                ansi = (DEFAULT_TERMINAL_THEME if light else TERMINAL_THEME).ansi_colors
                terminal = TerminalTheme(bg, fg, [ansi[i] for i in range(8)], [ansi[i] for i in range(8, 16)])
                with patch("wizolt.utils.terminal.background", return_value=bg):
                    baseline = "light" if light else "dark"
                    Theme.configure(base, folder, {})
                    Theme.set_diff_style("classic")
                    rendered = sample(figures, terminal)
                config = f'[runtime]\ntheme = "{base}"\n\n[ui.diff]\nstyle = "classic"'
                payload[base] = {
                    "name": name,
                    "note": note,
                    "baseline": baseline,
                    "sample": rendered,
                    "bg": "#" + "".join(f"{part:02x}" for part in bg),
                    "config": config,
                }
        finally:
            session.close()
    widget = Path(__file__).with_name("theme-preview-widget.html").read_text(encoding="utf-8")
    html = widget.replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c"))
    if not args.fragment:
        static = ROOT / "docs" / "_static"
        css = "\n".join((static / name).read_text(encoding="utf-8") for name in ("custom.css", "theme-preview.css"))
        html = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Theme preview</title><style>{css} main {{max-width: 85rem; margin: auto; padding: 1rem;}}</style></head>"
            f"<body><main><h1>Theme preview</h1>{html}</main></body></html>"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not args.output.exists() or args.output.read_text(encoding="utf-8") != html:
        args.output.write_text(html, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
