"""Full terminal-projection probes, with temporary output and no model or user-state access.

Run the same script/interpreter with --source pointing to an exported baseline and the worktree.
Do not run tests or builds concurrently. Digests compare complete ANSI output and physical rows.
"""

import argparse
import contextlib
import gc
import hashlib
import io
import json
import statistics
import sys
import time
import tracemalloc
from functools import partial
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from prompt_toolkit.output import ColorDepth

    from wizolt.render import MessageBlock, UiPrinter
    from wizolt.tui.scrollback import ScrollbackRegion

    printer = UiPrinter()
    results = {}
    markdown = (
        "## Result\n\nSome **bold text** and 中文说明.\n\n```python\ndef hello(name):\n"
        "    return f'Hello {name}'\n```\n\n| Key | Value |\n| --- | --- |\n| status | ready |\n"
    )

    def cell(text):
        return partial(printer.render_to_ansi, [MessageBlock(printer, text, "assistant", 0, False)], color_depth=ColorDepth.DEPTH_8_BIT)

    def fresh(entries):
        region = ScrollbackRegion()
        region.transcript = list(entries)
        return region

    def measure(name, invoke):
        elapsed = []
        digest = hashlib.sha256()
        for index in range(args.repeat):
            start = time.perf_counter()
            text, rows = invoke(index)
            elapsed.append((time.perf_counter() - start) * 1000)
            digest.update(text.encode())
            digest.update(str(rows).encode())
        results[name] = {
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
    print(json.dumps({"source": str(args.source.resolve()), "python": sys.version, "repeat": args.repeat, "results": results}, indent=2))


if __name__ == "__main__":
    main()
