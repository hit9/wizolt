# Interception reference

`plugin.intercept(operation, handler, *, match=None, response=None)` wraps one of wizolt's own
operations, so a plugin can change how wizolt works, not only add things beside it.

```python
from wizolt.sdk.operations import Refusal, ToolResult

SDK_VERSION = 1


def setup(plugin):
    async def guard(context, call, next):
        if "rm -rf" in call.arguments.get("command", ""):
            return Refusal("I don't run recursive deletes")
        return await next(call)

    plugin.intercept("tool.call", guard, match={"tool": "Bash"})
```

## The contract

- `await next(value)` runs the rest of the chain and then wizolt itself, and returns its result.
  Run code before and after it, change the value with `value.replace(...)`, or return a result
  without calling it.
- `next` is single-use and belongs to this handler call. Await it inside the handler; it fails
  if called twice, concurrently, or after the handler returned.
- Refuse with the operation's typed `Refusal(reason)` **before** calling `next`. Raising an
  exception means your interceptor failed: the operation stops and this registration is
  blocked until you reload or disable the plugin. It is never skipped silently.
- You cannot hide a later failure: if something downstream fails, the operation fails even if
  your handler catches the error.
- Read-only fields (identities, purposes, origins) cannot change; wizolt checks every step.
- One registration per operation and plugin. Put several cases in one handler.
- `match` is a prefilter on read-only fields. Values that don't match never reach your worker.
- Several plugins form one chain in one user-level order, the first outermost; unlisted plugins
  follow by name. Manage it for the user with `wizolt plugin order list`,
  `wizolt plugin order move NAME --before OTHER` (or `--after`) and `wizolt plugin order reset`,
  then apply it with `Plugin(action="reload")`. It is saved as
  `[plugin_manager.interception] order`, separate from settings and component layout.
- Each handler has 60 seconds of its own time; waiting for `next` or for the user does not count.
- Handlers run concurrently and can re-enter your plugin (for example, a nested tool call made
  by wizolt). Never hold your own lock across `await next(...)`.
- Views (`plugin.ui`) are available to `prompt.submit` and `tool.call` handlers only. Settings
  writes and layout changes stay with commands and tools. `plugin.models.complete` works in any
  handler and skips your own interceptor, so it cannot recurse into itself.

Operation types live in `wizolt.sdk.operations`.

## `prompt.submit`

`Prompt(text, original, attachments, origin)` → `Prompt` or `Refusal`. Match on `origin`:
`user` (a turn's opening input), `followup` (typed while the agent works) or `child` (a parent
model's message to this subagent; never a command).

- Runs once per submitted item, after slash commands (which never reach you) and before the
  `UserPromptSubmit` shell hook, skills and `@` mentions, which all see your effective text.
- The model gets your text; the user's history keeps `original`.
- Image labels such as `[Image #1 · a.png]` appear in `text`. You may drop names from
  `attachments`, never add any.
- A `Refusal` stops a turn's opening input; a refused follow-up is withheld once and reported.

## `context.compose`

`Blocks(blocks, purpose)` → `Blocks`. Match on `purpose` (`turn`). Runs for each agent-step
request, after compaction, before `model.request`.

| Block | You may |
| --- | --- |
| `system` | Replace the instruction text (language and attribution directives stay wizolt's) |
| `environment`, `skills`, `mcp` | Read |
| `instructions` | Replace the project/user instructions for this request |
| `conversation` | Read a bounded view (recent messages, clipped); history itself is never edited |

- `blocks.with_text(id, text)` edits an editable block; `blocks.add(id, text)` adds your own
  block, which wizolt names `plugin:<your name>:<id>` and places after the header, before the
  conversation, in plugin order. You cannot change or remove another plugin's blocks.
- Changing the prefix costs provider cache reuse on every request where it differs. Keep your
  text deterministic: no timestamps or random values unless you mean to pay that.
- If the composed request no longer fits the context budget, the request fails rather than
  being silently truncated.

## `model.request`

`ModelRequest(id, purpose, reason, provider, model, effort, tools, message_count, retry_of)` →
`ModelResponse(text, tool_calls, provider, model)` or `Refusal`. Match on `purpose`
(`turn`, `vision`, `compaction`, `plugin`) and `reason` (`normal`, `image_fallback`,
`tool_correction`).

- Only `provider` (a configured entry), `model` and `effort` may change, for this request only.
  Changing just `provider` uses that entry's own model. Wizolt checks the route exists, has
  credentials and fits the context; credentials never reach your plugin.
- Instruction text is not editable here; use `context.compose`.
- `response="preserve"` (default) routes and must return `next`'s response unchanged: the user
  keeps live streaming and no token passes through your worker. `response="replace"` may return a
  different or synthetic response; while it is in the chain the user sees progress, not a
  streaming draft. Returned tool calls must name tools the request offers.
- Transport retries happen inside `next` with your transformed request. A manual retry starts a
  new request whose `retry_of` is the previous `id`: key side effects to these IDs or make them
  idempotent, because your handler runs again.
- `plugin.models.complete` from inside your handler is a `plugin`-purpose request that skips
  your own registration; other plugins' interceptors still apply.
- A `Refusal` fails the request before anything is sent.

## `tool.call`

`ToolCall(id, tool, arguments)` → `ToolResult(content, status="ok"|"failed")` or `Refusal`.
Match on `tool`. Only `arguments` may change; `id` and `tool` are fixed.

- The call you pass to `next` is the one wizolt validates, shows for approval, gives to
  `PreToolUse` hooks and runs. A rewritten `ls` that became `rm` is approved as `rm`.
- `ToolResult.content` from `next` is the tool message the model would see. You may rewrite it.
  Shell-hook feedback stays attached by wizolt; you cannot remove it.
- Returning a `ToolResult` without calling `next` answers from your plugin: wizolt shows it as
  `[plugin <name>]`, does not count it as an executed tool, and fires no `tool.started`.
- A `Refusal` answers that one call; the rest of the model's batch still runs.
- ToolScript's nested calls reach the same interceptor.
- If your handler fails after the tool ran, the tool's actual output is kept and your failure is
  named beside it. Wizolt records each intercepted call's actual outcome separately from what was
  delivered.
