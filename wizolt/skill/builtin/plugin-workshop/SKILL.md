---
name: plugin-workshop
description: Create, edit, install, and manage wizolt Python plugins for personal UI and workflow customization.
---

# Plugin workshop

Use configuration when a format string is enough. For Python, read
[SDK.md](${SKILL_DIR}/SDK.md) before writing code: public API, limits and runnable examples. For
colors or bar formats, also read [APPEARANCE.md](${SKILL_DIR}/APPEARANCE.md). Start from these
references; host internals are not plugin APIs. `wizolt plugin paths` prints the same paths.
For prompts, selectors, full-screen viewers, live updates, notifications or shortcuts, read
[UI.md](${SKILL_DIR}/UI.md): every interaction API, value, limit and cancellation rule.

Run the CLI as `"${WIZOLT_EXECUTABLE:-wizolt}" plugin ...`: that is this session's own wizolt,
even when another is first on PATH. Its config and project default to this session's, even
after `cd`. Installations, enable/disable and layout are user-level across directories;
preferences live in `config.toml` under `[plugin_manager]` (installations, layout and shortcuts).
Plugin-specific settings remain in `[plugins.NAME]`; never put host fields there. The project only
sets execution context. `/plugins` is the user's interactive manager. Bundled plugins (`pet`,
`layout`) are installed but always off until the user enables one by name. The optional built-in `layout` plugin exposes list/move/gap/reset tools:
enable and reload it to arrange components and spacing for the user. `/plugins` manages lifecycle only.
It also supplies shortcut list/bind/unbind operations; configure keys for the user through
`Plugin`, checking conflicts and asking before replacing occupied keys.

1. **Inspect:** `plugin list`, then `plugin inspect NAME`. Edit the returned path.
2. **Create:** strongly prefer one Git repository per user plugin, at `~/.wizolt/plugins/<name>/`.
   Put `<name>.py` inside for a single-file plugin, or use a `pyproject.toml` package (see SDK.md).
   Pass the `.py` file to CLI commands, or the repository directory for a package.
   The installed NAME is the file stem, or the project name with `-`/`.` turned into `_`; it names
   the `[plugins.NAME]` config table and `{plugins.NAME.FIELD}` fields.
3. **Check:** `plugin validate PATH`, then `plugin test PATH --theme forest --width 80`. Read the
   JSON report and view each PNG: components and contributed statusbar/divider presets. Test a
   narrow width too; `--times 0 0.5 1` samples animation. Trials run trusted Python for real.
   Use `--call command:NAME --interactions answers.json` for scripted dialogs and temporary
   settings writes; [TESTING.md](${SKILL_DIR}/TESTING.md) defines fixtures and report fields. Inspect
   `svg_path`/`png_path` file contents, not just their paths.
   Handlers calling `models.complete`, layout or shortcut services need a live session: validate offline,
   then reload and test through `Plugin call`. “Host services unavailable in offline trials”
   is that boundary, not evidence your handler is broken. Plugin-owned `service()` resources work offline.
   With `DEPENDENCIES`, run step 4's `enable` first; then test by installed NAME.
4. **Save:** `plugin enable PATH`. This saves a preference and prepares a worker environment
   when declared dependencies need one; it does not activate anything.
5. **Activate:** call `Plugin(action="reload", name="NAME")`; omit name to apply all saved choices. It
   affects this agent only; `starting`/`reloading`/`stopping` wait for the current turn or this plugin's active call. A failure includes `traceback`
   and `log`; the previous version keeps running.

`plugin disable NAME`, then a reload, turns a plugin off without deleting it. `/plugins`
also offers rollback. Files never load just because they exist.

Keep source, tests and non-secret metadata in Git; exclude secrets, virtualenvs and trial output.
Review the diff and checkpoint tested changes with commits under the user's Git instructions.
Git preserves development history; hot-reload rollback only retains the previous live revision.
A local repository is enough: sharing and remote hosting are optional.

**What a plugin can add:** UI (fields, components, themes, presets), slash commands for the user,
and tools for you. Register `plugin.tool` when the user wants *you* to gain an ability; after
activation, use `Plugin` actions `list`, `describe`, then `call` (the user approves calls).
`plugin.tool` does **not** add a standalone model tool. Do not call its bare name or look for it
in ToolScript's tool table; its model-facing entry point is the existing `Plugin` gateway.
`@plugin:NAME` is a reference only. Use `Plugin(action="list", name="NAME")` to discover its
current tools, or CLI `plugin inspect NAME` for saved installation state; mentioning does not enable it.
Commands are typed by the user; you cannot run them.

Keep mutable state inside `setup`, use theme roles, and keep render callbacks cheap and pure.
Use `context.layout.rows` to fit the remaining height, and `context.turn.tools` / `active_tools`
for real execution counts and activity. Session/tool lifecycle events are read-only; see SDK.md.
Settings go in `[plugins.NAME]` of wizolt's config; declare them with `plugin.configure(...)` and
save interactive choices with `await plugin.settings.update(values, reset=[...])` (see SDK.md).
Apply the returned settings yourself or reload; `plugin.config` stays frozen.
Use `plugin.service` for connections, `plugin.models.complete` for model
requests and `plugin.summarizer` for compaction; tell the user about model costs first. Do not
mutate host internals, create unmanaged tasks, install into the running interpreter, or send
desktop notifications.

Use host-owned views, never terminal escapes or prompt-toolkit widgets. Dialogs belong to
explicit actions, never background observers. If recovery is needed, `wizolt --no-plugins`
skips all plugins for one launch without deleting their preferences; disable/fix and restart.

If the reference is unclear, read the `sdk` directory from `plugin paths`, then `source` for
`plugins/worker.py` and `plugins/runtime.py`. Keep plugins on the documented API.

Complete starting points: [EXAMPLES.md](${SKILL_DIR}/EXAMPLES.md).
