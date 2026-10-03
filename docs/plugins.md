# Plugins

Make wizolt yours with small Python plugins: a companion above your input, custom statusbar
fields, extra `/status` information, or your own commands and tools.

Ask the agent to use **plugin-workshop** to create or change a plugin. The skill is built in;
it includes the matching SDK reference and runnable examples.

## Try the pet

Open `/plugins`, select **pet**, and choose **enable**. The little cat sits above the input,
animates while the agent works, and changes mood when your input is needed. Its colors follow
your theme. It is installed but **disabled by default** and makes no model calls.

```text
 /\_/\
( • • )~  on it

❯ Your next message
```

Choose **disable** to hide it. Disabling keeps a plugin installed so you can enable it again.

## Manage your plugins

`/plugins` shows installed plugins, enablement, actual runtime status, and source paths.
Move with **↑/↓** or **j/k**, press **Enter** for actions, and **Esc** to go back.

| Action | What it does |
| --- | --- |
| Enable / Disable | Turn an installed plugin on or off |
| Reload | Load changes from its source file |
| Rollback | Restore its previous in-memory version |

Enablement is saved per project. Changes affect the current agent and future agents; existing
agents keep their own instances. A change requested during a turn shows **pending** until that
turn ends. Failed reloads keep the old version working.

## Create one

Keep personal source in `~/.wizolt/plugins/name.py`, or project-specific source in
`.wizolt/plugins/name.py`. Files are only loaded after explicit enablement.

```python
from wizolt.sdk import Plugin

SDK_VERSION = 1

def setup(plugin: Plugin) -> None:
    plugin.field("mood", lambda context: "busy" if context.status == "running" else "ready")
```

Save as `mood.py`, then ask the agent to install it using **plugin-workshop**. Use `{plugins.mood.mood}` in a
statusbar or divider [format](appearance.md). The same SDK supports theme-aware components
above the input and on `/status`'s Session tab.

For installed plugins: `/plugins enable mood`, `/plugins reload mood`, and
`/plugins disable mood`. The agent uses the `Plugin` tool for these operations.

## Dependencies and recovery

A plugin can declare `DEPENDENCIES = ["package>=1.0"]`. Ask the agent to install it through
**plugin-workshop**. wizolt builds a separate
environment and returns a restart command. Save your session, exit, then use that command with
your usual `--config` and `--resume` arguments. Your running environment is unchanged.

That environment borrows your current wizolt installation; keep the installation available.
Conflicting dependencies must be resolved before activation.

Plugins execute trusted Python with your permissions. Only enable code you trust. To start
without loading plugins, run `WIZOLT_NO_PLUGINS=1 wizolt`, then disable the problematic plugin
in `/plugins`. SDK 1 is experimental.
