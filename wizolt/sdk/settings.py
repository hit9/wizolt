"""Generation-local configuration: validate plain data, then expose a deeply frozen view."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wizolt.sdk.models import HostCall


class Settings:
    """Explicit persistent edits; the generation's frozen config never changes implicitly."""

    def __init__(self):
        self.call: HostCall | None = None
        self.schema: dict | None = None
        self.defaults: dict = {}

    async def update(self, values: Mapping[str, Any], *, reset=()) -> Mapping[str, Any]:
        """Atomically save top-level overrides/reset defaults and return resolved settings.

        Only configure() properties can be edited. Reset removes an override, not a default.
        Validation runs here in the worker; the host compares the read snapshot before writing.
        """
        from wizolt.sdk import PluginError

        if self.schema is None or self.call is None:
            raise PluginError("Settings updates require configure() and an explicit hosted action")
        if not isinstance(values, Mapping) or isinstance(reset, str):
            raise PluginError("Settings update requires a mapping and a sequence of reset keys")
        values, reset = dict(values), list(reset)
        declared = self.schema.get("properties", {})
        if any(not isinstance(key, str) or key not in declared for key in [*values, *reset]):
            raise PluginError("Settings updates may only edit declared top-level properties")
        if set(values).intersection(reset):
            raise PluginError("Cannot update and reset the same setting")
        check_values(values)
        current = (await self.call("settings.read", {}))["values"]
        candidate = {key: value for key, value in current.items() if key not in reset}
        candidate.update(values)
        resolved = resolve(candidate, self.schema, self.defaults)
        await self.call("settings.update", {"values": values, "reset": reset, "expected": current})
        return resolved


def check_values(value: Any) -> None:
    """The writable subset is finite JSON without null; TOML has no null representation."""
    import math

    from wizolt.sdk import PluginError

    if type(value) in (str, bool, int) or (type(value) is float and math.isfinite(value)):
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for child in value.values():
            check_values(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            check_values(child)
        return
    raise PluginError("Settings require finite JSON values without null; use reset to remove an override")


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def resolve(values: Mapping[str, Any], schema: Mapping[str, Any], defaults: Mapping[str, Any]) -> Mapping[str, Any]:
    """Defaults are explicit top-level fallbacks; JSON Schema's default annotation is not mutation.

    Never include instance values in validation errors: they may contain credentials. Local
    schema references are supported; external references are forbidden to keep validation offline.
    """
    from jsonschema import Draft202012Validator

    from wizolt.sdk import PluginError

    specification = deepcopy(dict(schema))
    Draft202012Validator.check_schema(specification)
    pending = [specification]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key in ("$ref", "$dynamicRef") and not value.startswith("#"):
                    raise PluginError("Configuration schemas only support local references")
                pending.append(value)
        elif isinstance(item, list):
            pending.extend(item)
    merged = {**deepcopy(dict(defaults)), **deepcopy(dict(values))}
    error = next(Draft202012Validator(specification).iter_errors(merged), None)
    if error is not None:
        location = ".".join(map(str, error.absolute_path)) or "<root>"
        raise PluginError(f"Invalid plugin configuration at {location}: {error.validator} constraint failed")
    return freeze(merged)
