# Plugins

Plugins are either built in or installed by you. Install and enable once, then use them
in any directory.

Make wizolt yours: describe what you want, and let the agent build a plugin for you.
You do not need to write Python or edit configuration files yourself.

Your plugin settings, enabled choices, shortcuts and layout are saved in `config.toml`.
Sync it to keep your preferences on another computer; copy or reinstall your plugin source
and dependencies there too. Cached data does not need to travel with your configuration.

![A context meter above the divider, a pet above the input and token counts in the statusbar.](_static/plugins-overview.svg)

## Start with a wish

Tell the agent what you want to see, where it belongs, and when it should appear:

> Use plugin-workshop to add a context bar above my input. Give each category its own
> color, match my theme, and hide the legend when the terminal is short.
> Let me toggle it with /context-bar.

The built-in **plugin-workshop** skill helps the agent build and preview it. Approve the
reload to try it without restarting.

> Let me choose the pet's appearance from a menu and remember my choice for next time.
> Test choosing, cancelling and reopening before loading it.

Plugins can save their own settings. The agent can test menus with scripted answers and show
you previews. Trials use the same menu defaults and form checks as the live interface, with a
temporary config so your saved choices stay unchanged.

![Write, preview, approve and load a plugin live.](_static/plugins-workflow.svg)

Keep refining it in plain language:

> Make the bar quieter. Show token counts beside the categories, and only show it while working.

Type `@plugin:` to name an installed plugin. Mentioning it does not enable or run it.

## Things to ask for

### See your context

> Show a stacked bar above the divider: one color for each part of my context window,
> with a compact legend underneath.

![A context bar split by category, with token counts underneath.](_static/plugins-meter.svg)

### Track tokens and activity

> Show this session’s input and output token counts in my statusbar, using compact
> numbers like 42k. Match my current theme.

![A statusbar with the model, input and output token counts.](_static/plugins-tokens.svg)

> Show a small token-speed wave below my input, plus the current tool and this turn's
> tool-call count. Keep it to two lines.

### Add your own commands and abilities

> Add /note to save project notes. Let the agent search those notes when they help with a task.

![The agent reads saved project notes through a plugin.](_static/plugins-tool.svg)

You can also ask for a new color theme, statusbar or divider style. Plugin styles appear in
`/theme`. Panels can sit above the divider, above or below the input, or inside `/status`.

### Give your plugin an interface

> Add a searchable task list. Enter opens the selected task's full-screen log;
> Esc takes me back. Ask for confirmation before stopping a task.

![A task list opens a full-screen log; Escape returns to the list.](_static/plugins-views.svg)

Plugins can ask for text, show forms and single/multiple-choice menus, and update a live
viewer. Your input draft remains intact when you return.

> Bind F6 to my task list. Check for conflicts first.

Ask the agent to manage shortcuts through the built-in **layout** plugin. Bindings survive
restarts and become inactive when their plugin is disabled.

### Change how a step works

Plugins can step into what you send, the model requests, tool calls and summaries:

> When my message starts with `fix:`, expand it into my usual bug-fix checklist. Keep what I
> typed in my history.

> Send image descriptions and summaries to my cheaper `[provider.fast]` model.

> Before any Bash command that deletes files, show me the command and ask first.

> Add my project's open decisions from `DECISIONS.md` to every request.

> Summarize with my local model when the conversation is compacted.

wizolt's own checks still apply: a rewritten tool call still asks for approval, and your history
keeps what you typed. Adding text to every request costs tokens on each turn and can reduce
provider cache reuse. A plugin that calls a model is billed like any other request.

If such a plugin breaks, the step it wraps stops with an error naming it, rather than quietly
skipping it. Disable or reload it in `/plugins` to continue. When two plugins wrap the same
step, the first in `wizolt plugin order list` runs first; ask the agent to reorder them.

### Redraw tool results and activity

> Show Bash results as the command plus the first line of output, green when it passed.

> Replace the running-reply preview with a one-line status: current tool and elapsed time.

A plugin can redraw a finished tool call, its result summary, or the activity line while the
agent works. Approvals and the stored-result key (`tr.N`) stay wizolt's. If two plugins want
the same spot, wizolt keeps its own drawing until you pick one with
`wizolt plugin presenter choose`. A slow or failing redraw falls back to wizolt's.

## Try the built-in pet

Open `/plugins`, select **pet**, then **enable**. A rainbow pet strolls along your prompt line
while the agent works and rests when it stops. Type `/pet` to choose a cat, dog, rabbit, frog,
dragon or robot; your choice is saved. It starts disabled and makes no model calls.

![The pet working, waiting for input and finishing.](_static/plugins-pet.svg)

> Move @plugin:pet above the divider and reload it.

## The built-in context bar

Enable **context_bar** in `/plugins` to see what fills your context window above the input: one
colored segment per category (system prompt, tools, memory files, skills, messages), the
percentage used at the end, and a legend with token counts. In a short terminal only the bar
remains. It makes no model calls.

## Arrange your space

> Enable the built-in layout plugin. Put my context meter before my pet and leave one
> empty line between them.

> Remove that empty line. Keep the current order.

Order and spacing survive restarts. Disabling **layout** keeps your choices.
Without a saved order, plugins appear in name order.

Panels share up to six lines, fewer in short terminals. Gaps shrink to fit.
If `/plugins` shows a panel as hidden or clipped, ask for a more compact design.

## Manage and recover

If a plugin makes the session unusable, start **`wizolt --no-plugins`**. It skips plugin
code without deleting anything. Disable the broken plugin in `/plugins`, then restart normally.

Open **`/plugins`**, select a plugin and press **Enter**:

The list shows its source (`builtin` or `user`), whether it is enabled, and its current state.

| Action | What you get |
| --- | --- |
| Enable / Disable | Turn it on or off across projects |
| Reload | Apply its latest code and settings |

To undo a change, restore the earlier source from Git and reload; wizolt keeps no old versions.
If you delete a plugin's `[plugin_manager.installations.NAME]` table from `config.toml`, the next
reload stops it in running agents too.

The state is **running**, **failed** or **off**. During a turn, or while that plugin's command
is running, a change waits and shows what it waits to do: **starting**, **reloading** or
**stopping**. Commands in other plugins do not hold up the update. A failed update or
settings save leaves the current plugin unchanged. You can ask the agent to manage it too:

> Update @plugin:pet, preview the change, then reload it. Keep its source in its own Git
> repository so I can undo changes later.

Only enable code you trust: plugins can access your files and network.
Keep secrets out of plugin source; ask the agent to use environment variables.
