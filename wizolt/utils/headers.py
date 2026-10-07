"""HTTP header value templates: `{session_id}` and nothing else, expanded per request.

Shared by the provider catalog and the config file, which both check a template when they load it
so a misspelled variable is an error there rather than literal braces on the wire."""

from __future__ import annotations

import re
from collections.abc import Mapping

HEADER_VARIABLES = frozenset({"session_id"})
# Only `{word}` is a variable; other braces (a JSON-looking value) pass through as written.
_VARIABLE_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


def unknown_header_variables(template: str) -> list[str]:
    return [name for name in _VARIABLE_RE.findall(template) if name not in HEADER_VARIABLES]


def render_header(template: str, variables: Mapping[str, str]) -> str:
    return _VARIABLE_RE.sub(lambda match: variables.get(match.group(1), ""), template)
