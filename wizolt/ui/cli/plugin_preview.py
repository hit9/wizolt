"""Export plugin component frames through the same projection used by the live TUI.

This is a cell-layout preview, not a screenshot of the user's terminal. Export is deliberately
outside the plugin worker: plugin code supplies semantic data, never SVG, fonts or file paths.
"""

from __future__ import annotations

import hashlib
import json
from html import escape
from pathlib import Path

from prompt_toolkit.utils import get_cwidth
from rich.color import Color

from wizolt.plugins.protocol import MAX_PANEL_ROWS, Snapshot
from wizolt.sdk import Panel
from wizolt.ui.bars import Fragments
from wizolt.ui.cli.plugins import PluginView
from wizolt.ui.render import Theme


class PreviewExporter:
    """A single cell grid drives text, SVG and PNG; clipping remains owned by PluginView.

    Font metrics vary across terminals. SVG permits browser fallback; PNG uses the explicitly
    selected font or an available monospace font. Reports retain text/styles for exact inspection.
    """

    CELL_WIDTH = 12
    CELL_HEIGHT = 28
    PADDING = 20
    FONT_SIZE = 20

    def __init__(self, directory: Path, *, font: str = "", height: int = 24):
        self.directory = directory
        self.font = font
        self.height = height

    @staticmethod
    def color(value: str) -> str:
        if value in ("", "default"):
            return "#202020" if Theme.appearance() == "light" else "#e0e0e0"
        resolved = Color.parse(Theme.RICH_COLOR_NAMES.get(value, value)).get_truecolor()
        return resolved.hex

    def export(self, frame: dict, index: int) -> list[dict]:
        snapshot = Snapshot.decode(frame)
        columns = frame["context"]["columns"]
        projected = PluginView.project({slot: [panel] for slot, panel in snapshot.panels.items()}, columns, self.height)
        return [self.panel(panel, slot, columns, index, projected.get(slot)) for slot, panel in snapshot.panels.items()]

    def panel(self, panel: Panel, slot: str, columns: int, index: int, fragments: Fragments | None = None) -> dict:
        # Match the live input component's cap. The status tab uses the full bounded panel.
        rows = PluginView.input_rows(self.height) if slot in PluginView.INPUT_SLOTS else MAX_PANEL_ROWS
        fragments = PluginView.render([panel], columns, rows) if fragments is None else fragments
        result = self.draw(fragments, slot, columns, index)
        result["clipped"] = len(panel.rows) > result["rows"] or any(get_cwidth(row.text) > columns for row in panel.rows)
        return result

    def color_of(self, value: str) -> str:
        return self.color(value if value in ("", "default") or value.startswith("ansi") else "#" + value)

    def draw(self, fragments: Fragments, slot: str, columns: int, index: int) -> dict:
        """Write SVG and PNG for already-projected rows: a component's or a bar preset's."""
        normalized: Fragments = []
        for style, value in fragments:
            for index_in_run, part in enumerate(value.split("\n")):
                if index_in_run:
                    normalized.append(("", "\n"))
                if part:
                    normalized.append((style, part))
        fragments = normalized
        text = "".join(value for _, value in fragments)
        lines = text.count("\n") + 1 if text else 0
        title = f"{slot} · {columns} columns · {Theme.name()}"
        foreground = self.color(Theme.color("muted"))
        # Transparent themes inherit a real terminal's background. Offline previews need an
        # explicit canvas; report the resolved value rather than pretending we queried a tty.
        background = Theme.active().background or ("#ffffff" if Theme.appearance() == "light" else "#202020")
        width = columns * self.CELL_WIDTH + self.PADDING * 2
        height = (max(1, lines) + 2) * self.CELL_HEIGHT + self.PADDING * 2
        svg = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            f'<rect width="100%" height="100%" fill="{background}"/>',
            '<g font-family="monospace" font-size="20" xml:space="preserve">',
            f'<text x="{self.PADDING}" y="{self.PADDING + self.FONT_SIZE}" fill="{foreground}">{escape(title)}</text>',
        ]
        runs = []
        column, line = 0, 0
        for style, value in fragments:
            if value == "\n":
                column, line = 0, line + 1
                continue
            attributes = Theme.transcript_style().get_attrs_for_style_str(style)
            color = self.color_of(attributes.color or "")
            # Bars are cut by background segments; a preview without them hides the layout.
            fill = self.color_of(attributes.bgcolor) if attributes.bgcolor and attributes.bgcolor != "default" else ""
            if attributes.reverse:
                color, fill = fill or background, color
            cells = get_cwidth(value)
            x = self.PADDING + column * self.CELL_WIDTH
            y = self.PADDING + (line + 2) * self.CELL_HEIGHT
            if value:
                if fill:
                    top = y - self.FONT_SIZE - (self.CELL_HEIGHT - self.FONT_SIZE) // 2
                    svg.append(f'<rect x="{x}" y="{top}" width="{cells * self.CELL_WIDTH}" height="{self.CELL_HEIGHT}" fill="{fill}"/>')
                svg.append(
                    f'<text x="{x}" y="{y}" fill="{color}" textLength="{cells * self.CELL_WIDTH}" lengthAdjust="spacingAndGlyphs">{escape(value)}</text>'
                )
                runs.append((x, y - self.FONT_SIZE, value, color, fill))
            column += cells
        svg.extend(("</g>", "</svg>"))
        # Slot names are not paths. The SDK currently restricts them, but exporters should not
        # acquire arbitrary write capability if the slot vocabulary grows in a future SDK.
        name = f"frame-{index}-{''.join(c if c.isalnum() else '-' for c in slot)}"
        self.directory.mkdir(parents=True, exist_ok=True)
        svg_path = self.directory / f"{name}.svg"
        svg_path.write_text("\n".join(svg), encoding="utf-8")
        png_path = self.directory / f"{name}.png"
        font = self.png(png_path, width, height, title, foreground, background, runs)
        return {
            "slot": slot,
            "columns": columns,
            "rows": lines,
            "text": text,
            "styles": fragments,
            "svg_path": str(svg_path),
            "png_path": str(png_path),
            # Hash rendered cells/colors/geometry, never export paths or random view IDs.
            # Raster font differences remain outside this cell-layout digest.
            "render_sha256": hashlib.sha256(json.dumps([width, height, background, runs], ensure_ascii=True).encode()).hexdigest(),
            "font": font,
            "background": background,
        }

    def png(self, path: Path, width: int, height: int, title: str, foreground: str, background: str, runs: list) -> str:
        # Pillow is only imported for image export, never for startup or live rendering.
        from PIL import Image, ImageDraw, ImageFont

        if self.font:
            font = ImageFont.truetype(self.font, self.FONT_SIZE)
        else:
            font = None
            for candidate in ("DejaVuSansMono.ttf", "/System/Library/Fonts/Menlo.ttc"):
                try:
                    font = ImageFont.truetype(candidate, self.FONT_SIZE)
                    break
                except OSError:
                    continue
            if font is None:
                font = ImageFont.load_default(size=self.FONT_SIZE)
        image = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(image)
        draw.text((self.PADDING, self.PADDING), title, font=font, fill=foreground)
        for x, y, value, color, fill in runs:
            if fill:
                top = y - (self.CELL_HEIGHT - self.FONT_SIZE) // 2
                draw.rectangle((x, top, x + get_cwidth(value) * self.CELL_WIDTH - 1, top + self.CELL_HEIGHT - 1), fill=fill)
            # Place characters by terminal cells rather than proportional font advances.
            # Combining marks attach to the previous cell; they do not advance the grid.
            previous = x
            for character in value:
                cells = get_cwidth(character)
                draw.text((x if cells else previous, y), character, font=font, fill=color)
                if cells:
                    previous, x = x, x + cells * self.CELL_WIDTH
        image.save(path)
        return str(getattr(font, "path", "Pillow default"))
