# Performance optimization plan

Scope: preserve model requests, prompt-cache prefixes, durable history, cancellation and terminal
behavior. Work on local branch `dev19`; no remote operations. Do not change the project interpreter.

## Sequence and acceptance

- [x] 1. Prewarm only configured provider SDKs (main, compaction, vision, worker).
  Verify protocol resolution, mixed providers, deferred imports, startup and shutdown.
- [x] 2. Reuse completed transcript rendering across appended output and resize.
  Keep memory bounded, output byte-equivalent, row counts correct, and replay retention unchanged.
  Cover cache eviction, pending output, narrow/wide resize, and real tmux zoom/unzoom.
- [x] 3. Reuse token estimates within one request preparation.
  Preserve compaction decisions and final status, including automatic/fallback compaction and images.
- [x] 4. Reuse state digests in a frozen snapshot write plan to avoid repeated serialization.
  Preserve JSONL format, captured-state ownership, append/replacement detection, and concurrent input.
- [x] 5. Measure large-session restore; optimize demonstrated redundant work without deleting history.
  Verify old formats, blobs, delta replacements, transcript restoration, and malformed logs.
- [x] 6. Measure large-worktree completion; optimize demonstrated work without weakening path filtering.
  Verify Git/rg/walk fallback, symlinks, missing paths, stale cache, cancellation and current-query wins.
- [x] 7. Review changes, update design/changelog as needed, run full validation.

## Initial measurements

Local existing environment, synthetic workloads; these are not end-to-end latency promises.
Fresh process import medians (3 runs): entry 46 ms, CLI 248 ms, OpenAI 673 ms, Anthropic 727 ms,
both SDKs 1,277 ms. Markdown replay (heading, code, table, Chinese; 300 blocks) takes about 257 ms
on a cache miss, near zero on a hit. Unchanged snapshot preparation takes 5.27 ms for 1 MB of
message text and 27.73 ms for 5 MB, although the delta is only 861 bytes. Token estimation takes
5.46 ms for 1,000 messages of 1,000 ASCII characters. Literal ranking takes 13.9 ms for 100,000
paths; collecting/statting/sorting 10,000 local files takes 23.8 ms, excluding discovery.

## Validation

Use deterministic behavioral and bounded-work regression tests instead of CI wall-clock limits.
Repeat representative benchmarks before/after in the same environment, without concurrent tests;
report limitations and any deferred direction. Retain changes only with demonstrated benefit and
no material behavioral or performance regression. Withdraw changes that fail this gate.
Required final checks:

- `uv run pytest`
- `uv run pytest -m tmux`
- `uv run ruff check wizolt`
- `uv run ruff format --check wizolt`
- `uv run pyright`
- Build docs with `make -C docs html` if user-facing docs change.

## Results

All six directions have a bounded implementation. A reusable probe is in
`benchmarks/optimization.py`; it supports an exported baseline through `--source` and creates
only temporary fixtures. Baseline: `da32208`. Environment: Linux aarch64, existing CPython 3.14.7
virtualenv. Compare with the same interpreter and nine repetitions, with tests/builds stopped:

```sh
.venv/bin/python benchmarks/optimization.py --source /path/to/exported-da32208 --repeat 9
.venv/bin/python benchmarks/optimization.py --repeat 9
```

Median timings from uncontended runs (negative means less time):

| Workload | Before (ms) | After (ms) | Change |
| --- | ---: | ---: | ---: |
| Fresh process + configured Chat SDK warmup | 1340.555 | 699.280 | -47.8% |
| Fresh process + configured Anthropic SDK warmup | 1393.132 | 761.164 | -45.4% |
| Append one Markdown block, replay 300 prior blocks at two cached widths | 546.496 | 2.441 | -99.6% |
| Prepare request with 1 MB of message text | 12.044 | 7.030 | -41.6% |
| Prepare unchanged 1 MB snapshot | 5.382 | 2.795 | -48.1% |
| Prepare replacement of 1 MB snapshot | 10.263 | 7.682 | -25.1% |
| Prepare append to 1 MB snapshot (control) | 5.348 | 5.285 | -1.2% |
| Read and merge 10,000 JSONL deltas | 52.167 | 46.035 | -11.8% |
| Discover/collect 10,000 files with Python fallback | 31.658 | 6.272 | -80.2% |
| Validate 10,000 external candidates (control) | 24.299 | 23.612 | -2.8% |

These measure local operations, not model latency or total tmux resize time. Startup includes
process/session creation and completion of selected SDK imports, not first-prompt latency.
Single-width cold rendering and already-cached rendering are not claimed speedups.

Validation completed:

- Full suite: **3,481 passed**, with one existing fork-in-multithreaded-process deprecation warning.
- Real tmux: **28 passed, 2 xfailed** (existing expected failures), including alternate screen on/off.
- Ruff checks and formatting, Pyright: passed.
- HTML docs: passed using an isolated `uv run --no-project --with-requirements docs/requirements.txt
  make -C docs html` environment; the project virtualenv was not modified.
- Four bounded-work regression cases were also run against the exported baseline and all failed
  as intended: append/resize with pending or already-flushed output, single request estimation,
  and duplicate fallback file-type checks. They pass with the changes.

The restore change optimizes delta merging, without a new checkpoint format or history deletion.
The discovery change removes duplicate type checks only in the Python fallback; Git/rg candidates
still require validation. First-time terminal widths still need full layout, and replay still
writes the complete transcript to the terminal. No prompt-cache behavior is changed.

Cold replay initially appeared 8% slower in separate runs. A follow-up alternating both versions
in one process (21 measured samples each, identical rendered bytes and row counts) gave medians
**263.44 ms before / 264.59 ms after**. Treat cold replay as unchanged, not as a claimed benefit.
The two-width cache retains old layouts longer after output, within its existing character cap;
there is no unbounded per-message cache. Git/rg validation and ordinary append-only snapshot
workloads are control cases, not claimed speedups.

## Second pass: long transcript replay

Baseline: `c22a053`. Preserve exact terminal projection (including selected color depth), native
scrollback, row counts, pending-write ordering and the 5,000-write retention limit. No model or
prompt-cache changes. Keep only measured improvements, and record costs as well as benefits.

- [x] Replace all-or-nothing layout caching with bounded entries for two widths. Check partial
  caching above one million characters, eviction at 5,000 writes, oversized individual writes,
  pending/flush/direct writes, and bounded metadata and rendered text.
- [x] Evaluate bounded Markdown parse reuse across widths: **withdrawn**. It improved new-width
  layout, but alternating 21 samples per version showed first projection rising from 106.268 to
  110.015 ms (+3.5%), and the 100-document probe retained about 1.7 MB extra. No parse cache ships.
- [x] Measure ANSI/fragment conversion: adjacent-style coalescing preserved output but changed
  83.612 to 82.741 ms (about 1%); insufficient benefit to justify adding it. No change ships.
- [x] Add a full-projection benchmark (rather than only MessageBlock.ansi), including cold widths,
  revisited widths, cache-budget overflow, retention eviction and memory measurements.
- [x] Run targeted tests, baseline regression checks, full pytest, real tmux, Ruff, Pyright and docs.

### Second-pass measurements

Run `benchmarks/replay.py` with the same interpreter against an exported `c22a053` and the worktree:

```sh
.venv/bin/python benchmarks/replay.py --source /path/to/exported-c22a053 --repeat 7
.venv/bin/python benchmarks/replay.py --repeat 7
```

Uncontended local runs, median of seven samples. Each timed workload checks a SHA-256 digest of
its complete ANSI output and physical-row counts; **all baseline/current digests match**.
The final worktree measurements exclude both withdrawn experiments above.

| Workload | Before (ms) | After (ms) |
| --- | ---: | ---: |
| First projection, 100 Markdown blocks (control) | 107.318 | 107.390 |
| New width, 100 blocks (control) | 106.898 | 106.050 |
| Revisited width, 100 blocks (control) | <0.001 | 0.008 |
| Append to 100 blocks (control) | 0.573 | 0.555 |
| Append at the 5,000-write retention limit | 1481.701 | 0.913 |
| Revisited width above the character budget | 979.672 | 256.473 |

The two target workloads improve by 99.94% and 73.8%, respectively. First/new widths are controls,
not claimed speedups. This measures projection preparation, not terminal I/O or total tmux latency;
full transcript output is still written on replay.

Costs: a fully cached replay now traverses and joins entries, adding about 8 microseconds in the
100-block control. At two widths, tracemalloc reports retained allocations of 206,909 -> 242,943
bytes (+36,034 bytes), and peak allocations of 8,766,890 -> 8,584,669 bytes. These are measurements
of this fixture, not general memory bounds. Rendered text keeps the existing one-million-character
cap per width; metadata is bounded by the two widths and 5,000 retained writes. Oversized histories
now retain useful partial layouts within that budget, so they can retain more memory than before.

Validation:

- Targeted terminal tests: **127 passed**.
- Two bounded-work regressions fail on the exported baseline for the expected reasons: retained
  writes rerender after history eviction, and oversized transcripts discard all useful cache hits.
- Full suite: **3,484 passed**, one existing fork-in-multithreaded-process deprecation warning.
- Real tmux: **28 passed, 2 xfailed** (existing expected failures), including alternate screen on/off.
- Ruff checks/formatting and Pyright passed; HTML docs built in an isolated environment without
  changing the project's virtualenv.
