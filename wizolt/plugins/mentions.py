"""Plugin references point to discovery; they neither load code nor expand capabilities."""

from wizolt.mentions import scan_mentions

MAX_REFERENCES = 50
MAX_REFERENCE_CHARACTERS = 8000


def resolve_mentions(text: str) -> str:
    """Preserve explicit names, including unknown ones, just like file references.

    Availability changes with activation and agent focus. The model queries it when needed;
    storing tool metadata here would duplicate discovery and retain stale capabilities.
    """
    names = dict.fromkeys(span.payload for span in scan_mentions(text) if span.kind == "plugin" and span.complete)
    if not names:
        return ""
    lines = []
    size = 0
    for name in names:
        if len(lines) >= MAX_REFERENCES or size + len(name) > MAX_REFERENCE_CHARACTERS:
            lines.append("Additional plugin references omitted; consult the original message.")
            break
        lines.append(f"[{name}]")
        size += len(name)
    return "\n".join(
        [
            "--- PLUGIN MENTIONS ---",
            'The user referenced these plugins. Query Plugin(action="list", name=...) for current tools, then describe one as needed.',
            "For installed/disabled plugins, use wizolt plugin list/inspect. Mentioning a plugin does not enable it or authorize execution.",
            "No capabilities, schemas, settings or source are inlined.",
            *lines,
        ]
    )
