# Plugins

Make wizolt yours: describe what you want, and let the agent build a plugin for you.
You do not need to write Python or edit configuration files yourself.

![A context meter above the divider, a pet above the input and a cost field in the statusbar.](_static/plugins-overview.svg)

## Start with a wish

Tell the agent what you want to see, where it belongs, and when it should appear:

> Use plugin-workshop to add a context bar above my input. Give each category its own
> color, match my theme, and hide the legend when the terminal is short.
> Let me toggle it with /context-bar.

The built-in **plugin-workshop** skill helps the agent build and preview it. Approve the
reload to try it without restarting.

![Write, preview, approve and load a plugin live.](_static/plugins-workflow.svg)

Keep refining it in plain language:

> Make the bar quieter. Show token counts beside the categories, and only show it while working.

Type `@plugin:` to name an installed plugin. Mentioning it does not enable or run it.

## Things to ask for

### See your context

> Show a stacked bar above the divider: one color for each part of my context window,
> with a compact legend underneath.

![A context bar split by category, with token counts underneath.](_static/plugins-meter.svg)

### Track cost and activity

> Add the session cost to my statusbar. Ask me for my provider's token prices first.

![A statusbar with the model, context usage and session cost.](_static/plugins-cost.svg)

> Show a small token-speed wave below my input, plus the current tool and this turn's
> tool-call count. Keep it to two lines.

### Add your own commands and abilities

> Add /note to save project notes. Let the agent search those notes when they help with a task.

![The agent reads saved project notes through a plugin.](_static/plugins-tool.svg)

You can also ask for a new color theme, statusbar or divider style. Plugin styles appear in
`/theme`. Panels can sit above the divider, above or below the input, or inside `/status`.

## Try the built-in pet

Open `/plugins`, select **pet**, then **enable**. A small cat reacts to the agent above your
input. It starts disabled and makes no model calls.

![The pet working, waiting for input and resting.](_static/plugins-pet.svg)

> Move @plugin:pet above the divider and reload it.

## Arrange your space

> Enable the built-in layout plugin. Put my context meter before my pet and leave one
> empty line between them.

> Remove that empty line. Keep the current order.

Order and spacing survive restarts. Disabling **layout** keeps your choices.
Without a saved order, plugins appear in name order.

Panels share up to six lines, fewer in short terminals. Gaps shrink to fit.
If `/plugins` shows a panel as hidden or clipped, ask for a more compact design.

## Manage and recover

Open **`/plugins`**, select a plugin and press **Enter**:

| Action | What you get |
| --- | --- |
| Enable / Disable | Turn it on or off for this project |
| Reload | Apply its latest code and settings |
| Rollback | Restore the previous loaded version |

Changes during a turn show **pending** until it ends. A broken update keeps the old version
running. You can ask the agent to manage plugins and settings too:

> Update @plugin:pet, preview the change, then reload it. Keep its source in its own Git
> repository so I can undo changes later.

Only enable code you trust: plugins can access your files and network.
Keep secrets out of plugin source; ask the agent to use environment variables.

If a plugin causes trouble, start with `WIZOLT_NO_PLUGINS=1 wizolt` and disable it in `/plugins`.
