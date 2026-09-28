"""Self-contained helpers: standard library only, no knowledge of wizolt.

A module lives here when it imports nothing from the rest of wizolt and answers one plain question
through one owning class: what a malformed JSON object meant (`json_repair.JsonRepair`), what an
image header says (`image_header.ImageHeader`), which paths a .gitignore excludes
(`gitignore.GitIgnore`), where a directory sits in its project (`workspace.Workspace`), or what a
short shell command printed (`process.ShellCommand`). It must not know about sessions, model
clients, or configuration; a module that needs those belongs in the layer that consumes it.
"""
