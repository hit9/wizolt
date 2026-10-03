"""Read plugin metadata without executing it, then load an explicitly trusted generation."""

from __future__ import annotations

import ast
import hashlib
import inspect
import os
import stat
import sys
import types
import uuid
from dataclasses import dataclass
from pathlib import Path

from wizolt.sdk import SDK_VERSION, Plugin, PluginError

MAX_SOURCE_BYTES = 256 * 1024


@dataclass(frozen=True)
class PluginSource:
    """An immutable source revision, shared by validation, activation, and rollback.

    Retaining the text matters: the file can change again after validation. Activation executes
    this revision, and rollback uses retained text rather than trusting the current file.
    """

    path: str
    text: str
    name: str
    digest: str
    dependencies: tuple[str, ...]

    @classmethod
    def read(cls, path: str, text: str | None = None) -> PluginSource:
        """Parse only literal metadata; discovering dependencies must not execute plugin code."""
        location = Path(path).expanduser().resolve()
        if location.suffix != ".py" or not location.stem.isidentifier() or not location.stem.isascii():
            raise PluginError("Use a .py filename that is an ASCII Python identifier")
        if text is None:
            # A FIFO/device masquerading as a .py file must not block the host before the
            # worker exists. Inspect the opened descriptor, not a racy path stat, and bound
            # the read itself so concurrent file growth cannot bypass the source limit.
            descriptor = os.open(location, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise PluginError("Plugin source must be a regular file")
                data = stream.read(MAX_SOURCE_BYTES + 1)
            if len(data) > MAX_SOURCE_BYTES:
                raise PluginError("Plugin source exceeds 256 KiB")
            text = data.decode("utf-8")
        elif len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise PluginError("Plugin source exceeds 256 KiB")
        tree = ast.parse(text, filename=str(location))
        metadata = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {"SDK_VERSION", "DEPENDENCIES"}:
                        metadata[target.id] = ast.literal_eval(node.value)
        if metadata.get("SDK_VERSION") != SDK_VERSION:
            raise PluginError(f"{location.stem}: declare SDK_VERSION = {SDK_VERSION}")
        dependencies = metadata.get("DEPENDENCIES", ())
        if not isinstance(dependencies, (tuple, list)) or any(not isinstance(item, str) or not item.strip() for item in dependencies):
            raise PluginError("DEPENDENCIES must be a literal list or tuple of requirement strings")
        return cls(str(location), text, location.stem, hashlib.sha256(text.encode()).hexdigest()[:12], tuple(dependencies))


@dataclass
class LoadedPlugin:
    """Own one module identity; callers must retire it even when activation is deferred."""

    source: PluginSource
    plugin: Plugin
    module_name: str

    @classmethod
    def load(cls, source: PluginSource, config: dict | None = None) -> LoadedPlugin:
        """Execute trusted setup from exact source, bypassing timestamp-based bytecode caches."""
        if source.dependencies:
            from importlib.metadata import PackageNotFoundError, version

            from packaging.requirements import InvalidRequirement, Requirement

            for declaration in source.dependencies:
                try:
                    requirement = Requirement(declaration)
                    if requirement.marker and not requirement.marker.evaluate():
                        continue
                    installed = version(requirement.name)
                except (InvalidRequirement, PackageNotFoundError) as error:
                    raise PluginError(f"{source.name}: dependency {declaration!r} is unavailable; use install") from error
                if not requirement.specifier.contains(installed, prereleases=True):
                    raise PluginError(f"{source.name}: {declaration} conflicts with installed {installed}; use install")
        name = f"_wizolt_plugin_{source.name}_{uuid.uuid4().hex}"
        module = types.ModuleType(name)
        module.__file__ = source.path
        # Dataclasses and introspection need a real module identity. Each agent/generation gets
        # a unique identity; retirement explicitly removes it rather than leaking sys.modules.
        sys.modules[name] = module
        try:
            exec(compile(source.text, source.path, "exec"), module.__dict__)  # noqa: S102 - explicitly trusted Python plugins.
            setup = module.__dict__.get("setup")
            if not callable(setup) or inspect.iscoroutinefunction(setup):
                raise PluginError(f"{source.name}: provide synchronous setup(plugin)")
            plugin = Plugin(source.name, config)
            setup(plugin)
            return cls(source, plugin, name)
        except BaseException as error:
            sys.modules.pop(name, None)
            if isinstance(error, SystemExit):
                raise PluginError(f"{source.name}: setup attempted to exit the host") from error
            raise

    def close(self) -> None:
        """Release host registration, not arbitrary side effects of imported Python code.

        Plugins must not create unmanaged tasks or threads: removing a module from the import
        registry cannot cancel those resources, and they could outlive a disabled generation.
        """
        sys.modules.pop(self.module_name, None)
