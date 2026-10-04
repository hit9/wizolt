# Plugin interaction design

Plugins describe views; the host owns their rendering, input, focus and lifetime. Keep
native scrollback: transient inline modals and alternate-screen viewers are supported,
not persistent splits or arbitrary terminal drawing.

## Contract

- A view combines a title, a document/list/form, actions and an inline/fullscreen choice.
  Actions have stable IDs and optional view-local keys. Results carry IDs and entered values.
- `ui.input`, `confirm`, and `select` compose the same view contract. `ui.show` awaits one
  result; `ui.open` additionally gives the action a handle for live content updates.
- Each action opens one view at a time. Returning from a child view and reopening the parent
  is ordinary plugin control flow, not a second host navigation stack.
- Only explicit command/tool invocations may interact. Observers, rendering and compaction
  cannot interrupt the user. Noninteractive frontends and offline trials report unavailable.
- The host queues interactions, preserves drafts, and never overlays a pending approval.
  Cancellation, worker exit, disable and successful replacement dismiss owned views.
- Human interaction pauses the enclosing action's execution deadline. Worker code outside
  that interaction remains bounded; update requests keep ordinary RPC deadlines.

## Layers

`sdk.views`: immutable declarations, validation and convenience methods, no TUI imports.
`plugins`: request ownership, transport deadlines and generation retirement.
`ui`: view adapters over existing selectors/detail rendering and the modal owner.

Global shortcuts target registered commands. User-level saved bindings remain independent
of plugin settings; conflicts and protected input keys are checked by the host. View-local
bindings expire with their view. LLM-facing management uses existing plugin operations,
not a new settings screen or model tool. Help is derived from effective actions.

## Verification

Exercise real worker RPC as well as real TUI input. Cover cancellation at every waiting
boundary, approval contention, agent isolation, drafts, reload, malformed declarations,
dynamic updates, narrow terminals and shortcut conflicts. Interactive waits must outlive
the normal action deadline without allowing a stalled renderer or worker to hang shutdown.
Run the tmux and Zellij acceptance suites for terminal ownership changes. Document author
contracts in the packaged SDK reference and user workflows, without code, in `docs/plugins.md`.
