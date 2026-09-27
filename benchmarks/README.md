# Local performance baselines

Use the existing project environment; do not install or switch interpreters to run these probes.
Stop tests, builds and other heavy work first. Measurements use temporary session data and make no
model requests. Startup probes load SDKs without contacting providers.

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
