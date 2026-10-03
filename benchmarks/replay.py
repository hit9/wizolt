"""Full terminal-projection probes, with temporary output and no model or user-state access.

Run the same script/interpreter with --source pointing to an exported baseline and the worktree.
Do not run tests or builds concurrently. Digests compare complete ANSI output and physical rows.
"""

import argparse
import contextlib
import gc
import hashlib
import importlib
import io
import json
import os
import statistics
import sys
import time
import tracemalloc
from functools import partial
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.output import ColorDepth

    package = "wizolt.ui" if (args.source / "wizolt" / "ui" / "render.py").is_file() else "wizolt"
    render = importlib.import_module(f"{package}.render")
    ScrollbackRegion = importlib.import_module(f"{package}.tui.scrollback").ScrollbackRegion
    MessageBlock, UiPrinter, Theme = render.MessageBlock, render.UiPrinter, render.Theme

    # Recorded rows take their depth from the output, which is a pipe here; pin it to the 256
    # colors a terminal selects, and keep the color environment out, so every revision draws the
    # same bytes.
    for variable in ("NO_COLOR", "PROMPT_TOOLKIT_COLOR_DEPTH", "COLORTERM"):
        os.environ.pop(variable, None)
    output = SimpleNamespace(get_default_color_depth=lambda: ColorDepth.DEPTH_8_BIT)
    render.get_app_session = lambda: SimpleNamespace(output=output)

    printer = UiPrinter()
    results = {}
    markdown = (
        "## Result\n\nSome **bold text** and 中文说明.\n\n```python\ndef hello(name):\n"
        "    return f'Hello {name}'\n```\n\n| Key | Value |\n| --- | --- |\n| status | ready |\n"
    )

    def cell(text):
        return partial(printer.render_to_ansi, [MessageBlock(printer, text, "assistant", 0)], color_depth=ColorDepth.DEPTH_8_BIT)

    def fresh(entries):
        region = ScrollbackRegion()
        region.transcript = list(entries)
        return region

    def measure(name, invoke):
        elapsed = []
        digest = hashlib.sha256()
        for index in range(args.repeat):
            # Keep retained garbage from earlier probes out of this sample's starting state.
            # GC remains enabled inside the measured operation.
            gc.collect()
            start = time.perf_counter()
            text, rows = invoke(index)
            elapsed.append((time.perf_counter() - start) * 1000)
            digest.update(text.encode())
            digest.update(str(rows).encode())
        results[name] = {
            "samples_ms": [round(value, 6) for value in elapsed],
            "median_ms": round(statistics.median(elapsed), 3),
            "min_ms": round(min(elapsed), 3),
            "max_ms": round(max(elapsed), 3),
            "output_sha256": digest.hexdigest(),
        }

    entries = [cell(markdown + str(index)) for index in range(100)]
    measure("first_projection_100_blocks", lambda _: fresh(entries)._replay_layout(80))
    region = fresh(entries)
    region._replay_layout(80)
    measure("new_width_100_blocks", lambda index: region._replay_layout(81 + index))
    measure("revisited_width_100_blocks", lambda _: region._replay_layout(81 + args.repeat - 1))

    def append_and_replay(index):
        with contextlib.redirect_stdout(io.StringIO()):
            region.write_direct(cell(f"appended **{index}**\n"))
        return region._replay_layout(80)

    region = fresh(entries)
    region._replay_layout(80)
    measure("append_100_blocks", append_and_replay)

    # Same public retention limit as production; this must not discard all 4,999 old layouts.
    region = fresh([cell(f"retained **{index}**\n") for index in range(5000)])
    region._replay_layout(80)
    measure("append_at_5000_write_limit", append_and_replay)

    # Exceed the character budget using real Markdown -> ANSI -> fragments -> output conversion.
    large = [cell(markdown + ("plain content " * 600) + str(index)) for index in range(150)]
    region = fresh(large)
    text, _ = region._replay_layout(80)
    assert len(text) > region.MAX_CACHED_LAYOUT_CHARS
    measure("revisited_width_above_character_budget", lambda _: region._replay_layout(80))

    # Plain rows -- tool lines, notices -- are most of what a session prints, and each one is
    # rendered once as it is emitted. A per-row cost here is paid on every line of every turn.
    def emit_rows(count):
        recorded = []
        emitter = UiPrinter(output_fn=lambda text: None)
        emitter.transcript_sink = recorded.append
        for index in range(count):
            emitter.print_parts([FormattedText([(Theme.fg("tool"), "  Read "), (Theme.fg("muted"), f"src/file{index}.py 0:100 → tr.{index}\n")])])
        return recorded

    measure("emit_500_plain_rows", lambda _: ("".join(emit_rows(500)), 500))

    # Track cache and renderer allocations with existing inputs, separately from timing samples.
    region = fresh(entries)
    gc.collect()
    tracemalloc.start()
    region._replay_layout(80)
    region._replay_layout(120)
    gc.collect()
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    results["two_widths_100_blocks_memory"] = {"retained_bytes": retained, "peak_bytes": peak}

    # `/theme` redraws everything retained: markdown blocks re-render, and plain rows re-render
    # from their kept fragments. Alternate two themes so every sample is a real change. Last, so
    # the theme it leaves behind cannot charge its style's construction to the memory probe.
    # Revisions from before `/theme` have nothing to recolor and skip it.
    if hasattr(ScrollbackRegion, "recolor"):
        region = fresh([*entries, *emit_rows(500)])
        region._replay_layout(80)

        def recolor(index):
            Theme.set_mode(("light", "dark")[index % 2])
            region.recolor()
            return region._replay_layout(80)

        measure("recolor_100_blocks_500_rows", recolor)
    print(json.dumps({"source": str(args.source.resolve()), "python": sys.version, "repeat": args.repeat, "results": results}, indent=2))


if __name__ == "__main__":
    main()
