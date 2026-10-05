"""Read plugin metadata without executing it, then load an explicitly trusted generation."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import sys
import types
import uuid
from dataclasses import dataclass
from pathlib import Path

from wizolt.plugins.files import read_regular
from wizolt.plugins.package import PackageSnapshot, summary
from wizolt.sdk import SDK_VERSION, Plugin, PluginError

MAX_SOURCE_BYTES = 256 * 1024


@dataclass(frozen=True)
class PluginSource:
    """An immutable source revision, shared by validation and activation.

    Retaining the text matters: the file can change again after validation. Activation executes
    exactly this revision rather than trusting the current file.
    """

    path: str
    text: str
    name: str
    digest: str
    dependencies: tuple[str, ...]
    entry: str = ""
    package: PackageSnapshot | None = None
    # What the plugin is for, read without running it: the module docstring's first paragraph,
    # or a package's [project] description. Shown in /plugins; never sent to the worker.
    description: str = ""
    # Its user documentation in Markdown: the whole docstring, or a package's README.md.
    about: str = ""

    def require_name(self, expected: str) -> None:
        """An installed name owns settings, tools and preferences; reload cannot rename it."""
        if self.name != expected:
            raise PluginError(f"Plugin identity changed from {expected!r} to {self.name!r}; restore the installed name before reloading")

    def descriptor(self) -> dict:
        """Small RPC metadata; package bytes travel through a parent-owned frozen directory."""
        return {"path": self.path, "text": self.text, "name": self.name, "digest": self.digest, "dependencies": self.dependencies, "entry": self.entry}

    @classmethod
    def read(cls, path: str, text: str | None = None) -> PluginSource:
        """Parse only literal metadata; discovering dependencies must not execute plugin code."""
        location = Path(path).expanduser().resolve()
        if location.is_dir():
            package = PackageSnapshot.read(location)
            return cls(
                str(location), package.manifest, package.name, package.digest, package.dependencies, package.entry, package, package.description, package.about
            )
        if location.suffix != ".py" or not location.stem.isidentifier() or not location.stem.isascii():
            raise PluginError("Use a .py filename that is an ASCII Python identifier")
        if text is None:
            # A FIFO/device masquerading as a .py file must not block the host before the
            # worker exists. Inspect the opened descriptor, not a racy path stat, and bound
            # the read itself so concurrent file growth cannot bypass the source limit.
            data = read_regular(location, MAX_SOURCE_BYTES)
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
        digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        docstring = ast.get_docstring(tree) or ""
        return cls(str(location), text, location.stem, digest, tuple(dependencies), description=summary(docstring), about=docstring)


def unmet_dependencies(declarations: tuple[str, ...]) -> list[str]:
    """Requirements this interpreter cannot satisfy, each with the reason; markers are honored."""
    if not declarations:
        return []  # Most plugins declare none; packaging costs every worker launch ~9 ms.
    from importlib.metadata import PackageNotFoundError, version

    from packaging.requirements import InvalidRequirement, Requirement

    unmet = []
    for declaration in declarations:
        try:
            requirement = Requirement(declaration)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            installed = version(requirement.name)
        except (InvalidRequirement, PackageNotFoundError):
            unmet.append(f"{declaration!r} is unavailable")
            continue
        if not requirement.specifier.contains(installed, prereleases=True):
            unmet.append(f"{declaration} conflicts with installed {installed}")
    return unmet


@dataclass
class LoadedPlugin:
    """Own one module identity; callers must retire it even when activation is deferred."""

    source: PluginSource
    plugin: Plugin
    module_name: str

    @classmethod
    def load(cls, source: PluginSource, config: dict | None = None, directory: str = "") -> LoadedPlugin:
        """Execute trusted setup from exact source, bypassing timestamp-based bytecode caches."""
        if unmet := unmet_dependencies(source.dependencies):
            raise PluginError(f"{source.name}: dependency {unmet[0]}; run `wizolt plugin enable {source.name}` to prepare dependencies")
        if source.entry:
            # Each generation owns a fresh process. Normal package imports therefore support
            # relative imports/resources without retaining stale submodules across reloads.
            if not directory:
                raise PluginError("Package loading requires an admitted source snapshot")
            name, _, attribute = source.entry.partition(":")
            if name.split(".")[0] in sys.modules:
                raise PluginError(f"Entry module {name!r} conflicts with an already loaded module")
            sys.path.insert(0, directory)
            module = importlib.import_module(name)
            setup = module
            for part in attribute.split("."):
                setup = getattr(setup, part)
            plugin = Plugin(source.name, config)
            cls.register(source.name, setup, plugin)
            return cls(source, plugin, name)
        name = f"_wizolt_plugin_{source.name}_{uuid.uuid4().hex}"
        module = types.ModuleType(name)
        module.__file__ = source.path
        # Dataclasses and introspection need a real module identity. Each agent/generation gets
        # a unique identity; retirement explicitly removes it rather than leaking sys.modules.
        sys.modules[name] = module
        try:
            exec(compile(source.text, source.path, "exec"), module.__dict__)  # noqa: S102 - explicitly trusted Python plugins.
            setup = module.__dict__.get("setup")
            plugin = Plugin(source.name, config)
            cls.register(source.name, setup, plugin)
            return cls(source, plugin, name)
        except BaseException as error:
            sys.modules.pop(name, None)
            if isinstance(error, SystemExit):
                raise PluginError(f"{source.name}: setup attempted to exit the host") from error
            raise

    @staticmethod
    def register(name: str, setup: object, plugin: Plugin) -> None:
        if not callable(setup) or inspect.iscoroutinefunction(setup):
            raise PluginError(f"{name}: provide a synchronous plugin entry callable")
        setup(plugin)

    def close(self) -> None:
        """Release host registration, not arbitrary side effects of imported Python code.

        Plugins must not create unmanaged tasks or threads: removing a module from the import
        registry cannot cancel those resources, and they could outlive a disabled generation.
        """
        sys.modules.pop(self.module_name, None)
