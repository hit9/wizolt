"""Generation-local configuration: validate plain data, then expose a deeply frozen view."""

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator


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
