---
name: plugin-workshop
description: Create, edit, install, and manage wizolt Python plugins for personal UI and workflow customization.
---

# Plugin workshop

Use configuration when a format string is enough. For Python, read [SDK.md](SDK.md), including
the pet and tokenweather examples. `/plugins` is the user's interactive manager.
For the bundled pet, simply enable `pet` by name; it is installed but disabled by default.

1. **Inspect:** `wizolt plugin list`, then `wizolt plugin inspect NAME`. Edit the returned path.
2. **Create:** default to `~/.wizolt/plugins/<name>.py`; use `<project>/.wizolt/plugins/<name>.py`
   for project-specific code. Use an ASCII identifier filename and absolute tool paths.
3. **Check:** `wizolt plugin validate PATH`, then `wizolt plugin test PATH --theme forest --width 80`.
   Read the JSON report and view its PNG. Test a narrow width too; `--times 0 0.5 1` samples animation.
   Trials execute trusted Python in a temporary process, with real filesystem/network access.
4. **Save:** `wizolt plugin enable PATH` (or installed NAME); use `install PATH` for declared dependencies.
   These commands save preferences, not live activation. Use the running wizolt's `--config` and
   `--project` when different from the defaults. `test` and `validate` also accept installed names.
5. **Activate:** call `PluginHotReload(name="NAME")`; omit name to apply all saved choices.
   It affects this agent only. Pending means activation waits for the current turn to finish.

Installation enables by default. `wizolt plugin disable NAME`, then `PluginHotReload`, hides it
without removing its source. `/plugins` also offers live controls and source rollback. Files never
auto-load merely because they exist. Saved choices apply to future agents in this project.

Keep mutable state inside `setup`, use theme roles, and keep render callbacks cheap and pure.
Put user settings in `[plugins.NAME]` in wizolt's `config.toml`. Declare validation and defaults
with `plugin.configure(...)`, then read immutable `plugin.config` (see SDK.md). Validate using
the same `--config` as the live agent, and hot reload after editing settings.
Do not mutate host internals, create unmanaged tasks, or install packages
into the running interpreter. No desktop notifications. Use the manager, not direct registry edits.
