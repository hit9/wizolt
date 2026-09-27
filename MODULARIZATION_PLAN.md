# Modularization plan

Goal: make subsystem ownership visible in the directory tree, keep each capability cohesive,
and reduce the amount of another module's mutable state a collaborator can reach.
Behavior, snapshot formats, terminal history and delayed SDK imports must remain unchanged.

## Steps

- [x] Record import/startup baselines; group orchestration under `agent/` and presentation under
  `ui/`. Update internal consumers directly; do not leave forwarding modules behind.
- [x] Put each complete wire adapter beside its protocol conversions. Keep the common protocol
  contract independent of adapters; absorb the small reasoning-history helper into it.
  Consolidate `QueuedInput` with session value types if this preserves dependency direction.
- [x] Give frontend presentation state an explicit owner. Make `View` and transcript replay
  depend on their required data/output operations rather than the whole command loop. Keep
  admission, cancellation and shutdown owned by the runtime, with command dispatch separate.
- [x] Move feature assembly to the application layer. Make ownership of shared worker resources
  and session resource shutdown explicit without changing the persistent state model.
- [x] Trim misplaced base responsibilities, document the resulting boundaries, and add focused
  import/dependency guards and behavioral coverage where ownership changed.
- [x] Run full tests, quality checks, real tmux/Zellij acceptance and import/startup comparisons.

## Boundaries

- Entry/application assembly connects session state, resource owners, agent execution and UI.
- Agent orchestration consumes model/tool capabilities and session state; it never imports UI.
- UI state and terminal geometry never become session persistence data.
- Model wires own provider-specific construction/interpretation; the client owns retries and
  accounting. Heavy SDKs remain lazy.
- Session codecs/stores own durable encoding and IO; feature assembly does not happen in codecs.
- Shared value types and UI hook contracts are dependency leaves, even when located inside the
  subsystem that gives them meaning. Package initializers must not defeat this property.
- Avoid a replacement service locator: collaborators receive concrete, bounded dependencies.

## Validation / evidence

Baseline: 81 Python files, 18 root-level files, 33,312 lines including comments/blank lines.
Import timing samples are recorded outside the repository in `/tmp/wizolt-modules-baseline.json`.
Existing suite on the preceding change: 3,492 passed; tmux 28 passed / 2 known xfails;
Zellij 13 passed / 1 known xfail. Terminal jobs run separately from the CPU-heavy unit suite.

Completed ownership changes:

- Root-level Python files: 18 → 9. Total: 81 → 83 (two subsystem package initializers and
  substantive lifecycle/presentation owners added; model/history and session/queue absorbed).
  This reduces root-level navigation noise, not the total file count.
- CommandLoop: 1,132 → 627 lines. Session: 829 → 720 lines. View and ResumeRenderer no longer
  hold a CommandLoop; commands share one registry, and live/replayed output shares Presentation.
- WireProtocol and TranscriptOutput are structurally checked at their typed construction sites.
  Concrete implementations do not inherit the consumer's protocol. UiHooks remains a callback
  dataclass, and View consumes concrete bounded owners without another mirror interface.
- create_session/load_session own assembly and writable restore; BackgroundServices owns admitted
  maintenance tasks. Worker model shutdown preserves borrowed MCP until root resource shutdown.
- Logging policy belongs to the entry point. Update state belongs to Presentation. Existing
  relative/tilde-expanded cache paths remain resolved by Session.data_path at the call boundary.

Validation completed so far:

- Full default suite: 3,501 passed (one existing fork-from-multithreaded-process deprecation).
- Real tmux: 28 passed, 2 known xfails. Real Zellij 0.45.1: 13 passed, 1 known xfail.
- Ruff across the repository, format check for wizolt, and Pyright: passed.
- uv build: passed; extracted wheel imports and --version passed outside the source directory,
  with no stale pre-migration modules in the wheel.
- New coverage includes shared-MCP shutdown with real request lifecycle cancellation, repeated
  close, session-relative update caching, and fresh-interpreter lower-layer import guards.

User-requested benchmark baseline:

- Add benchmarks/run.py for source-revision export, metadata/sample recording and comparison.
- Commit a fixed master baseline and paired working-tree report under benchmarks/, recording this
  Linux ARM64 / Python 3.14 execution environment, not assuming macOS from the checkout path.
- Measure sequentially after tests/builds; compare output digests as well as timing distributions.

Internal refactors do not need changelog entries or a version bump. Internal Python import paths
change; CLI behavior and snapshot format are preserved. Commit and merge only when separately
requested.

Final performance evidence is versioned in `benchmarks/baselines/linux-arm64-py314-master.json`
and `benchmarks/results/linux-arm64-py314-dev22.json`. Both use 9 samples, identical dependency
versions, exported sources on the same filesystem, and GC before each in-process timed sample.
The runner verifies compatible metadata and records every sample; all six replay hashes match.
Pre-normalization experiments remain under `benchmarks/results/natural-gc/`.

The root import medians were 47.875 → 44.568 ms (__main__), 247.362 → 232.007 ms (CLI),
and 83.679 → 74.292 ms (model). File-walk medians were 6.233 → 6.211 ms; first replay of
100 blocks was 112.345 → 106.741 ms. These are local observations, not universal speedups:
append-and-zoom was 2.890 → 3.203 ms and Chat startup 718.797 → 750.259 ms. The full table
retains slower measurements instead of imposing a noisy pass/fail threshold or selecting only
improvements. The refactor's verified gains are explicit ownership, smaller coordination/state
owners, unchanged behavior coverage and reproducible performance evidence.

Final validation also checked baseline metadata mismatch rejection, replay digest mismatch
reporting, invalid repeat arguments, all sample counts, and hashes against the final working tree.
