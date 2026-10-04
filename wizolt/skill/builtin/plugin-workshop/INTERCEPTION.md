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
- Several plugins form one chain in the user's order, the first outermost. The user can change
  it with `wizolt plugin order`.
- Each handler has 60 seconds of its own time; waiting for `next` or for the user does not count.
- Handlers run concurrently and can re-enter your plugin (for example, a nested tool call made
  by wizolt). Never hold your own lock across `await next(...)`.
- Views (`plugin.ui`) are available to `prompt.submit` and `tool.call` handlers only. Settings
  writes and layout changes stay with commands and tools. `plugin.models.complete` works in any
  handler and skips your own interceptor, so it cannot recurse into itself.

Operation types live in `wizolt.sdk.operations`.
