"""See the whole system prompt with /prompt, and replace its instructions with your own file.

Your file replaces wizolt's instruction text in every request; the language, attribution and
reactions lines wizolt adds stay. Nothing is written until you save an edit, so enabling this changes
nothing by itself. A project's copy overrides your own. An edit applies from the next request;
each saved change costs the provider cache once. It makes no model calls.

## Use

- Enable **system_prompt** in `/plugins`.
- Type `/prompt` to read the system prompt as it is sent: the instructions, then each line a
  setting adds, under a gray rule naming that setting.
- Press `e` to edit the instructions in your editor (`$VISUAL` or `$EDITOR`): your file, or
  wizolt's own prompt when you have none yet. Saving a change writes
  `<data dir>/plugins/system_prompt/system.md`.
- For one project only, put a copy in `.wizolt/plugins/system_prompt/system.md` there.
- Delete the file to return to wizolt's own prompt.
"""

# Built only on the public SDK: `context.compose` substitutes the file verbatim, adding nothing,
# and `/prompt` shows it with `plugin.ui.show` and edits it through `plugin.ui.edit`. The file is
# read on every request rather than cached, so an edit applies without a reload, and unchanged
# bytes keep the cached prefix.

import os
from collections.abc import Mapping
from pathlib import Path

from wizolt.sdk import Context, Plugin
from wizolt.sdk.operations import Blocks
from wizolt.sdk.views import Action, Document, Section, View

SDK_VERSION = 1

FILE = "system.md"
MAX_BYTES = 256 * 1024  # Larger than any prompt worth sending; a runaway file is not read whole.


def read(path: Path) -> str | None:
    """The file's text, or None when it cannot serve as a prompt: unreadable, not UTF-8, empty
    or oversized. Wizolt's own prompt then stands; the file is never half-applied."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if text.strip() and len(data) <= MAX_BYTES else None


def write(path: Path, text: str) -> None:
    """Replace the file in one rename, so a request never reads half an edit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def setup(plugin: Plugin) -> None:
    """Default enablement belongs to the installation catalog, never to plugin source code."""

    def effective(context: Context) -> Path | None:
        return next((path for path in plugin.user_file_paths(context, FILE) if path.is_file()), None)

    async def compose(context: Context, blocks: Blocks, next):
        path = effective(context)
        text = read(path) if path is not None else None
        if text is not None and blocks.get("system") is not None:
            blocks = blocks.with_text("system", text)
        return await next(blocks)

    async def show(context: Context, arguments: Mapping[str, object]) -> str:
        """The system prompt as sent; `e` hands its instructions to the editor."""
        path = effective(context)
        own = read(path) if path is not None else None
        source = "wizolt's own" if own is None else "your file" if path == plugin.user_file_paths(context, FILE)[-1] else "this project's file"
        # The lines settings add follow whatever the instructions are; `e` cannot change them.
        sections = tuple(Section(f"added by {item.setting}", item.text) for item in await plugin.agent.system_directives())
        text = own or await plugin.agent.system_prompt()
        view = View(f"System prompt · {source}", Document(text, sections=sections), (Action("edit", "edit instructions", "e"),), fullscreen=True)
        if await plugin.ui.show(view) is None:
            return "System prompt unchanged"
        return await edit(context)

    async def edit(context: Context) -> str:
        paths = plugin.user_file_paths(context, FILE)
        path = effective(context)
        # Read again: the file may have changed while the view was open.
        original = (read(path) if path is not None else None) or await plugin.agent.system_prompt()
        edited = await plugin.ui.edit(original)
        if edited is None:
            return "The editor did not save; nothing was written."
        # Editors add or drop a final newline on their own; that alone is not an edit.
        if edited.rstrip("\n") == original.rstrip("\n"):
            return "No changes; nothing was written."
        if not edited.strip():
            return "Not saved: an empty prompt. Delete the file to return to wizolt's own."
        if len(edited.encode("utf-8")) > MAX_BYTES:  # Saved, it would be skipped on every request.
            return f"Not saved: a prompt is limited to {MAX_BYTES // 1024} KiB."
        target = path if path is not None else paths[-1]
        try:
            write(target, edited)
        except OSError as error:
            return f"Not saved: {error}"
        return f"Saved {target}; it applies from the next request."

    plugin.intercept("context.compose", compose)
    plugin.command("prompt", "Show the system prompt; e edits it in your editor", show)
