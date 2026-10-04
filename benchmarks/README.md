# Local performance baselines

## Baseline updates

Branch reviews record measurements under `results/`; they do not replace the official baseline.
After merging into `master`/`main`, measure the merged commit with the same environment and workload
as the previous reference. Investigate significant regressions, optimize where practical and
document any retained cost. Update `baselines/` and this README's reference before releasing;
never accept a regression merely by replacing the baseline. If the environment or workload changed,
remeasure the previous reference revision too so the comparison remains meaningful.

## Plugin foundation

### Interception and presenters (branch review)

[Master](results/linux-arm64-py314-interception-master.json) and the
[reviewed branch](results/linux-arm64-py314-interception-review.json) (working tree over
`3effe92b`, exact source hash in the report) use Linux ARM64, CPython 3.14.7, identical
dependencies and workloads, nine samples, run back to back without tests or builds in flight.
The release reference is unchanged.

The first comparison found two startup regressions, both fixed before this measurement:
worker launch had grown by about 15 ms (`packaging` loaded even for plugins declaring no
dependencies, plus eager operation/presentation modules), and CLI import by about 5 ms
(interception modules loaded eagerly). Both now load only when used, guarded by import tests in
`tests/test_startup.py`.

| Workload | Master (ms) | Branch (ms) | Change |
| --- | ---: | ---: | ---: |
| First prompt frame | 148.595 | 154.117 | +3.7% |
| CLI import | 194.203 | 193.947 | -0.1% |
| Ten headless turns, no hooks | 26.717 | 28.252 | +5.8% |
| Enable and close one plugin | 100.837 | 104.069 | +3.2% |
| Plugin tool, 20 calls | 16.758 | 5.022 | -70.0% |
| Cached projection, 1,000 reads | 5.427 | 5.836 | +7.5% |

Replay and dense output hashes match. No workload rose more than 8%. The turn and projection
paths gained interception fast-path checks and per-registration health; the retained cost is
about 0.15 ms per turn and 0.4 microseconds per cached read.

New long-term probes (`plugins.py`) track the chain itself. Cores are trivial, so these are
pure host and IPC overhead:

| Interception workload | Median (ms) |
| --- | ---: |
| Empty chain, 1,000 operations | 2.015 |
| Non-matching chain, 1,000 operations | 2.715 |
| Three no-op interceptors, 20 operations | 24.100 |
| Buffered response transform, 20 requests | 8.316 |
| Present one tool result, 20 times | 4.224 |
| Refresh with an activity presenter, 20 passes | 11.055 |

An operation no plugin intercepts costs about 2 microseconds; each matching interceptor about
0.4 ms (the call and its `next` round trip). An activity presenter adds one round trip to each
5 Hz refresh pass.

### Cached plugin row projection (dev27)

[Before](results/dev27-plugin-hotspots-before.json) is `f0f4c8d8`; the
[after report](results/dev27-plugin-hotspots-after.json) records the optimized working-tree
source hash. Both use Linux ARM64, CPython 3.14.7, the same dependencies and workloads, and nine
samples. Measurements ran sequentially without tests or builds. The release reference is unchanged.

Profiling found repeated control-character cleaning, cell-width calculation and clipping in
the host paint path, even when worker snapshots were unchanged. A bounded row cache now keys
projection by immutable content, width and the existing theme revision. It retains up to 128
rows; changing content, terminal width or colors recomputes the projection. Worker scheduling,
RPC and validation are unchanged.

| Workload | Before (ms) | After (ms) | Change |
| --- | ---: | ---: | ---: |
| Enable and close one plugin | 97.607 | 93.795 | -3.9% |
| Three workers, 20 sample rounds | 11.731 | 11.927 | +1.7% |
| Plugin tool, 20 calls | 15.672 | 16.681 | +6.4% |
| Cached projection, 1,000 reads | 10.733 | 5.428 | -49.4% |
| Dense projection, 1,000 reads | 406.648 | 20.059 | -95.1% |

The new dense workload renders six unchanged rows of 64 alternating-color CJK/text spans at
100 columns. It guards repeated painting between samples, not continuously changing content:
cold rows still pay cleaning/clipping plus cache bookkeeping. Both dense output and terminal
replay hashes match. The smaller IPC/startup movements are observations, not claimed speedups
or regressions. No measured workload regressed by more than 15% in this comparison.

Reproduce with `benchmarks/run.py --revision f0f4c8d8 --repeat 9`, then run the current tree
with `--repeat 9 --baseline BEFORE.json`; supply `--output` for each report.

### Interactive views and portable preferences (dev27)

[Before](results/dev27-ui-before.json) is `138b9823`; [after](results/dev27-ui-after.json)
is `7c0a2914`. Both use Linux ARM64, CPython 3.14.7, identical dependencies/workloads and nine
samples, run sequentially without concurrent tests or builds. This branch comparison does not
replace the release reference.

| Workload | Before (ms) | After (ms) |
| --- | ---: | ---: |
| First prompt frame | 143.483 | 142.414 |
| Startup bootstrap, three skills | 91.407 | 95.389 |
| Enable and close one plugin | 87.466 | 95.389 |
| Three workers, 20 sample rounds | 11.913 | 11.982 |
| Plugin tool, 20 calls | 15.727 | 15.730 |
| Cached projection, 1,000 reads | 10.915 | 10.916 |
| Ten headless turns, no hooks | 25.689 | 26.204 |
| Twenty file reads, no hooks | 5.284 | 5.853 |
| External discovery, 10,000 files | 21.087 | 23.670 |

Replay hashes match. Continuous plugin sampling, calls and drawing remain close to the prior
revision. Retained startup costs are roughly 4 ms for bootstrap and 8 ms for enabling a worker;
the expanded SDK and interaction lifecycle add initialization work, not a per-frame callback.
The file probes also rose (0.57 ms and 2.58 ms); their implementation is unchanged, so these
measurements do not establish a regression in file discovery. Keep the raw observations rather
than absorbing them into a new baseline. No broad performance improvement is claimed.

### Alpha 2 release reference

The current reference is [0.73.0a2](baselines/linux-arm64-py314-0.73.0a2.json), measured
after merging `dev27` into `master` (`eae455be`, with release changes; exact source hash in
the report). [Alpha 1 was remeasured](results/linux-arm64-py314-before-0.73.0a2.json) with
the same current workloads, dependencies and environment: Linux ARM64, CPython 3.14.7,
nine samples, sequential runs without concurrent tests or builds.

| Workload | Alpha 1 (ms) | Alpha 2 (ms) |
| --- | ---: | ---: |
| First prompt frame | 141.790 | 153.361 |
| Startup bootstrap, three user skills | 93.522 | 97.105 |
| CLI import | 183.614 | 181.805 |
| Ten headless turns, no hooks | 25.782 | 25.988 |
| Cached plugin projection, 1,000 reads | 10.739 | 5.134 |
| Dense plugin projection, 1,000 reads | 375.924 | 20.007 |

Replay and dense plugin output hashes match. No measured timing increased by more than 15%
except the banner (47.004 → 60.306 ms). An
[alternating first-frame check](results/linux-arm64-py314-0.73.0a2-frame.json) confirms the
startup cost: banner 48.743 → 59.564 ms, first prompt 142.342 → 152.278 ms. The new key
migration check reads configuration before the banner so it can ask before the TUI takes over.
Retain and record this roughly 10 ms startup cost; do not treat it as noise or a per-turn cost.
Plugin row caching improves repeated painting; cold or changing rows still do the full work.

### Alpha 1 release reference

The previous reference is [0.73.0a1](baselines/linux-arm64-py314-0.73.0a1.json),
measured after merging `dev26` into `master` (`6697f998`, with release changes;
the report records the exact source hash). The previous published release, `2110ac18`,
was [remeasured](results/linux-arm64-py314-before-0.73.0a1.json) with identical workloads,
dependencies and environment: Linux ARM64, CPython 3.14.7, nine samples, sequential runs
without concurrent tests or builds. Older historical baselines remain available; their
workload metadata differs from the current plugin-aware suite.

| Workload | 0.72.0 (ms) | Alpha 1 (ms) |
| --- | ---: | ---: |
| First prompt frame | 142.515 | 145.992 |
| Startup bootstrap, three user skills | 82.372 | 91.372 |
| CLI import | 173.043 | 182.732 |
| Ten headless turns, no hooks | 26.442 | 25.826 |

Replay output hashes match. An [alternating bootstrap check](results/linux-arm64-py314-0.73.0a1-bootstrap.json)
confirmed the startup cost (82.696 → 90.470 ms), so it is retained, not dismissed as noise:
assembling the plugin capability adds about eight milliseconds once per fresh interpreter.
End-to-end first frame rises 2.4%; ordinary request and replay workloads have no material regression.

Enabled-plugin probes measure 88.915 ms enable/close, 11.603 ms for twenty three-worker sample
rounds, 15.811 ms for twenty tool calls including refresh, and 10.940 ms for 1,000 cached reads.
They remain close to the optimized layout measurements below; these costs are additional to
the disabled-plugin workloads above. This reference preserves the known allocation trade-off.

### Layout allocation and execution facts

[Before](results/linux-arm64-py314-layout-before.json) (`90224631`),
[initial implementation](results/linux-arm64-py314-layout-unoptimized.json), and
[optimized implementation](results/linux-arm64-py314-layout-after.json) use Linux ARM64 /
CPython 3.14.7, the same workload and dependencies, nine samples, and sequential runs without
tests/builds in flight. Working-tree source hashes are recorded in each report.

| Workload | Before (ms) | Initial (ms) | Optimized (ms) |
| --- | ---: | ---: | ---: |
| Three workers, 20 sample rounds | 5.682 | 18.871 | 12.039 |
| Plugin tool, 20 calls including refresh | 7.163 | 22.676 | 15.938 |
| Cached projection, 1,000 reads | 8.746 | 11.180 | 11.190 |
| Enable and close one worker | 89.779 | 94.986 | 87.490 |

Fusing sampling with each generation's first component removes one IPC per component-bearing
plugin. Remaining allocation requires sequential component results, so enabled sampling still
costs about 0.60 ms per round instead of 0.28 ms (roughly 1.6 ms extra per second at 5 Hz for
this three-component workload). Cached drawing adds about 2.4 microseconds per read. This is a
retained cost for responsive height allocation, not a speedup claim; user callbacks may cost more.

With plugins disabled, first prompt frame is 141.748 → 142.279 ms (+0.4%); other principal
workloads remain within 10%. Banner timing rose 11.9% (about 5 ms), while banner-to-prompt
fell 5.3%; the total first frame did not materially regress. Replay output hashes match.
The official baseline remains unchanged until the merged revision is measured.

### Complete branch review

[Master measurement](results/linux-arm64-py314-dev26-master.json) (`2110ac18`) and
[reviewed dev26 measurement](results/linux-arm64-py314-dev26-review.json) (working tree over
`bd930548`, exact source SHA-256 in the report) use Linux ARM64 / CPython 3.14.7, identical
dependencies and workloads, nine samples per metric, natural GC and warm filesystem caches.
Runs were sequential with no concurrent tests or builds. Plugins remain disabled for the
existing suites. No material regression was observed; all replay output hashes match.

| Workload | Master (ms) | Reviewed branch (ms) | Change |
| --- | ---: | ---: | ---: |
| First prompt frame | 145.253 | 138.879 | -4.4% |
| Startup bootstrap, three user skills | 89.883 | 91.146 | +1.4% |
| CLI import | 176.697 | 180.118 | +1.9% |
| Ten headless turns, no hooks | 26.299 | 25.843 | -1.7% |
| Prepare a 1 MB request | 7.129 | 6.610 | -7.3% |
| Append, 100 replay blocks | 0.595 | 0.624 | +4.9% |
| Emit 500 plain rows | 6.272 | 6.421 | +2.4% |

These local observations do not establish a general speedup. The largest relative increase
is a 0.029 ms replay append difference. The reference under `baselines/` is unchanged; remeasure
the merged commit before replacing it.

`plugins.py` adds four long-term probes through actual workers. Its fixed plugins expose one
field, one panel and a local tool; they make no network/model requests. Sampling is explicit,
so the automatic 5 Hz refresh cannot overlap timing. Workers import the selected source export,
not whichever wizolt is installed. Each metric has a warmup and nine measured samples:

| Enabled-plugin workload | Median (ms) |
| --- | ---: |
| Enable and close one worker | 83.135 |
| Sample three workers, 20 rounds | 5.236 |
| Invoke one tool, 20 calls including refresh | 6.516 |
| Read cached fields and project three panels, 1,000 times | 8.430 |

These are new observations, not comparisons with master (which has no plugins). Wall-clock
thresholds are deliberately not CI assertions. `run.py` now includes packaged Markdown in
working-tree exports and source hashes: omitting the built-in skill would make them differ
from Git revision exports. Historical reports with the old workload must be remeasured for
an apples-to-apples comparison.

```sh
uv run --no-sync python benchmarks/run.py --revision master --repeat 9 --output /tmp/master.json
uv run --no-sync python benchmarks/run.py --repeat 9 --baseline /tmp/master.json --output /tmp/review.json
uv run --no-sync python benchmarks/plugins.py --repeat 9
```

### Initial foundation

[Baseline](results/linux-arm64-py314-before-plugins.json) at `2110ac18` and
[plugin result](results/linux-arm64-py314-plugins.json) at `81b09124` use the same repaired
benchmark workloads and installed dependencies on Linux ARM64 / CPython 3.14.7. Runs were
sequential without concurrent tests or builds, with three samples per metric and natural GC.
These are exploratory observations, not a replacement for the nine-sample reference below.

Plugins were disabled (the shipped default). Startup bootstrap changed by +0.64%, first frame
by −4.18%, and CLI import by +3.97%; replay output hashes matched. These samples do not establish
a speedup or measure the cost of user-authored callbacks and enabled plugin workloads.

## Detail wrapping

[Recorded samples](results/linux-arm64-py314-detail-wrapping.json) compare `ca66036d` with
the shared text-wrapper fix (source SHA-256 in the report). Five interleaved samples per side,
after warmup, used CPython 3.14.7 on Linux ARM64 with no concurrent tests or builds.
Only `Text.wrap_styled` was replaced with its baseline implementation; the detail renderer
and dependencies were identical. Each sample created a new `DetailSheet` for a unified diff
adding one unbroken line, then called `fragments()` at 60×24 terminal cells.

| Added line | Before | After |
| --- | ---: | ---: |
| 1,000 characters | 0.675 ms | 0.460 ms |
| 100,000 characters | 2,399.996 ms | 26.881 ms |

Visible fragments matched exactly. The wrapper now advances over consumed cells instead of
rescanning and copying the remaining tail. Layout still allocates cells and rows proportional
to the full document; this is not a constant-memory or lazy-rendering change. These focused
measurements do not replace the broader startup/session baselines below.

## Subagent review

The historical subagent reference is
[`baselines/linux-arm64-py314-subagents.json`](baselines/linux-arm64-py314-subagents.json).
It compares the reviewed subagent branch with `master` at `4cc040f2`, recorded in
[`baselines/linux-arm64-py314-before-subagents.json`](baselines/linux-arm64-py314-before-subagents.json).
The optimized source SHA-256 is `1ab879b2eaeb5d86e5f9767782fdf7f71481a6a7ee52b54004e451396eaf93fd`;
the report's revision, `a7d2c6cc`, is the parent of the measured working tree.

Both full runs used Linux aarch64, installed CPython 3.14.7, identical dependencies and workloads,
nine samples per metric, temporary source exports without bytecode, warm filesystem caches, and
no concurrent tests or builds. These probes make no external model requests.

The [initial result](results/linux-arm64-py314-subagents-before-optimization.json) showed
10,000-delta merging +18.6% and ten headless turns +14.4%. A
[reverse-order repeat](results/linux-arm64-py314-subagents-reverse-repeat.json) reproduced both
(+15.3% and +16.5%). Checkpoints now omit unchanged agent metadata and empty default state;
state projection copies only durable fields rather than copying and discarding runtime fields.
Final turn snapshots remain mandatory.

Four [interleaved rounds](results/linux-arm64-py314-subagents-interleaved.json), alternating
master/optimized and optimized/master, measured the final change. Optimization metrics pool
12 samples per side; replay metrics pool 20. Medians are milliseconds:

| Metric | master | Optimized | Change |
| --- | ---: | ---: | ---: |
| Merge 10,000 session deltas | 46.901 | 41.272 | -12.0% |
| Prepare a 1 MB request | 7.252 | 7.172 | -1.1% |
| Ten headless turns, no hooks | 22.121 | 26.205 | +18.5% |
| Ten headless turns, configured hooks | 39.134 | 44.316 | +13.2% |
| Twenty reads, no hooks | 5.803 | 5.659 | -2.5% |
| Twenty reads, configured hooks | 36.975 | 38.184 | +3.3% |
| Rescan 100 cached skills | 0.530 | 0.636 | +19.9% |
| First projection, 100 blocks | 98.165 | 99.933 | +1.8% |
| Revisit width, 100 blocks | 0.022 | 0.022 | -1.0% |
| Append, 100 blocks | 0.610 | 0.675 | +10.5% |
| Append at 5,000-write limit | 0.861 | 0.891 | +3.5% |
| Revisit width above cache budget | 244.458 | 246.715 | +0.9% |
| Emit 500 plain rows | 6.127 | 6.337 | +3.4% |
| Recolor 100 blocks / 500 rows | 104.752 | 106.118 | +1.3% |

The merge regression is recovered. Headless turns still cost about **0.41 ms more per turn**
without hooks: unlike master, `Agent.run` now saves the final answer, settled status, elapsed time
and child result before returning. This is extra durability work, not identical persistence
semantics; removing that save would weaken recovery. The smaller rescan/append differences are
retained in the report rather than rounded away; cached skill rescanning itself has no behavior
change in this branch.

The full paired run measured first-frame latency at 139.61 → 144.99 ms. Every replay output hash
matches in both the paired and interleaved runs. These local measurements do not establish a
general speedup, and do not measure provider latency or many simultaneously active children.

To compare future work against the updated reference:

```sh
uv run --no-sync python benchmarks/run.py --repeat 9 \
  --baseline benchmarks/baselines/linux-arm64-py314-subagents.json \
  --output /tmp/wizolt-next-benchmark.json
# Reproduce interleaving with git archive exports, alternating source order for four rounds:
uv run --no-sync python benchmarks/optimization.py --source /path/to/export --repeat 3
uv run --no-sync python benchmarks/replay.py --source /path/to/export --repeat 5
```

## Theme refresh against 0.62.0

The theme, spacing, statusbar and live-preview commits after 0.62.0 (`ca45fa61`) are compared with
0.62.0 (`11493363`), the last source benchmarked by the appearance review below, in
[`baselines/linux-arm64-py314-before-theme-refresh.json`](baselines/linux-arm64-py314-before-theme-refresh.json)
and [`results/linux-arm64-py314-theme-refresh.json`](results/linux-arm64-py314-theme-refresh.json).
Both runs used Linux aarch64, installed CPython 3.14.7, nine samples per metric, identical
dependencies and workloads, an idle machine and warm filesystem caches.

That paired run, and a second pass in reverse order, pooled to 18 samples per side, showed
`frame.banner` +7.3%, `replay.append_100_blocks` +10.9% and `replay.revisited_width_100_blocks`
+15.9%. The two replay probes take under 1 ms and their medians varied as much between two runs
of the same revision. Six interleaved rounds of the frame and replay probes, alternating fresh
exports of each revision, did not reproduce any of them; see
[`results/linux-arm64-py314-theme-refresh-interleaved.json`](results/linux-arm64-py314-theme-refresh-interleaved.json),
which also keeps the reverse-order pass.

| Metric (median ms, interleaved) | 0.62.0 | Theme refresh | Samples |
| --- | ---: | ---: | ---: |
| Process → first prompt frame | 182.49 | 178.29 | 30 |
| Process → banner | 51.67 | 50.99 | 30 |
| First projection, 100 blocks | 99.76 | 98.17 | 54 |
| Revisit width, 100 blocks | 0.018 | 0.018 | 54 |
| Append, 100 blocks | 0.631 | 0.601 | 54 |
| Append at 5,000-write limit | 0.942 | 0.887 | 54 |
| Revisit width above cache budget | 247.83 | 251.13 | 54 |
| Emit 500 plain rows | 6.14 | 6.29 | 54 |
| Recolor 100 blocks / 500 rows | 106.39 | 106.51 | 54 |

Two-width retained memory medians were 243,401 → 242,084 bytes. Every replay output hash matches:
the changes leave the replay and append paths untouched, and the probes do not draw padded user
messages, statusbars or live command previews. Interleaved frame probes launch each export
directly, so their absolute times differ from `run.py`'s; compare them only within this table.
These single local runs show no regression; they do not establish a speedup.

To repeat without replacing these results:

```sh
uv run --no-sync python benchmarks/run.py --revision 11493363 --repeat 9 \
  --output /tmp/wizolt-before-theme-refresh.json
uv run --no-sync python benchmarks/run.py --revision ca45fa61 --repeat 9 \
  --baseline /tmp/wizolt-before-theme-refresh.json --output /tmp/wizolt-theme-refresh.json
# Interleave: export each revision with `git archive`, then alternate per round
uv run --no-sync python benchmarks/frame.py --source /tmp/wizolt-11493363 --repeat 5
uv run --no-sync python benchmarks/frame.py --source /tmp/wizolt-ca45fa61 --repeat 5
uv run --no-sync python benchmarks/replay.py --source /tmp/wizolt-11493363 --repeat 9
uv run --no-sync python benchmarks/replay.py --source /tmp/wizolt-ca45fa61 --repeat 9
```

## Appearance review against master

The appearance branch and its bug fixes are compared with `master` (`6b6492a96f12`) in
[`baselines/linux-arm64-py314-before-appearance-review.json`](baselines/linux-arm64-py314-before-appearance-review.json)
and [`results/linux-arm64-py314-appearance-final-review.json`](results/linux-arm64-py314-appearance-final-review.json).
The earlier [review results](results/linux-arm64-py314-appearance-review.json) are retained.
The final reviewed source is identified by SHA-256 `2fa8382a203c522f5a46baaef434316db5c45804e9f3af47b41404fb21ce410c`;
the report's Git revision is the parent of the measured working tree.

Both runs used Linux aarch64, installed CPython 3.14.7, nine samples per metric, identical
dependencies and workloads, and source exports without bytecode on the same temporary filesystem.
The final review reuses the recorded master baseline with matching environment and workload
metadata. Each measurement ran without concurrent tests or builds.
Frame measurements use warm filesystem caches and no terminal background-query replies.

| Metric (median ms) | master | Appearance review |
| --- | ---: | ---: |
| Process → first prompt frame | 144.45 | 142.27 |
| Banner → prompt | 94.25 | 95.76 |
| CLI import | 174.61 | 178.79 |
| Cold replay, 300 blocks | 265.18 | 258.73 |
| First projection, 100 blocks | 98.58 | 97.32 |
| New width, 100 blocks | 97.39 | 98.07 |
| Emit 500 plain rows | 5.94 | 5.96 |
| Recolor 100 blocks / 500 rows | 107.25 | 105.47 |
| Append at 5,000-write limit | 0.878 | 1.006 |
| Revisit width above cache budget | 244.46 | 247.74 |

First-frame latency decreased by 1.5% and recoloring by 1.7%; appending at the write limit
increased by 14.6% (0.128 ms), and replay above the cache budget increased by 1.3%.
Two-width retained memory was 244,190 → 242,312 bytes. These single local runs do not establish
a general speedup or slowdown. All replay output hashes match except recoloring, which switches
to the changed light-theme syntax palette. The probes do not measure every custom theme,
padded user messages, picker navigation latency, or animation smoothness; real tmux tests
cover menu resizing separately.

To repeat without replacing these results:

```sh
uv run --no-sync python benchmarks/run.py --revision 6b6492a96f12 --repeat 9 \
  --output /tmp/wizolt-before-appearance-review.json
uv run --no-sync python benchmarks/run.py --repeat 9 \
  --baseline /tmp/wizolt-before-appearance-review.json \
  --output /tmp/wizolt-appearance-review.json
```

## Earlier comparisons

The retrospective 0.55.1 comparison is recorded in
[`baselines/linux-arm64-py314-before-0.55.1.json`](baselines/linux-arm64-py314-before-0.55.1.json)
and [`results/linux-arm64-py314-release-0.55.1.json`](results/linux-arm64-py314-release-0.55.1.json).
It compares `da32208` (before the performance work) with `v0.55.1` (`77ea690`), using the current
benchmark methodology and environment. It is separate from the later modularization comparison.
The release's timing table and memory trade-off are recorded in [CHANGELOG.md](../CHANGELOG.md).

To reproduce that comparison without replacing the recorded baseline:

```sh
uv run --no-sync python benchmarks/run.py --revision da32208 --repeat 9 \
  --output /tmp/wizolt-before-0.55.1.json
uv run --no-sync python benchmarks/run.py --revision v0.55.1 --repeat 9 \
  --baseline /tmp/wizolt-before-0.55.1.json --output /tmp/wizolt-release-0.55.1.json
```

Use the existing project environment; do not install or switch interpreters to run these probes.
Stop tests, builds and other heavy work first. Measurements use temporary session data and make no
model requests. Startup probes load SDKs without contacting providers.

The `frame` suite measures the interactive startup the other suites decompose: each sample launches
the real entry point under a pseudo-terminal with an isolated HOME and a minimal unused provider,
recording when the banner reaches the terminal and when the first prompt frame draws. The
pseudo-terminal defaults to answering no OSC/CPR queries. Older revisions wait up to 200 ms for
the background-color probe; the editable-startup path sends this query after its first frame.
`--answer-background` also measures a terminal that answers immediately. `frame` measures
the warm-cache case only — a cold filesystem cache dominates both sides. Note that
`optimization.startup_chat`/`startup_anthropic` join the warm-up thread before stopping the clock,
so they measure "startup including background imports", not time-to-prompt; `frame.first_frame` is
the user-facing number and never waits for the warm-up thread.

To reproduce a project's banner-to-prompt pause, use the same interpreter and working directory
for both source revisions, with an isolated configuration and no model requests:

```sh
uv run --no-sync python benchmarks/frame.py --source /path/to/exported/source \
  --cwd /path/to/project --yolo --repeat 5
# Repeat with --answer-background to exclude an unanswered terminal query.
```

`banner_to_prompt` measures the interval after the banner, separately from process startup.
Add `--check-input` to send `Q` as soon as the prompt appears and wait for its echo before
clearing the draft and quitting. `first_echo` measures process-to-echo and `prompt_to_echo`
measures prompt-to-echo. Neither measures command execution or completion of background work.

The editable-startup comparison is recorded in
[`results/linux-arm64-py314-starting-input.json`](results/linux-arm64-py314-starting-input.json).
It compares `2317c8d` with the recorded working-tree source hash using the installed interpreter,
the wizolt repository as cwd, isolated HOME/config, `--yolo --check-input`, five alternating
samples per revision and terminal mode, and exports without bytecode on the same filesystem.

| Terminal response | Metric (median ms) | Before | After |
| --- | --- | ---: | ---: |
| None | Process → prompt | 268.77 | 85.91 |
| None | Banner → prompt | 232.57 | 45.29 |
| None | Prompt → first key echo | 114.48 | 2.86 |
| Immediate | Process → prompt | 130.33 | 84.99 |
| Immediate | Banner → prompt | 92.38 | 45.46 |
| Immediate | Prompt → first key echo | 102.45 | 3.08 |

The initial frame uses default colors until configuration attaches; submitted commands wait for
assembly. Provider imports still run during `starting…` and can delay later keystrokes. These
numbers measure one early keystroke, with warm filesystem caches and no personal hooks.

The subsequent bottom-anchored input layout is checked against `d20fef9` in
[`results/linux-arm64-py314-anchored-input.json`](results/linux-arm64-py314-anchored-input.json),
using the same workload and methodology (five alternating samples, installed CPython 3.14.7,
Linux aarch64, isolated HOME/config, warm caches). Process-to-prompt medians were 85.17 → 87.49 ms
without terminal replies and 84.64 → 82.71 ms with immediate background replies; prompt-to-key-echo
medians were 3.05 → 2.96 ms and 2.93 → 3.04 ms. These small differences do not establish a speedup
or slowdown. The probe does not answer CPR, so it also checks that the new layout does not wait
for a cursor-position reply. Layout stability and transcript preservation are covered separately
by terminal regression and real-multiplexer acceptance tests.

The startup-order correction is checked against `06cad6c` in
[`results/linux-arm64-py314-startup-order.json`](results/linux-arm64-py314-startup-order.json),
with the same environment and five alternating samples per terminal mode. Process-to-prompt
medians were 85.27 → 84.57 ms without replies and 86.35 → 83.89 ms with immediate background
replies; prompt-to-key-echo medians were 3.04 → 2.93 ms and 3.06 → 3.27 ms. This probe measures
initial editing, not command latency: commands now wait for initial skill discovery while the
editor remains live. Deliberately blocked discovery is exercised in the frontend regression tests.

The private-project comparison (project name and path omitted) is recorded in
[`results/linux-arm64-py314-terminal-probe-overlap.json`](results/linux-arm64-py314-terminal-probe-overlap.json):
the installed `wizolt` interpreter, `--yolo`, isolated HOME/config, default color output, five
alternating samples per revision and terminal mode. It compares `2ea1d69` with the recorded
working-tree source hash, exporting both without bytecode to the same temporary filesystem.
These numbers exercise the project's cwd but do not include personal configuration or hooks.

Record an immutable source revision:

```sh
uv run --no-sync python benchmarks/run.py --revision master --repeat 9 \
  --label my-machine --output benchmarks/baselines/my-machine.json
```

Compare the working tree using the same environment:

```sh
uv run --no-sync python benchmarks/run.py --repeat 9 --label my-machine \
  --baseline benchmarks/baselines/my-machine.json --output /tmp/wizolt-comparison.json
```

The report includes the source commit and content hash, workload hash, environment/dependency
versions, every timing sample, median, replay output hashes and retained memory. Both historical and working-tree code are
exported to temporary directories on the same filesystem, without existing bytecode caches. The current benchmark scripts drive both old and new module
layouts, so the workload stays the same. All runs reuse the current interpreter and dependencies;
this does not recreate a historical dependency environment. Import timings include a fresh Python
process and use a warm filesystem cache. In-process probes run `gc.collect()` outside each timed
sample to normalize their starting state; GC stays enabled during timing. Subprocess probes retain
Python's default GC behavior. These are operation timings, not an end-to-end interactive latency SLA.

`comparison.comparable` checks environment, workload, GC policy and sample-count equality. It cannot account
for CPU contention, thermal throttling or host load. A positive `median_change_percent` means
slower; a negative value means faster. Very short operations have especially noisy percentages.
Repeat suspicious results on an idle machine before treating them as regressions. Timing changes
are observations, not automatic CI pass/fail thresholds. `output_matches` compares complete ANSI
output and physical row counts; every replay output must match for this refactor.

Do not overwrite a baseline just because an optimization changed the result. Keep its source
revision and add a new named file when deliberately adopting a new reference. Changing workloads
or dependencies makes old measurements non-comparable; record a fresh baseline in that case.

The initial `linux-arm64-py314-master.json` records the execution environment used for the
modularization work: Linux ARM64, Python 3.14.7. The checkout path starts with `/Users`, but this is
not a macOS measurement. A macOS baseline must be recorded on that machine. The paired working-tree
report is `results/linux-arm64-py314-dev22.json`; its source hash identifies the measured code before
commit. Labels are descriptive; the recorded environment determines comparability.

Preliminary measurements under `results/natural-gc/` predate GC normalization and are retained as
diagnostics. Their workload hashes differ from the adopted baseline, so they are not directly
comparable. That method showed a roughly 13% difference in a 10,000-file scan whose implementation
was unchanged. A paired probe with GC before timing measured about 6.20 ms (master) and 6.09 ms
(working tree), while profiling found equal call counts. This motivated controlling the starting
state, not changing the scan implementation. Neither diagnostic proves universal speedups.

## Skills compatibility comparison

`baselines/linux-arm64-py314-before-skills.json` records `master` at `eaba7c6`;
`results/linux-arm64-py314-skills.json` records the skills-compatibility working tree against it
(Linux ARM64, CPython 3.14.7, 9 samples, comparable). The workload adds three probes:
`skills_load_100` (the startup scan of 100 skills), `skills_turn_rescan_100` (the rescan at each
turn start, absent before) and `startup_bootstrap_3_skills` (a fresh interpreter assembling a
session over three user skills). Medians in milliseconds:

| Metric | Before | After | Change |
| --- | ---: | ---: | ---: |
| optimization.skills_load_100 | 1.078 | 8.230 | +663% |
| optimization.skills_turn_rescan_100 | — | 0.540 | new |
| optimization.startup_bootstrap_3_skills | 82.722 | 97.730 | +18.1% |
| optimization.startup_chat | 689.938 | 681.430 | -1.2% |
| optimization.startup_anthropic | 735.315 | 752.540 | +2.3% |
| optimization.prepare_request_1mb | 7.397 | 6.740 | -8.9% |
| imports.wizolt.model | 75.782 | 78.484 | +3.6% |

The skill costs are YAML: about 9 ms to import PyYAML the first time a skill is parsed, and about
0.07 ms per SKILL.md parsed instead of 0.01 ms with the line regex it replaced. Sessions without
skills never import it. Parsed skills are cached by file signature, so the per-turn rescan stays
near half a millisecond for 100 skills. `wizolt.model` imports the stdlib-only skill invocation
and hook modules the tool layer needs (about 3 ms). Other probes moved within their usual noise.

## Recorded comparison

Each cell is the median of 9 samples in milliseconds. This is a structural refactor, not a claim
of universal speedup: slower readings remain visible below, including append-and-zoom (+0.313 ms).
All six replay output hashes match. Host load and small-operation timing still vary despite
normalizing the filesystem and GC starting state. No timing threshold is enforced in CI.

| Metric | master | dev22 | Change |
| --- | ---: | ---: | ---: |
| optimization.snapshot_unchanged_1mb | 3.208 | 3.163 | -1.40% |
| optimization.snapshot_append_1mb | 6.026 | 5.841 | -3.07% |
| optimization.snapshot_replace_1mb | 7.821 | 7.946 | +1.60% |
| optimization.prepare_request_1mb | 7.255 | 7.231 | -0.33% |
| optimization.restore_merge_10000_deltas | 47.857 | 49.893 | +4.25% |
| optimization.collect_external_10000_files | 24.758 | 22.405 | -9.50% |
| optimization.collect_walk_10000_files | 6.233 | 6.211 | -0.35% |
| optimization.replay_cold_300_blocks | 269.878 | 266.315 | -1.32% |
| optimization.replay_append_and_zoom_300_blocks | 2.890 | 3.203 | +10.83% |
| optimization.replay_warm_300_blocks | 0.089 | 0.087 | -2.25% |
| optimization.startup_chat | 718.797 | 750.259 | +4.38% |
| optimization.startup_anthropic | 807.788 | 748.498 | -7.34% |
| replay.first_projection_100_blocks | 112.345 | 106.741 | -4.99% |
| replay.new_width_100_blocks | 118.642 | 108.403 | -8.63% |
| replay.revisited_width_100_blocks | 0.053 | 0.026 | -50.94% |
| replay.append_100_blocks | 0.789 | 0.621 | -21.29% |
| replay.append_at_5000_write_limit | 1.136 | 0.983 | -13.47% |
| replay.revisited_width_above_character_budget | 261.246 | 250.084 | -4.27% |
| imports.wizolt.__main__ | 47.875 | 44.568 | -6.91% |
| imports.wizolt.cli | 247.362 | 232.007 | -6.21% |
| imports.wizolt.model | 83.679 | 74.292 | -11.22% |

## Hooks review comparison

The paired reports are
[`baselines/linux-arm64-py314-before-hooks-review.json`](baselines/linux-arm64-py314-before-hooks-review.json)
and [`results/linux-arm64-py314-hooks-review.json`](results/linux-arm64-py314-hooks-review.json).
They compare `skills-compat` HEAD `ce69d84` with the reviewed working tree (source hash in the
report), on Linux ARM64 / CPython 3.14.7, with 9 samples and matching workload/environment metadata.
Tests and builds were stopped during measurement; the current interpreter and dependencies served
both source exports. Reproduce with:

```sh
uv run --no-sync python benchmarks/run.py --revision ce69d84 --repeat 9 \
  --output /tmp/wizolt-before-hooks.json
uv run --no-sync python benchmarks/run.py --repeat 9 \
  --baseline /tmp/wizolt-before-hooks.json --output /tmp/wizolt-after-hooks.json
```

The new probes run ten headless Agent turns with real snapshots and fixed local model replies,
and a batch of twenty reads of one small file. The configured-hook variants run `true` at
UserPromptSubmit/Stop or PreToolUse/PostToolUse, respectively. Setup is outside timing, while
execution includes event-loop setup and session closure. Configured tool hooks serialize these
reads in both revisions. No external model or MCP calls are made.

| Workload | Before (ms) | After (ms) | Change |
| --- | ---: | ---: | ---: |
| 10 turns, no hooks | 22.511 | 22.569 | +0.26% |
| 20 reads, no hooks | 5.580 | 5.668 | +1.58% |
| 10 turns, configured hooks | 39.722 | 40.264 | +1.36% |
| 20 reads, configured hooks | 39.516 | 37.306 | -5.59% |
| Fresh session with 3 skills | 97.164 | 97.978 | +0.84% |
| Prepare a 1 MB request | 7.234 | 7.187 | -0.65% |
| Drain 8 MiB, retain 1,024 characters | 9.718 | 4.741 | -51.21% |

No substantial regression was observed on these workloads. Small timing differences are within
sample variation; they do not establish a general speedup. The large-output probe additionally
records Python allocation peaks with tracemalloc in a separate untimed run: 16,808,227 bytes before,
371,443 after. It measures `head -c 8388608 /dev/zero`, including process and pipe handling, not total
process RSS. Bounded draining trades extra reader-task bookkeeping for avoiding retention of the
whole output. All six terminal replay output hashes match; retained two-width replay memory moved
by +3,979 bytes. Hooks still add the running time of every matching user command.

## Themes comparison

[`baselines/linux-arm64-py314-before-themes.json`](baselines/linux-arm64-py314-before-themes.json)
records `master` at `1f12316`, before `/theme`;
[`results/linux-arm64-py314-themes.json`](results/linux-arm64-py314-themes.json) records the
`theme-switching` working tree against it (Linux ARM64, CPython 3.14.7, 9 samples, comparable).
Both were measured with this workload, which adds two replay probes: `emit_500_plain_rows` renders
500 tool lines through the printer into a transcript, the path every emitted line takes, and
`recolor_100_blocks_500_rows` times `/theme`'s redraw of a transcript mixing markdown blocks and
plain lines (revisions without `/theme` skip it). Both pin the recorded depth to 256 colors and
clear `NO_COLOR`, `COLORTERM` and `PROMPT_TOOLKIT_COLOR_DEPTH`, so every revision draws the same
bytes. Reproduce with:

```sh
uv run --no-sync python benchmarks/run.py --revision 1f12316 --repeat 9 \
  --output /tmp/wizolt-before-themes.json
uv run --no-sync python benchmarks/run.py --repeat 9 \
  --baseline /tmp/wizolt-before-themes.json --output /tmp/wizolt-themes.json
```

Medians in milliseconds for the probes this work touches:

| Metric | Before | After | Change |
| --- | ---: | ---: | ---: |
| replay.emit_500_plain_rows | 12.614 | 6.089 | -51.7% |
| replay.recolor_100_blocks_500_rows | — | 108.261 | new |
| replay.first_projection_100_blocks | 106.548 | 100.356 | -5.8% |
| replay.new_width_100_blocks | 107.067 | 104.526 | -2.4% |
| replay.revisited_width_above_character_budget | 254.035 | 256.941 | +1.1% |
| optimization.replay_cold_300_blocks | 260.697 | 257.947 | -1.1% |
| imports.wizolt.cli | 229.475 | 240.589 | +4.8% |

Every replay output hash matches, so the default theme draws byte-identical output, and retained
two-width replay memory moved by -582 bytes. Other probes moved within their usual noise;
`append_100_blocks`, a 0.6 ms operation, read -18%, -4%, +4% and +20% across four comparisons the
same day. The import delta is also noise: fifteen interleaved imports of each revision measured
250.1 ms before and 244.7 ms after, and `-X importtime` shows the new modules cost about 0.9 ms
(`wizolt.ui.themes` 0.72 ms, `wizolt.utils.terminal` 0.16 ms; `tomlkit` loads only when a theme is
saved).

The emit probe caught what the markdown-only probes could not. Tagging fragments with
`class:role.<name>` first made the path 2x slower (25.2 ms), because prompt-toolkit resolves a
class by scanning every style rule and a transcript row renders outside any live render's cache;
memoizing resolved style strings on the one transcript style per theme brought it below the
baseline. That style costs about 174 KB, once per active theme.

## First-frame comparison

`baselines/linux-arm64-py314-before-first-frame.json` (`02699d1`, master) against
`results/linux-arm64-py314-first-frame.json` (`perf/first-frame`, `3d60fc5` plus the uncommitted
skill-attach change), 9 samples, the same environment as the comparisons above. The work moved
Rich and its Markdown stack off the interactive first-frame path (warming it in the background
thread instead), deferred the manual-compaction import to `/compact`, and attached the skill
library without scanning it at startup.

| Metric | Before | After | Change |
| --- | ---: | ---: | ---: |
| frame.first_frame | 590.3 | 485.5 | -17.8% |
| frame.banner | 45.7 | 43.5 | -4.8% |
| imports.wizolt.cli | 251.5 | 189.7 | -24.6% |
| imports.wizolt.model | 90.9 | 82.1 | -9.7% |
| optimization.startup_anthropic | 787.2 | 1024.4 | +30.1% |

Both `frame` numbers include the pseudo-terminal's full 200 ms background-probe timeout, so the
frame path itself went from about 390 ms to 285 ms. The `startup_*` increase is the semantics
documented above — the probe joins the warm-up thread, which now also loads `wizolt.ui.markdown` —
not a time-to-prompt regression; `frame.first_frame` never waits for that thread. Other probes
moved within the ±10% run-to-run band this suite shows: `imports.wizolt.__main__` read -12.8% and
+10.6% on the same comparison, and the `replay.*` metrics swung similarly across same-day runs.
