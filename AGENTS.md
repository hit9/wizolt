# Agent guide

Keep this file short. It is an entry point, not a second design document.

## Start here

- New: read [Orientation](design/DESIGN.md#orientation) (objectives, module layers, one turn's shape)
  and skim [Common pitfalls](design/DESIGN.md#common-pitfalls) — changes that look like cleanups and are
  not.
- Read [design/DESIGN.md](design/DESIGN.md) before changing cross-cutting behavior or module ownership; follow
  the nearest existing pattern before introducing a new abstraction or dependency.
- For main/subagent state ownership, read [State ownership](design/STATE_OWNERSHIP.md).

## Project map

- `wizolt/agent/lifecycle.py`: application session creation/resume, feature assembly, background task
  ownership and resource shutdown. `Session` stores state; it does not assemble features.
- `wizolt/agent/engine.py`: the agent turn loop composing context, model, and tools; `wizolt/agent/hooks.py`
  is the one presentation seam (`UiHooks`) the agent shares with them.
- `wizolt/agent/context.py`, `wizolt/model/`, `wizolt/agent/runner.py`: context projection/compaction,
  provider request protocols (`model/client.py` with the per-API adapters beside it), and the tool
  execution lifecycle.
- `wizolt/ui/cli/update.py`, `wizolt/ui/cli/hints.py`: the background version check and quick hints.
- `wizolt/session/`: durable semantic state (`__init__.py`) and snapshot persistence
  (`store.py`); `store.py` never imports the package at module scope.
- `wizolt/tools/`, `wizolt/image.py`, `wizolt/mcp/`, `wizolt/skill/`: vertical
  features; `tools/` splits built-ins by capability, registry in `__init__.py`. `skill/` splits
  reading (`skillfile`), discovery, trust, invocation, and what the model was told (`listing`).
- `wizolt/shellhooks.py`: supported command-hook events, orchestrated by the runner, engine and compactor.
- `wizolt/utils/`: dependency-free helpers (`process.ShellCommand`, `workspace.Workspace`, parsers).
- `wizolt/config.py`, `wizolt/providers/`: config-file settings, the model capability catalog
  (`providers/catalog.py`), and evidence-backed compatibility policy (`providers/compat.py`).
- `wizolt/ui/cli/`, `wizolt/ui/tui/`, `wizolt/ui/render.py`: commands (`cli/commands.py`,
  `cli/modals.py`, agent selection in `cli/agents.py`), resume replay (`cli/resume.py`), TUI runtime (`cli/runtime.py`), view
  fragments (`cli/view.py`), interaction, and presentation state (`cli/presentation.py`).
  `View` and replay receive bounded dependencies rather than a `CommandLoop` handle.
- `tests/`: behavior-oriented tests grouped by subsystem and boundary.

## Project workflow

- **Tests:** run targeted tests while iterating and `uv run pytest` before completing behavior changes.
  That run excludes real-multiplexer acceptance tests (they need a terminal and real time); run
  `uv run pytest -m tmux` and `uv run pytest -m zellij` when a change touches the terminal
  projection. The Zellij suite requires 0.45.1 (the CI pin) or a compatible later version.
- **Quality:** run `uv run ruff check wizolt`, `uv run ruff format --check wizolt`, and `uv run pyright`.
- **Docs:** on user-facing doc changes, update the English source and build `html`
  (`make -C docs html`).
- **Docs standard:** `docs/` is written for users, not for the people who changed the code.
  - Say what the reader gets and what it costs them. Leave out how it is implemented, why an
    alternative was rejected, and the taxonomy of ways it can fail.
  - Prefer a number to a mechanism: "about eight recent messages survive" beats a paragraph on
    how the window is bounded.
  - Add a trade-off only when the reader has to choose. Name the setting or the `/status` row
    that answers it, and stop.
  - Rewriting a paragraph is usually better than appending one. A behavior change means the old
    sentence is wrong, not incomplete.
  - Reasoning that is worth keeping but not worth showing a user goes in a code comment or a
    commit message.
  - A `term-shot` is a screenshot in HTML: when the colors or rows it depicts change, it is stale
    and has to be redrawn. Keep every `<span>` closed.
- **Changelog:** record notable changes under `Unreleased`, including user-facing behavior,
  internal refactors, dependencies, tests/CI, and documentation or maintenance work. Performance
  claims need before/after measurements with the workload, environment and comparison revisions;
  link the recorded results and include meaningful trade-offs or regressions.
- **Release (only when requested):** bump `pyproject.toml` and `wizolt/base.py`, move Unreleased
  entries under the dated version, run tests, quality checks, the doc build, and `uv build`,
  commit `Release X.Y.Z`, and create the lightweight tag `vX.Y.Z`. Do not push or publish.

## Working rules

- Make the smallest cohesive change; no pass-through wrappers or speculative specialization.
- Prefer black-box tests at the narrowest stable public boundary; bug fixes cover the reproduced
  failure, intended result, and important rejection paths (see `design/DESIGN.md` for the full policy).
- Mock external uncertainty, not the core behavior under test; keep tests deterministic and fast.
- Keep `CHANGELOG.md` aligned with notable project changes, including internal work.
- Never rebuild or re-sync the project `.venv` (no `uv run --python X` / `uv sync --python X`
  with a different interpreter): the developer's own `wizolt` process runs out of it, and swapping
  it mid-session deletes modules under that process and crashes it. For cross-version testing,
  point `UV_PROJECT_ENVIRONMENT` at a throwaway directory instead.
