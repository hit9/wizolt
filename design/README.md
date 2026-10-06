# Maintainer notes

These documents describe implementation decisions and invariants. User documentation lives in
[`docs/`](../docs/); the repository's agent entry point is [`AGENTS.md`](../AGENTS.md).

- [Design](DESIGN.md): orientation, module boundaries and durable decisions.
- [State ownership](STATE_OWNERSHIP.md): main/subagent isolation, shared services and data lifetimes.
- [Known issues](KNOWN_ISSUES.md): accepted costs and unresolved terminal behavior.
- [Dependency review](DEPENDENCY_REVIEW.md): production dependency costs and alternatives.
- [Tools and prompts](TOOLS_AND_PROMPTS.md): user-owned tool residency/visibility and plugin
  prompt-file convention (design; not implemented).
- [Transcript appearance](TRANSCRIPT_APPEARANCE.md): transcript rows as format strings over a
  host-owned block structure, in a `/theme` Transcript tab; density levels become presets,
  per-tool overrides stay sparse; presenters unchanged above.
