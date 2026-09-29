"""Record benchmarks and compare with a versioned baseline on the same environment.

Run without concurrent tests/builds. Historical revisions are exported to a temporary directory;
the existing interpreter and dependency environment are reused without installing anything.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = ("optimization", "replay", "frame")


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def digest(paths, root):
    result = hashlib.sha256()
    for path in sorted(paths):
        result.update(path.relative_to(root).as_posix().encode() + b"\0")
        result.update(path.read_bytes() + b"\0")
    return result.hexdigest()


def environment():
    cpu = platform.processor()
    info = Path("/proc/cpuinfo")
    if info.exists():
        cpu = next((line.split(":", 1)[1].strip() for line in info.read_text().splitlines() if line.startswith("model name")), cpu)
    quota = Path("/sys/fs/cgroup/cpu.max")
    return {
        "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
        "processor": cpu, "python": platform.python_version(), "implementation": platform.python_implementation(),
        "cpu_count": os.cpu_count(), "cpu_affinity_count": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "cpu_quota": quota.read_text().strip() if quota.exists() else None,
        "packages": dict(sorted((dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions() if dist.metadata["Name"] != "wizolt")),
    }


def measure(source, repeat):
    suites = {}
    for suite in SUITES:
        print(f"Measuring {suite} ({repeat} samples)", file=sys.stderr)
        result = subprocess.run(
            [sys.executable, str(ROOT / "benchmarks" / f"{suite}.py"), "--source", str(source), "--repeat", str(repeat)],
            check=True, capture_output=True, text=True,
        )
        suites[suite] = json.loads(result.stdout)["results"]
    imports = {}
    for name in ("wizolt.__main__", "wizolt.ui.cli" if (source / "wizolt/ui").is_dir() else "wizolt.cli", "wizolt.model"):
        samples = []
        for _ in range(repeat):
            started = time.perf_counter()
            subprocess.run([sys.executable, "-c", "import sys,importlib;sys.path.insert(0,sys.argv[1]);importlib.import_module(sys.argv[2])", str(source), name], check=True, capture_output=True)
            samples.append((time.perf_counter() - started) * 1000)
        imports[name.replace("wizolt.ui.cli", "wizolt.cli")] = {"samples_ms": samples, "median_ms": statistics.median(samples)}
    suites["imports"] = imports
    return suites


def comparison(current, baseline):
    mismatches = [key for key in ("environment", "workload_sha256", "repeat", "gc_policy") if current.get(key) != baseline.get(key)]
    results = {}
    for suite, metrics in current["results"].items():
        for name, value in metrics.items():
            old = baseline["results"].get(suite, {}).get(name)
            if old is None:
                continue
            row = {}
            if "median_ms" in value and old.get("median_ms"):
                row["median_change_percent"] = round((value["median_ms"] / old["median_ms"] - 1) * 100, 2)
            if "output_sha256" in value:
                row["output_matches"] = value["output_sha256"] == old.get("output_sha256")
            if "retained_bytes" in value:
                row["retained_bytes_change"] = value["retained_bytes"] - old["retained_bytes"]
            results[f"{suite}.{name}"] = row
    return {"comparable": not mismatches, "mismatched_metadata": mismatches, "baseline_source": baseline["source"], "metrics": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="local Git revision to export; default: current working tree")
    parser.add_argument("--repeat", type=int, default=9)
    parser.add_argument("--label", default="local", help="human-readable environment/run label")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, help="write JSON here; default: stdout")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    with tempfile.TemporaryDirectory(prefix="wizolt-benchmark-") as temporary:
        source = Path(temporary) / "source"
        source.mkdir()
        revision = git("rev-parse", "--verify", (args.revision or "HEAD") + "^{commit}")
        if args.revision:
            archive = Path(temporary) / "source.tar"
            subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", f"--output={archive}", revision], check=True)
            with tarfile.open(archive) as bundle:
                bundle.extractall(source, filter="data")
        else:
            # Put both revisions on the same filesystem and start without bytecode caches.
            # A mounted checkout and a local /tmp export can have very different import costs.
            for path in [*ROOT.glob("wizolt/**/*.py"), *ROOT.glob("wizolt/**/*.json"), ROOT / "pyproject.toml", ROOT / "uv.lock"]:
                target = source / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        report = {
            "schema_version": 1, "recorded_at": datetime.now(UTC).isoformat(), "label": args.label,
            "environment": environment(), "repeat": args.repeat,
            "gc_policy": "collect before each in-process sample; enabled during timing; subprocess defaults",
            "workload_sha256": digest([ROOT / "benchmarks" / f"{name}.py" for name in (*SUITES, "run")], ROOT),
            "source": {"revision": revision, "working_tree": not bool(args.revision),
                       "sha256": digest([*source.glob("wizolt/**/*.py"), *source.glob("wizolt/**/*.json"), source / "pyproject.toml", source / "uv.lock"], source)},
            "results": measure(source, args.repeat),
        }
    if args.baseline:
        report["comparison"] = comparison(report, json.loads(args.baseline.read_text()))
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
