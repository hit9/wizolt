"""Transcript record probes: the per-tool-call hot path, from a settled call to laid-out rows.

Each sample settles 300 tool calls (Bash with a 50-line stream, Read with 200 lines, a failure)
through `toolblocks.finish_display` and lays every block out at 100 columns, under the default
(builtin) rendering, `preset:minimal`, and a custom tail-3 format. Revisions without
`[transcript] format` measure the default only. Run with --source like the other suites.
"""

import argparse
import gc
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from wizolt.base import ToolCall
    from wizolt.config import Config
    from wizolt.session import Session
    from wizolt.tools import Tool, toolblocks
    from wizolt.ui.render import UiPrinter

    try:
        from wizolt.tools import transcript
    except ImportError:
        transcript = None

    directory = tempfile.mkdtemp()
    session = Session(cwd=directory, config=Config(data_dir=directory))
    printer = UiPrinter()
    stream = "\n".join(f"line {index}: some build output that is moderately long" for index in range(50))
    calls = []
    for index in range(100):
        calls.append((ToolCall(f"b{index}", "Bash", ["cargo test --workspace"]), Tool.process_result("BashToolResult", 0, stream, ""), False))
        calls.append((ToolCall(f"r{index}", "Read", [{"path": "src/db/rows.rs"}]), "\n".join(f"{n}: fn row_{n}() {{}}" for n in range(200)), False))
        calls.append((ToolCall(f"f{index}", "Read", [{"path": "missing.rs"}]), "ToolError: no such file", True))

    formats = {"default": {}}
    if transcript is not None:
        formats["minimal"] = {"format": "preset:minimal"}
        # The same look in either engine's language: call line, last three output lines, trailer.
        if hasattr(transcript, "OUTPUT_ROW"):
            custom = "{tool} {args}\n{output|tail:3}\n{% if elided %}… +{elided} more lines{% endif %}"
        else:
            custom = "{tool} {args}\n{% for line in output|tail:3 %}{line}\n{% endfor %}{% if elided %}… +{elided} more lines{% endif %}"
        formats["custom_tail3"] = {"format": custom}

    results = {}
    for name, table in formats.items():
        session.config.transcript = table
        elapsed = []
        digest = hashlib.sha256()
        for _ in range(args.repeat):
            gc.collect()
            start = time.perf_counter()
            rows = []
            for number, (call, output, failed) in enumerate(calls):
                block = toolblocks.finish_display(session, call, f"tr.{number}", output, failed=failed, elapsed=0.4)
                rows.append(printer.log_segments(block, 100) if not isinstance(block, str) else block)
            elapsed.append((time.perf_counter() - start) * 1000)
            digest.update(repr(rows).encode())
        results[f"settle_300_records_{name}"] = {
            "samples_ms": [round(value, 6) for value in elapsed],
            "median_ms": round(statistics.median(elapsed), 3),
            "min_ms": round(min(elapsed), 3),
            "max_ms": round(max(elapsed), 3),
            "output_sha256": digest.hexdigest(),
        }
    print(json.dumps({"source": str(args.source.resolve()), "python": sys.version, "repeat": args.repeat, "results": results}, indent=2))


if __name__ == "__main__":
    main()
