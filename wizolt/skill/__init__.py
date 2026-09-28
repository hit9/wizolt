"""wizolt skills: Markdown instruction packs loaded on demand.

`skillfile` reads one SKILL.md into a `Skill`; `discovery` finds them on disk; `trust` records
which repositories may run their own; `invocation` starts one; `permissions` holds its
allowed-tools rules; `library` is the session's collection and `listing` what its model was told.

Empty of imports, like `agent/` and `ui/`: the tool layer reaches `listing` and `invocation` on
every import of the model stack, and must not assemble discovery, AGENTS.md and file mentions
with them. Import from the owning module.
"""
