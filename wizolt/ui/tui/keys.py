"""Canonical key names at the terminal boundary, shared by local and global actions."""

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from wizolt.sdk import PluginError


def normalized_key(key: str) -> str:
    """Validate through the public binding API rather than duplicating terminal aliases."""
    if not isinstance(key, str):
        raise PluginError("Key must be text")
    key = key.lower().replace("ctrl-", "c-") if len(key) > 1 else key
    bindings = KeyBindings()
    try:
        bindings.add(key)(lambda event: None)
    except ValueError as error:
        raise PluginError(f"Unknown key: {key}") from error
    parsed = bindings.bindings[0].keys[0]
    return parsed.value if isinstance(parsed, Keys) else parsed
