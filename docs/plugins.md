# Plugins

Make wizolt yours with small Python plugins: a companion above your input, custom statusbar
fields, extra `/status` information, or your own commands.

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

To move the pet above the divider, add this to `config.toml`, then reload it:

```toml
[plugins.pet]
slot = "above_divider"
```

Plugins can draw above the divider, above or below the input, and inside `/status`. Prompt
additions share at most six rows and shrink in short terminals. They can use multicolor rows,
context categories and token-speed samples; ask plugin-workshop for a context bar or speed graph.

## Manage your plugins

`/plugins` shows installed plugins, enablement, actual runtime status, and source paths.
Move with **↑/↓** or **j/k**, press **Enter** for actions, and **Esc** to go back.
Columns stay aligned when names are long or the terminal shrinks. The statusbar shows
`plugins N` for healthy plugins loaded in this agent; zero is hidden. Custom formats can use
`{plugins.count}`. A pending disable stays counted until the current turn finishes.

| Action | What it does |
| --- | --- |
| Enable / Disable | Turn an installed plugin on or off |
| Reload | Load changes from its source file |
| Rollback | Restore its previous in-memory version |

Enablement is saved per project. Changes affect the current agent and future agents; existing
agents keep their own instances. A change requested during a turn shows **pending** until that
turn ends. Failed reloads keep the old version working.

## Plugin settings

Put settings in your wizolt `config.toml`, under the installed plugin's name:

```toml
[plugins.context_helper]
provider = "deepseek"
model = "your-model"
```

The plugin defines which settings it accepts. Run `wizolt plugin validate NAME --config PATH`
to check them, then use `PluginHotReload` to apply them to the current agent. Invalid settings
leave the previous instance running. Rollback restores its previous source and settings.
Keep credentials in environment variables or your provider configuration.

## More ways to customize

| Capability | What you get |
| --- | --- |
| Themes and bar presets | New `plugins.NAME.CHOICE` entries in `/theme`; reload updates them live |
| Model helpers | Commands can ask a configured model using only the text the plugin supplies |
| Compaction | One enabled summary plugin can serve `/compact` and automatic compaction |
| Services | Connections such as LSP start on first use and close on disable, reload or exit |

Model helpers use extra tokens, have a 50-second limit, and return their usage separately from
the main agent's statistics. Model-backed summaries can also lose the main model's cached-prefix
savings. If a summary plugin fails, wizolt uses built-in compaction; fix and reload it to retry.
Your notes and recent messages remain protected.

Disabling a theme plugin temporarily uses the default appearance; enabling it restores your
saved choice. Each agent has its own active plugins and appearance choices.

## Create one

Keep personal source in `~/.wizolt/plugins/name.py`, or project-specific source in
`.wizolt/plugins/name.py`. Files are only loaded after explicit enablement.

For multiple modules and resource files, use a directory with `pyproject.toml` and a configurable
entry callable. Pass that directory to the same `validate`, `test`, and `install` commands.
Reload and rollback include its helpers and resources.

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
`/plugins disable mood`. Plugins can also add their own `/commands`.

## Try changes before enabling

```sh
wizolt plugin validate ~/.wizolt/plugins/mood.py
wizolt plugin test ~/.wizolt/plugins/mood.py --theme forest --width 80
```

The test returns a JSON report with errors, captured logs, and PNG/SVG previews for UI components.
Use `--width 30` to check a narrow terminal, or `--times 0 0.5 1` to sample animation. Previews use
the same text clipping and colors as the TUI. Use `--font` if the PNG font lacks your characters.

`wizolt plugin list` and `inspect NAME` show saved project choices. `enable PATH` and `disable NAME`
save changes for new agents. Ask the current agent to call **PluginHotReload** to apply them now;
it reloads one named plugin or all saved choices. A failed candidate keeps the old version.
Use `--config PATH` and `--project DIR` when your running session uses different defaults.

Trials never change installation choices. They run real plugin code with your permissions;
an explicitly tested command or event can still modify files or access the network.

## Dependencies and recovery

A plugin can declare `DEPENDENCIES = ["package>=1.0"]`. Ask the agent to install it through
**plugin-workshop**, or run `wizolt plugin install PATH`. wizolt builds a separate worker
environment. Apply it with **PluginHotReload**; no wizolt restart is needed.

That environment borrows your current wizolt installation; keep the installation available.
Conflicting dependencies must be resolved before activation.

Each plugin runs in its own process. A crash or blocking callback is reported without freezing
wizolt's input. Plugins still execute trusted Python with your permissions. To start
without loading plugins, run `WIZOLT_NO_PLUGINS=1 wizolt`, then disable the problematic plugin
in `/plugins`. SDK 1 is experimental.
