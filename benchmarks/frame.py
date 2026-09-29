"""Interactive first-frame probes: time from process start to a drawn prompt.

Run the same script/interpreter with --source pointing to an exported baseline and the worktree.
Each sample launches the interactive entry point under a pseudo-terminal with an isolated HOME,
recording when output first reaches the terminal and when the first frame draws the prompt. The
pseudo-terminal answers no OSC or CPR queries, so the background-color probe pays its full timeout
on both sides and cancels out of the comparison. The process is quit as soon as the frame is
seen; no model request is made and no user session data is touched. Subprocess probes retain
Python's default GC behavior.
"""

import argparse
import json
import os
import pty
import select
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def sample(source: Path, entry: str) -> dict[str, float | None]:
    home = tempfile.mkdtemp(prefix="wizolt-frame-home-")
    # The entry point requires a config; give it a minimal valid provider that no probe contacts.
    os.makedirs(os.path.join(home, ".wizolt"), exist_ok=True)
    with open(os.path.join(home, ".wizolt", "config.toml"), "w", encoding="utf-8") as file:
        file.write('[provider]\nactive = "default"\n\n[provider.default]\nurl = "https://example.test/v1"\nkey = ""\nmodel = ""\napi = "chat"\n')
    code = f"import sys;sys.path.insert(0,sys.argv[1]);from {entry} import main;sys.exit(main(sys.argv[2:]))"
    environment = {**os.environ, "HOME": home, "TERM": "xterm-256color"}
    master, slave = pty.openpty()
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(source)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
        close_fds=True,
    )
    os.close(slave)
    buffer = b""
    banner_ms = None
    frame_ms = None
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline and process.poll() is None:
            readable, _, _ = select.select([master], [], [], 0.02)
            if not readable:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break  # the child closed its end on exit; whatever it drew is already in the buffer
            if not chunk:
                break
            buffer += chunk
            if banner_ms is None:
                banner_ms = (time.perf_counter() - started) * 1000
            if frame_ms is None and b">" in buffer:
                frame_ms = (time.perf_counter() - started) * 1000
                break
        if frame_ms is None:
            raise RuntimeError(f"no prompt frame within deadline; output so far: {buffer[:200]!r}")
        try:
            os.write(master, b"/quit\r")
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    finally:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        os.close(master)
        shutil.rmtree(home, ignore_errors=True)
    return {"banner_ms": banner_ms, "frame_ms": frame_ms}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    entry = "wizolt.__main__" if (args.source / "wizolt" / "__main__.py").is_file() else "wizolt.cli"
    results = {name: {"samples_ms": []} for name in ("banner", "first_frame")}
    for _ in range(args.repeat):
        one = sample(args.source, entry)
        results["banner"]["samples_ms"].append(one["banner_ms"])
        results["first_frame"]["samples_ms"].append(one["frame_ms"])
    for row in results.values():
        row["median_ms"] = statistics.median(row["samples_ms"])
    print(json.dumps({"source": str(args.source), "python": sys.version, "repeat": args.repeat, "results": results}, indent=2))


if __name__ == "__main__":
    main()
