"""Repeatable local performance probes; run with the project's existing Python interpreter.

Compare identical workloads against an exported revision with --source /path/to/export.
Fixtures live in temporary directories; no model requests or user state are accessed.
Wall-clock results are observations, never CI assertions.
"""

import argparse
import asyncio
import contextlib
import gc
import io
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    source = str(args.source.resolve())
    sys.path.insert(0, source)
    from wizolt.config import Config
    from wizolt.mentions import FileMentions
    from wizolt.session import Session, SessionSnapshotCodec, SessionSnapshotStore

    # The same workload can measure exports from before the package reorganization.
    if (Path(source) / "wizolt" / "agent" / "engine.py").is_file():
        from wizolt.agent.engine import Agent
        from wizolt.ui.render import MessageBlock, UiPrinter
        from wizolt.ui.tui.scrollback import ScrollbackRegion
    else:
        from wizolt.engine import Agent
        from wizolt.render import MessageBlock, UiPrinter
        from wizolt.tui.scrollback import ScrollbackRegion

    results = {}

    def measure(name, invoke, prepare=lambda: None):
        times = []
        for _ in range(args.repeat):
            prepare()
            # Keep retained garbage from earlier probes out of this sample's starting state.
            # GC remains enabled inside the measured operation.
            gc.collect()
            start = time.perf_counter()
            invoke()
            times.append((time.perf_counter() - start) * 1000)
        results[name] = {"samples_ms": [round(value, 6) for value in times], "median_ms": round(statistics.median(times), 3), "min_ms": round(min(times), 3), "max_ms": round(max(times), 3)}

    with tempfile.TemporaryDirectory(prefix="wizolt-perf-") as directory:
        s = Session(cwd=directory, config=Config(data_dir=directory))
        s.ensure_ownership()
        try:
            s.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 1000} for i in range(1000)]
            s.transcript_messages = list(s.messages)
            s._snapshot_saved = SessionSnapshotCodec.marker(s)
            store = SessionSnapshotStore(s)
            measure("snapshot_unchanged_1mb", store.plan)
            s.messages.append({"role": "user", "content": "new input"})
            measure("snapshot_append_1mb", store.plan)
            s.messages[0]["content"] = "replacement"
            measure("snapshot_replace_1mb", store.plan)

            agent = Agent(s)
            s.settings.max_context_tokens = 2_000_000
            # Same event loop across samples, as in production; no model request is sent.
            with asyncio.Runner() as runner:
                measure("prepare_request_1mb", lambda: runner.run(agent.prepare_request([])))

            # A real delta's scalar fields plus an append to the durable transcript; active model
            # messages stay small as they do after compaction. No log checkpoint format changes.
            s.messages = [{"role": "user", "content": "retained request"}]
            s.transcript_messages = []
            marker = SessionSnapshotCodec.marker(s)
            template = SessionSnapshotCodec.delta(s, marker, {})
            log = Path(directory) / "restore.jsonl"
            with log.open("w") as output:
                output.write(json.dumps(SessionSnapshotStore.header(s)) + "\n")
                output.write(json.dumps(SessionSnapshotCodec.snapshot(s, {})) + "\n")
                for i in range(10000):
                    output.write(json.dumps({**template, "transcript_messages": [{"role": "assistant", "content": f"answer {i} " + "x" * 100}]}) + "\n")
            measure("restore_merge_10000_deltas", lambda: SessionSnapshotStore.read_merged(str(log)))

            files = Path(directory) / "files"
            files.mkdir()
            for i in range(10000):
                (files / f"file{i}.py").touch()
            mentions = FileMentions(Session(cwd=str(files), config=Config(data_dir=directory)))
            paths = [f"file{i}.py" for i in range(10000)]
            measure("collect_external_10000_files", lambda: mentions._collect(paths))
            measure("collect_walk_10000_files", lambda: mentions._collect(None))
        finally:
            s.close()

    markdown = "## Result\n\nSome **bold text** and 中文说明.\n\n```python\ndef hello(name):\n    return f'Hello {name}'\n```\n\n| Key | Value |\n| --- | --- |\n| status | ready |\n"
    printer = UiPrinter()
    region = ScrollbackRegion()
    cells = [MessageBlock(printer, markdown + str(i), "assistant", 0, False).ansi for i in range(300)]

    def cold():
        nonlocal region
        region = ScrollbackRegion()
        region.transcript = list(cells)

    measure("replay_cold_300_blocks", lambda: region._replay_layout(80), cold)

    def warm():
        cold()
        region._replay_layout(80)
        region._replay_layout(120)

    def append_and_zoom():
        with contextlib.redirect_stdout(io.StringIO()):
            region.write_direct(MessageBlock(printer, markdown, "assistant", 0, False).ansi)
        region._replay_layout(80)
        region._replay_layout(120)

    measure("replay_append_and_zoom_300_blocks", append_and_zoom, warm)
    measure("replay_warm_300_blocks", lambda: region._replay_layout(80), warm)

    # Fresh interpreters, warm filesystem cache. Includes process/session startup, not just the
    # selected imports. Run for both protocols to catch a trade-off favoring only one provider.
    for api in ("chat", "anthropic"):
        code = (
            "import sys;sys.path.insert(0,sys.argv[1]);"
            "from wizolt import __main__ as cli;"
            "from wizolt.config import Config,ProviderConfig;from wizolt.session import Session;"
            "import tempfile;"
            "d=tempfile.TemporaryDirectory();"
            "s=Session(cwd=d.name,config=Config(data_dir=d.name,providers={'default':ProviderConfig(api=sys.argv[2])}));"
            "modules=cli.startup_imports(s) if hasattr(cli,'startup_imports') else ['anthropic','openai'];"
            "cli.warm_imports(modules).join();d.cleanup()"
        )
        measure("startup_" + api, lambda code=code, api=api: subprocess.run([sys.executable, "-c", code, source, api], check=True, capture_output=True))

    print(json.dumps({"source": source, "python": sys.version, "repeat": args.repeat, "results": results}, indent=2))


if __name__ == "__main__":
    main()
