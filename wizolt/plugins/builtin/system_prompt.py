"""Replace the system prompt with your own file, edited with /prompt.

Your file replaces wizolt's instruction text in every request; the language and attribution
lines wizolt adds stay. Nothing is written until you save an edit, so enabling this changes
nothing by itself. A project's copy overrides your own. An edit applies from the next request;
each saved change costs the provider cache once. It makes no model calls.

## Use

- Enable **system_prompt** in `/plugins`.
- Type `/prompt`: your editor (`$VISUAL` or `$EDITOR`) opens on your file, or on wizolt's current
  prompt when you have none yet. Saving a change writes `<data dir>/plugins/system_prompt/system.md`.
- For one project only, put a copy in `.wizolt/plugins/system_prompt/system.md` there.
- Delete the file to return to wizolt's own prompt.
"""

# Built only on the public SDK: `context.compose` substitutes the file verbatim, adding nothing,
# and `/prompt` edits it through `plugin.ui.edit`. The file is read on every request rather than
# cached, so an edit applies without a reload, and unchanged bytes keep the cached prefix.

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from wizolt.sdk import Context, Plugin
from wizolt.sdk.operations import Blocks

SDK_VERSION = 1

FILE = "system.md"
MAX_BYTES = 256 * 1024  # Larger than any prompt worth sending; a runaway file is not read whole.


@dataclass
class Seen:
    """The system text the last request carried before substitution: what `/prompt` starts from
    when the user has no file yet."""

    system: str = ""


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
    seen = Seen()

    def effective(context: Context) -> Path | None:
        return next((path for path in plugin.user_file_paths(context, FILE) if path.is_file()), None)

    async def compose(context: Context, blocks: Blocks, next):
        system = blocks.get("system")
        if system is not None:
            seen.system = system.text
            path = effective(context)
            text = read(path) if path is not None else None
            if text is not None:
                blocks = blocks.with_text("system", text)
        return await next(blocks)

    async def edit(context: Context, arguments: Mapping[str, object]) -> str:
        paths = plugin.user_file_paths(context, FILE)
        path = effective(context)
        original = (read(path) if path is not None else None) or seen.system
        if not original:
            original = "Send a message first to start from wizolt's prompt, or write your own here.\n"
        edited = await plugin.ui.edit(original)
        if edited is None:
            return "The editor did not save; nothing was written."
        # Editors add or drop a final newline on their own; that alone is not an edit.
        if edited.rstrip("\n") == original.rstrip("\n"):
            return "No changes; nothing was written."
        if not edited.strip():
            return "Not saved: an empty prompt. Delete the file to return to wizolt's own."
        target = path if path is not None else paths[-1]
        try:
            write(target, edited)
        except OSError as error:
            return f"Not saved: {error}"
        return f"Saved {target}; it applies from the next request."

    plugin.intercept("context.compose", compose)
    plugin.command("prompt", "Edit the system prompt in your editor", edit)
