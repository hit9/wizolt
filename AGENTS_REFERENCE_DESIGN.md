# Global AGENTS.md and references

## Goal

Use AGENTS.md as wizolt's one plain-text, cross-session store for durable user instructions. A
user can read or edit it as a normal file, ask wizolt to update it, and cite either an entire
instructions file or a Markdown section with `@agents.md:`. There is no separate MEMORY.md
feature, memory index, memory ID, or memory-specific model tool.

This is for durable instructions and preferences (for example, how to write PR bodies), not an
automatic record of conversations. Wizolt may suggest a concise rule when a stable preference
emerges, but writes only after the user explicitly asks to save, change, or remove it. A normal
answer must not silently update AGENTS.md.

## Sources and context

- Load `<data_dir>/AGENTS.md` as the global source. Keep the existing project source:
  `AGENTS.md` in the session's cwd, falling back to `CLAUDE.md`. Do not add project memory files.
- At session start, put global instructions before project instructions in the fixed context
  prefix. Project instructions win on conflict. The two sources share the existing 8,000-token
  AGENTS.md budget; unused space from one source can go to the other. Mark truncation and show
  the file path so the model can read the full source when needed. The prefix stays fixed during
  the session; a new session picks up edits automatically.
- The agent may proactively use or read applicable AGENTS.md content without asking permission.
  A reference is an explicit focus for one request, not a new instruction priority level.

## `@agents.md:`

The bare `@` menu offers `agents.md:` alongside file, MCP, and skill. Typing `@agents.md:` opens
a bounded menu: "All applicable", then each available file followed by its Markdown sections in
file order. Label the groups with both scope and visible path, for example
`Global · ~/.wizolt/AGENTS.md` and `Project · ./AGENTS.md`. If the project's fallback file was
loaded, show `Project · ./CLAUDE.md` instead. A file row selects the whole file; a section row
selects that section. Show the original heading and a short excerpt of the original text on
section rows. Do not show generated IDs or paraphrase the content. An unheaded file still has a
file row. A custom data directory is shown by its actual path instead of `~/.wizolt`.

Completion inserts quoted, readable references, for example:

- `@agents.md:` — all applicable instructions; this bare form is valid when submitted.
- `@agents.md:"global"` — the global file.
- `@agents.md:"project"` — the current project's AGENTS.md or CLAUDE.md.
- `@agents.md:"global/PR body"` — one section. Nested headings use their visible heading path,
  for example `global/Contributing/PR body`.
- `@agents.md:"project/Build"` — a section in the project file.

The scope word in the inserted reference distinguishes files even if both contain a heading of
the same name. File paths stay visible in the menu, while the reference remains short and stable
if the data directory changes or a project switches between AGENTS.md and CLAUDE.md.

Parse normal ATX Markdown headings (`#` through `######`) outside fenced code. A section includes
its heading and following text through the next heading of the same or higher level. Text before
the first heading belongs to the file row. Search menu rows by source, heading path, and original
text. If two sections have the same source and heading path, do not guess: say that the reference
is ambiguous and ask for distinct headings. Unknown references also get an explicit error.

On send, resolve a reference against the current file and attach the selected original text to
that request, with a shared 8,000-token cap for all AGENTS references in the request. If clipped,
say so and include the path for a full Read. Preserve the expansion in the saved turn so replay
does not reinterpret it after a later file edit. Referencing global instructions does not override
conflicting project instructions or the user's current request.

## File access and update behavior

Use the existing Read and Edit tools. Allow Read of the exact global AGENTS.md path without an
outside-workspace prompt; allow Edit of that exact path through the existing write confirmation
flow. Do not add AGENTS-specific Edit formats, validators, or a new model tool. Keep the ordinary
file-view and edit recovery behavior for this path. The project file already follows normal
workspace file rules.

The explicit-request rule is an agent behavior rule, not a security guarantee: a general Bash
tool or unattended write mode can bypass it. Do not claim otherwise. Suggesting a rule does not
count as permission to write it.

## Migration and scope

Remove the branch's MEMORY.md catalog, `@mem:` completion/expansion, `/memory` command, and
related runtime state. Do not delete a user's existing `<data_dir>/MEMORY.md`: leave it untouched
so the user can review and move suitable rules into global AGENTS.md. The unrelated Recall and
RecallContext tools in `wizolt/tools/memory.py` remain.

No new slash command is needed. Users view all rules by opening the plain AGENTS.md files, and
the `@agents.md:` menu provides section discovery. Existing project AGENTS.md behavior remains
supported. Update user docs and changelog to describe the final behavior, not the discarded
MEMORY.md design.
