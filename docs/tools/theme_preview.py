"""Build a standalone color proposal from wizolt's real, offline renderers.

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


def colors(accent, user, tool, secondary, muted, surface, rule, *, light=False):
    """Use one main accent and quieter related colors for supporting information."""
    return {
        "accent": accent,
        "accent_secondary": secondary,
        "info": accent,
        "user": user,
        "user_bg": surface,
        "tool": tool,
        "success": tool,
        "muted": muted,
        "subtle": muted,
        "rule": rule,
        "warning": "#896522" if light else "#d3bf91",
        "error": "#9e5058" if light else "#de9398",
        "status_base": "#414956" if light else "#c7cbd2",
        "status_provider": accent,
        "status_reason": secondary,
        "status_mcp": muted,
        "status_context": tool,
        "status_yolo": user,
        "status_worker": secondary,
        "status_bg": surface,
        "menu_bg": surface,
        "menu_muted": muted,
        "selection_bg": "#d6dfdf" if light else "#485367",
        "selection_fg": "#273a41" if light else "#f0f2f4",
        "divider_glow": accent,
        "divider_rule": rule,
        "divider_label": accent,
        "syntax_assign": accent,
        "syntax_ident": accent,
        "syntax_builtin": secondary,
        "syntax_number": secondary,
        "syntax_string": tool,
    }


CANDIDATES = [
    {"id": "reference", "name": "00 · Default dark", "base": "dark", "note": "Unchanged reference palette.", "colors": {}},
    {
        "id": "slate",
        "name": "01 · Slate",
        "base": "one-dark",
        "note": "Blue tools and status bands, neutral user text, and cool code highlights.",
        "pygments": "github-dark",
        "colors": {
            **colors("#82b3e0", "#e2e8f0", "#82b3e0", "#a4b0c3", "#a2a9b5", "#30333b", "#48505e"),
            "success": "#a0cba8",
            "status_bg": "#293442",
            "menu_bg": "#344254",
            "selection_bg": "#425d7a",
        },
    },
    {
        "id": "forest",
        "name": "02 · Forest",
        "base": "everforest",
        "note": "A green identity: sage bands, leaf-green tools, and pale neutral user text.",
        "pygments": "native",
        "colors": {
            **colors("#93c79c", "#dce7da", "#9ad59a", "#b9c5b7", "#a0ada5", "#303733", "#48534c"),
            "status_bg": "#24302b",
            "menu_bg": "#2c3832",
            "selection_bg": "#395744",
        },
    },
    {
        "id": "sand",
        "name": "03 · Sand",
        "base": "gruvbox-dark",
        "note": "Amber tools and bands, warm white user text, and brown-gray supporting details.",
        "pygments": "gruvbox-dark",
        "colors": {
            **colors("#e2b66f", "#ece3d5", "#d2ac78", "#bdae98", "#b1aaa0", "#343337", "#55514d"),
            "success": "#a8c090",
            "status_bg": "#332a24",
            "menu_bg": "#3b3229",
            "selection_bg": "#725533",
        },
    },
    {
        "id": "plum",
        "name": "04 · Plum",
        "base": "rose-pine-dark",
        "note": "Lavender tools and bands, pale user text, and a distinct purple code palette.",
        "pygments": "dracula",
        "colors": {
            **colors("#b998d0", "#ece8f1", "#c9a3e4", "#b5a6c1", "#aaa6b9", "#323139", "#514e5f"),
            "success": "#9bc2b5",
            "status_bg": "#302938",
            "menu_bg": "#352d42",
            "selection_bg": "#67517d",
        },
    },
    {
        "id": "paper",
        "name": "05 · Paper",
        "base": "catppuccin-light",
        "note": "Ink-blue tools on paper gray, dark neutral user text, and clear blue selection bands.",
        "pygments": "friendly",
        "colors": {
            **colors("#245b84", "#3c454d", "#245b84", "#5f6c76", "#68716d", "#eae9e5", "#c0c5c1", light=True),
            "success": "#477253",
            "menu_bg": "#e3e6e7",
            "selection_bg": "#d2e1ec",
        },
    },
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


def sample(figures: Illustrations, terminal: TerminalTheme, *, compact=False) -> dict:
    message = figures.message("Fix the empty-input crash in parser.py", "user", 0)
    if compact:
        # A layout proposal: retain actual message colors, remove its shaded top row.
        message = message[message.plain.index("\n") + 1 :]
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
    idle = figures.bar({"divider": {"format": "preset:comet"}}, kind="divider", width=WIDTH, values={"running": False})
    idle.stylize("on " + Theme.rich_color("user_bg"))
    prompt = Text("> ", style=Theme.rich_color("accent")) + Text("Type a message...", style=Theme.rich_color("muted"))
    prompt.append(" " * (WIDTH - prompt.cell_len))
    prompt.stylize("on " + Theme.rich_color("user_bg"))
    bottom = Text(" " * WIDTH, style="on " + Theme.rich_color("user_bg"))
    return {
        "body": html_text(joined, terminal),
        "bars": bars,
        "dividers": dividers,
        "input": html_text(prompt if compact else Text("\n").join([idle, prompt, bottom]), terminal),
        "swatches": {role: Color.parse(Theme.rich_color(role)).get_truecolor(terminal).hex for role in ("accent", "user", "tool", "status_reason", "user_bg")},
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wizolt theme preview</title><style>
:root{color-scheme:light;--ink:#393b3b;--muted:#71736e;--line:#d7d5cb;--paper:#f5f3ec;--accent:#46645d}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.6 "SFMono-Regular",Consolas,"DejaVu Sans Mono",monospace}
main{max-width:1520px;margin:0 auto;padding:32px}h1{font-weight:500;font-size:30px;margin:0 0 8px}
p{margin:0 0 18px}.intro{color:var(--muted)}.choices{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin:24px 0 16px}
button,select{font:inherit;color:inherit}button{cursor:pointer;border:1px solid var(--line);background:transparent;border-radius:5px;padding:10px 12px}
button:hover{background:#e9e8df}button.active{border-color:var(--accent);background:#e3e8e1;color:#294c41}button:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
button:disabled{opacity:.45;cursor:default}
.controls{display:flex;gap:20px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--line);padding:8px 0 18px}label{display:flex;gap:8px;align-items:center}
select{background:#fffdf7;border:1px solid var(--line);border-radius:4px;padding:5px 8px}.description{min-height:28px;margin:16px 0;color:var(--muted)}
.comparison{display:grid;grid-template-columns:1fr 1fr;gap:20px}.caption{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px}
.comparison>section{min-width:0}
h2{font-size:16px;font-weight:600;margin:0}.caption small{color:var(--muted)}.terminal{border:1px solid #555b66;border-radius:5px;overflow:auto;background:var(--bg);color:var(--fg);padding:12px}
pre{margin:0;font:12.5px/1.65 "SFMono-Regular",Consolas,"DejaVu Sans Mono",monospace;white-space:pre;tab-size:4}pre.body{padding-bottom:16px}.working{padding:0 0 14px}.footer pre{line-height:1.65}
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
<nav class="choices" aria-label="Palette proposals"></nav>
<div class="controls">
<label>Status bar <select id="statusbar"><option>lualine</option><option>powerline</option><option>split</option><option>vim</option><option>default</option><option>minimal</option></select></label>
<label>Divider <select id="divider"><option>comet</option><option>capsule</option><option>rail</option></select></label>
<span class="screen-width">Same sample · 76 columns · Static colors</span></div>
<p class="description" id="description"></p>
<div class="comparison">
<section><div class="caption"><h2 id="before-title"></h2><small>Current preset</small></div><div class="terminal" id="before"></div><div class="swatches" id="before-swatches"></div></section>
<section><div class="caption"><h2 id="after-title"></h2><small>Proposed colors + spacing</small></div><div class="terminal" id="after"></div><div class="swatches" id="after-swatches"></div></section>
</div>
<div class="pick"><button id="pick">Pick this direction</button><span id="selection" role="status">Compare a few directions, then pick a favorite.</span></div>
<details><summary>View the proposed color configuration</summary><pre id="config"></pre></details>
<p class="footnote">Rendered wizolt samples, assembled for comparison. The right panel proposes single-row message and input backgrounds, with space below the divider. Color settings are shown below; compact spacing is a separate layout proposal. Both sides share a terminal background; Paper uses a light terminal. Default dark is an unchanged reference.</p>
</main><script id="data" type="application/json">__DATA__</script><script>
(() => {
const data=JSON.parse(document.getElementById('data').textContent);
let current=data.find(x=>x.id==='slate'), picked=null;
const nav=document.querySelector('.choices'), bar=document.getElementById('statusbar'), divider=document.getElementById('divider');
for(const item of data){const button=document.createElement('button');button.textContent=item.name;button.dataset.id=item.id;button.onclick=()=>{current=item;render()};nav.append(button)}
const labels={accent:'Accent',user:'User',tool:'Tools / success',status_reason:'Reasoning',user_bg:'Input background'};
function pane(id,sample){
 const element=document.getElementById(id);element.style.setProperty('--bg',current.bg);element.style.setProperty('--fg',current.fg);
 element.innerHTML='<pre class="body">'+sample.body+'</pre><pre class="working">'+sample.dividers[divider.value]+'</pre><div class="footer"><pre>'+sample.input+'</pre><pre>'+sample.bars[bar.value]+'</pre></div>';
 const swatches=document.getElementById(id+'-swatches');swatches.replaceChildren();
 for(const [role,color] of Object.entries(sample.swatches)){const row=document.createElement('span'),dot=document.createElement('i');dot.className='dot';dot.style.background=color.startsWith('#')?color:'transparent';row.append(dot,document.createTextNode(labels[role]));swatches.append(row)}
}
function render(){
 for(const button of nav.children){const active=button.dataset.id===current.id;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active))}
 document.getElementById('description').textContent=current.note;
 document.getElementById('before-title').textContent=current.base;
 document.getElementById('after-title').textContent=current.name;
 pane('before',current.before);pane('after',current.after);
 document.getElementById('config').textContent=current.config;
 document.getElementById('pick').disabled=current.id==='reference';
 document.getElementById('selection').textContent=picked?'Selected: '+picked:'Compare a few directions, then pick a favorite.';
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
                    Theme.configure(candidate["base"], folder, {})
                    Theme.set_diff_style("classic")
                    before = sample(figures, terminal)
                    if candidate["colors"]:
                        name = "proposal-" + candidate["id"]
                        Theme.configure(name, folder, {name: {"base": candidate["base"], "pygments": candidate["pygments"], "colors": candidate["colors"]}})
                        if Theme.name() != name:
                            raise ValueError("Candidate failed to load: " + name)
                    after = sample(figures, terminal, compact=bool(candidate["colors"]))
                name = "proposal-" + candidate["id"]
                config = f'[ui.themes.{name}]\nbase = "{candidate["base"]}"\n\n[ui.themes.{name}.colors]\n' + "\n".join(
                    f'{role} = "{color}"' for role, color in candidate["colors"].items()
                )
                if "pygments" in candidate:
                    config = config.replace(f'base = "{candidate["base"]}"', f'base = "{candidate["base"]}"\npygments = "{candidate["pygments"]}"')
                if candidate["id"] == "reference":
                    config = '[runtime]\ntheme = "dark"\n\n[ui.diff]\nstyle = "classic"'
                payload.append(
                    {
                        **candidate,
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
