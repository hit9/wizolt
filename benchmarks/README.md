# Local performance baselines

## Subagent review

The current reference for future comparisons is
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
