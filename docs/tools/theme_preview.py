"""Build a standalone preset comparison from wizolt's real, offline renderers.

Run: uv run --no-sync python docs/tools/theme_preview.py
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from render import NOW, TERMINAL_THEME, WIDTH, Illustrations
from rich.color import Color
from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from wizolt.config import Config
from wizolt.session import Session
from wizolt.ui.cli.appearance import DIFF_STYLE_SAMPLE
from wizolt.ui.render import Theme, UiPrinter

CANDIDATES = [
    {"id": "reference", "name": "00 · Default dark", "base": "dark", "note": "Unchanged reference palette."},
    {"id": "slate", "name": "01 · Slate", "base": "slate", "note": "Blue accents with amber messages, mint tools and lavender details."},
    {"id": "forest", "name": "02 · Forest", "base": "forest", "note": "Leaf green accents with wheat messages, teal code and warm details."},
    {"id": "sand", "name": "03 · Sand", "base": "sand", "note": "Amber accents with sage tools, teal code and rose details."},
    {"id": "plum", "name": "04 · Plum", "base": "plum", "note": "Lavender accents with rose messages, cyan tools and gold warnings."},
    {"id": "paper", "name": "05 · Paper", "base": "paper", "note": "Ink blue with warm brown messages, teal tools and purple details."},
    {"id": "gruvbox-dark", "name": "06 · Gruvbox Dark", "base": "gruvbox-dark", "note": "Retro orange, olive green and warm cream."},
    {"id": "solarized-dark", "name": "07 · Solarized Dark", "base": "solarized-dark", "note": "Cyan and ochre on a blue-green terminal."},
    {"id": "dracula", "name": "08 · Dracula", "base": "dracula", "note": "Bright pink, purple, cyan and green."},
    {"id": "papercolor-light", "name": "09 · PaperColor Light", "base": "papercolor-light", "note": "White paper with blue, pink and olive syntax colors."},
    {"id": "papercolor-dark", "name": "10 · PaperColor Dark", "base": "papercolor-dark", "note": "Charcoal with lime, gold and blue syntax colors."},
]
STATUSBARS = ("lualine", "powerline", "split", "vim", "default", "minimal")
DIVIDERS = ("comet", "capsule", "rail")


def html_text(text: Text, terminal: TerminalTheme) -> str:
    console = Console(width=WIDTH, record=True, file=io.StringIO(), color_system="truecolor")
    console.print(text, soft_wrap=True, end="")
    # SVG preserves terminal cell widths and fills whole rows, including Chinese text.
    # Plain inline HTML backgrounds leave gaps between browser text line boxes.
    template = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {terminal_width} {terminal_height}">
<style>{styles}\n.{unique_id}-matrix {{font-family:monospace;font-size:{char_height}px;line-height:{line_height}px}}</style>
<defs>{lines}</defs>{backgrounds}<g class="{unique_id}-matrix">{matrix}</g></svg>"""
    svg = console.export_svg(code_format=template, theme=terminal)

    def join_shape(match):
        attrs, glyph = match.groups()
        x = float(re.search(r'x="([\d.]+)"', attrs)[1])
        y = float(re.search(r'y="([\d.]+)"', attrs)[1]) - 18.5
        css_class = re.search(r'class="([^"]+)"', attrs)[1]
        points = "M0 0 L12.2 12.2 L0 24.4 Z" if glyph == "" else "M12.2 0 L0 12.2 L12.2 24.4 Z"
        return f'<path class="{css_class}" transform="translate({x:g},{y:g})" d="{points}"/>'

    # The actual bar renderer supplies join colors. Draw its two glyphs as paths so
    # comparing powerline colors does not require a Nerd Font in the browser.
    return re.sub(r"<text ([^>]+)>(|)</text>", join_shape, svg)


def sample(figures: Illustrations, terminal: TerminalTheme) -> dict:
    message = figures.message("Fix the empty-input crash in parser.py", "user", 0)
    rows = [
        message,
        Text(""),
        figures.message("Check the tokenizer, then handle empty input.", "assistant"),
        figures.log("Read", "parser.py"),
        figures.log("Edit", 'path="parser.py" replace_all=false'),
        figures.styled(UiPrinter.syntax_segments("def tokenize(text: str) -> list[str]:\n    return text.split()", "python", Theme.fg("text"))),
        figures.styled(figures.printer.diff_segments(DIFF_STYLE_SAMPLE, row_width=WIDTH)),
        figures.styled(
            [
                (Theme.fg("success"), "  12 passed"),
                (Theme.fg("muted"), " · "),
                (Theme.fg("warning"), "1 skipped"),
                (Theme.fg("muted"), " · "),
                (Theme.fg("error"), "0 failed"),
            ]
        ),
        Text(""),
        figures.styled([("class:choice.selected", "  /theme  "), (Theme.fg("muted"), " Choose a color theme")]),
    ]
    joined = Text("\n").join(row[:-1] if row.plain.endswith("\n") else row for row in rows)
    bars = {name: html_text(figures.bar({"statusbar": {"format": "preset:" + name}}, width=WIDTH), terminal) for name in STATUSBARS}
    dividers = {}
    for name in DIVIDERS:
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
        "swatches": {role: Color.parse(Theme.rich_color(role)).get_truecolor(terminal).hex for role in ("accent", "user", "tool", "status_reason", "user_bg")},
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wizolt theme preview</title><style>
:root{color-scheme:light;--ink:#393b3b;--muted:#71736e;--line:#d7d5cb;--paper:#f5f3ec;--accent:#46645d}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.6 "SFMono-Regular",Consolas,"DejaVu Sans Mono",monospace}
main{max-width:1520px;margin:0 auto;padding:32px}h1{font-weight:500;font-size:30px;margin:0 0 8px}
p{margin:0 0 18px}.intro{color:var(--muted)}.choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin:24px 0 16px}
button,select{font:inherit;color:inherit}button{cursor:pointer;border:1px solid var(--line);background:transparent;border-radius:5px;padding:10px 12px}
button:hover{background:#e9e8df}button.active{border-color:var(--accent);background:#e3e8e1;color:#294c41}button:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
button:disabled{opacity:.45;cursor:default}
.controls{display:flex;gap:20px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--line);padding:8px 0 18px}label{display:flex;gap:8px;align-items:center}
select{background:#fffdf7;border:1px solid var(--line);border-radius:4px;padding:5px 8px}.description{min-height:28px;margin:16px 0;color:var(--muted)}
.comparison{display:grid;grid-template-columns:1fr 1fr;gap:20px}.caption{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px}
.comparison>section{min-width:0}
h2{font-size:16px;font-weight:600;margin:0}.caption small{color:var(--muted)}.terminal{border:1px solid #555b66;border-radius:5px;overflow:auto;background:var(--bg);color:var(--fg);padding:12px}
pre{margin:0;font:12.5px/1.65 "SFMono-Regular",Consolas,"DejaVu Sans Mono",monospace;white-space:pre;tab-size:4}pre.body{padding-bottom:16px}.working{padding:0 0 14px}.footer pre{line-height:1.65}.input{padding-bottom:14px}
.terminal pre{white-space:normal}.terminal svg{display:block;width:100%;height:auto}
.swatches{display:flex;gap:14px;flex-wrap:wrap;padding:12px 0;color:var(--muted);font-size:12px}.swatches span{display:flex;gap:6px;align-items:center}.dot{width:12px;height:12px;border-radius:50%;border:1px solid #858984}
.pick{margin:20px 0 8px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}.pick button{background:var(--accent);color:white;border-color:var(--accent)}
#selection{color:var(--accent)}details{border-top:1px solid var(--line);padding-top:16px;margin-top:20px}summary{cursor:pointer;color:var(--muted)}
#config{background:#eeece3;border:1px solid var(--line);border-radius:4px;padding:16px;margin-top:12px;overflow:auto;font-size:13px}
.footnote{font-size:13px;color:var(--muted);margin:22px 0 0}.screen-width{margin-left:auto;color:var(--muted);font-size:13px}
@media(max-width:1100px){.comparison{grid-template-columns:1fr}.choices{grid-template-columns:repeat(3,1fr)}main{padding:24px}.screen-width{margin-left:0}}
@media(max-width:560px){main{padding:16px}h1{font-size:26px}.choices{grid-template-columns:repeat(2,1fr)}.caption{display:block}.controls{gap:12px}pre{font-size:11px}.terminal svg{min-width:620px}}
</style></head><body><main>
<h1>Wizolt theme preview</h1>
<p class="intro">Compare palettes, status bars, and dividers. Default dark stays unchanged. Every diff uses the existing classic colors.</p>
<nav class="choices" aria-label="Color presets"></nav>
<div class="controls">
<label>Status bar <select id="statusbar"><option>lualine</option><option>powerline</option><option>split</option><option>vim</option><option>default</option><option>minimal</option></select></label>
<label>Divider <select id="divider"><option>comet</option><option>capsule</option><option>rail</option></select></label>
<span class="screen-width">Same sample · 76 columns · Static colors</span></div>
<p class="description" id="description"></p>
<div class="comparison">
<section><div class="caption"><h2 id="before-title"></h2><small>Terminal default</small></div><div class="terminal" id="before"></div><div class="swatches" id="before-swatches"></div></section>
<section><div class="caption"><h2 id="after-title"></h2><small>Preset</small></div><div class="terminal" id="after"></div><div class="swatches" id="after-swatches"></div></section>
</div>
<div class="pick"><button id="pick">Pick this preset</button><span id="selection" role="status">Compare a few presets, then pick a favorite.</span></div>
<details><summary>View the configuration</summary><pre id="config"></pre></details>
<p class="footnote">Actual wizolt renderers, assembled for comparison. Messages shade only text rows. Input adds one shaded row above and below the text, with plain gaps to the divider and status bar. Both panels use the same terminal background. Light presets use a light terminal. Default dark and classic diff colors stay unchanged.</p>
</main><script id="data" type="application/json">__DATA__</script><script>
(() => {
const data=JSON.parse(document.getElementById('data').textContent);
let current=data.find(x=>x.id==='slate'), picked=null;
const nav=document.querySelector('.choices'), bar=document.getElementById('statusbar'), divider=document.getElementById('divider');
for(const item of data){const button=document.createElement('button');button.textContent=item.name;button.dataset.id=item.id;button.onclick=()=>{current=item;render()};nav.append(button)}
const labels={accent:'Accent',user:'User',tool:'Tools',status_reason:'Reasoning',user_bg:'Input background'};
function pane(id,sample){
 const element=document.getElementById(id);element.style.setProperty('--bg',current.bg);element.style.setProperty('--fg',current.fg);
 element.innerHTML='<pre class="body">'+sample.body+'</pre><pre class="working">'+sample.dividers[divider.value]+'</pre><div class="footer"><pre class="input">'+sample.input+'</pre><pre>'+sample.bars[bar.value]+'</pre></div>';
 const swatches=document.getElementById(id+'-swatches');swatches.replaceChildren();
 for(const [role,color] of Object.entries(sample.swatches)){const row=document.createElement('span'),dot=document.createElement('i');dot.className='dot';dot.style.background=color.startsWith('#')?color:'transparent';row.append(dot,document.createTextNode(labels[role]));swatches.append(row)}
}
function render(){
 for(const button of nav.children){const active=button.dataset.id===current.id;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active))}
 document.getElementById('description').textContent=current.note;
 document.getElementById('before-title').textContent=current.baseline;
 document.getElementById('after-title').textContent=current.name;
 pane('before',current.before);pane('after',current.after);
 document.getElementById('config').textContent=current.config;
 document.getElementById('pick').disabled=current.id==='reference';
 document.getElementById('selection').textContent=picked?'Selected: '+picked:'Compare a few presets, then pick a favorite.';
}
bar.onchange=render;divider.onchange=render;
document.getElementById('pick').onclick=()=>{picked=current.name+' · statusbar '+bar.value+' · divider '+divider.value;render()};
render();
})();
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "theme-preview.html")
    args = parser.parse_args()
    for key in ("NO_COLOR", "PROMPT_TOOLKIT_COLOR_DEPTH"):
        os.environ.pop(key, None)
    os.environ["COLORTERM"] = "truecolor"
    payload = []
    with (
        tempfile.TemporaryDirectory(prefix="wizolt-color-preview-") as folder,
        patch("time.monotonic", return_value=NOW),
        patch("shutil.get_terminal_size", return_value=os.terminal_size((WIDTH, 30))),
    ):
        session = Session(cwd=folder, config=Config(data_dir=folder))
        try:
            figures = Illustrations(session, Path(folder), [])
            figures.values.update(yolo=True, provider="zai", model="glm-5.3", reasoning="high")
            for candidate in CANDIDATES:
                light = Theme.BUILTIN[candidate["base"]].appearance == "light"
                bg, fg = ((245, 244, 240), (53, 58, 64)) if light else ((40, 44, 52), (207, 211, 218))
                terminal = TerminalTheme(
                    bg, fg, [TERMINAL_THEME.ansi_colors[index] for index in range(8)], [TERMINAL_THEME.ansi_colors[index] for index in range(8, 16)]
                )
                with patch("wizolt.utils.terminal.background", return_value=bg):
                    baseline = "light" if light else "dark"
                    Theme.configure(baseline, folder, {})
                    Theme.set_diff_style("classic")
                    before = sample(figures, terminal)
                    Theme.configure(candidate["base"], folder, {})
                    after = sample(figures, terminal)
                config = f'[runtime]\ntheme = "{candidate["base"]}"\n\n[ui.diff]\nstyle = "classic"'
                payload.append(
                    {
                        **candidate,
                        "baseline": baseline,
                        "before": before,
                        "after": after,
                        "bg": "#" + "".join(f"{part:02x}" for part in bg),
                        "fg": "#" + "".join(f"{part:02x}" for part in fg),
                        "config": config,
                    }
                )
        finally:
            session.close()
    args.output.write_text(PAGE.replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
