---
name: plugin-workshop
description: Create, edit, install, and manage wizolt Python plugins for personal UI and workflow customization.
---

# Plugin workshop

Use configuration when a format string is enough. For Python, read [SDK.md](SDK.md), including
the pet and tokenweather examples. `/plugins` is the user's interactive manager.
For the bundled pet, simply enable `pet` by name; it is installed but disabled by default.

1. **Inspect:** `Plugin(action="list")`, then `inspect` by name. Edit the returned source path.
2. **Create:** default to `~/.wizolt/plugins/<name>.py`; use `<project>/.wizolt/plugins/<name>.py`
   for project-specific code. Use an ASCII identifier filename and absolute tool paths.
3. **Check:** `Plugin(action="validate", target="/absolute/path/name.py")`. Exercise callbacks,
   errors, narrow screens, and agent-state isolation. Validation executes trusted Python.
4. **Activate:** `enable` by path; after editing, `reload` by name. If `DEPENDENCIES` are declared,
   use `install` by path; follow its restart command with the user's `--config`/`--resume` options.
5. **Verify:** inspect actual status and report the change, tests, and undo action. Pending or
   restart-required does not mean active.

Installation enables by default. `disable` keeps the source and installation; `enable` by name
restores it. `rollback` restores the previous in-memory version. Files never auto-load merely
because they exist. Enablement is per project; changes affect this agent and future agents.

Keep mutable state inside `setup`, use theme roles, and keep render callbacks cheap and pure.
Do not print to the terminal, mutate host internals, create unmanaged tasks, or install packages
into the running interpreter. No desktop notifications. Use the manager, not direct registry edits.
