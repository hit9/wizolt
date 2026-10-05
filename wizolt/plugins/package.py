"""Frozen package source and resources, independent of the live workspace after admission.

No build backend runs to discover metadata. The parent owns temporary snapshots so even a
hard-killed worker cannot leave its extracted package behind. Directory-descriptor traversal
refuses symlinks without a check/open race that could copy files outside the selected root.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from wizolt.plugins.files import read_regular
from wizolt.sdk import SDK_VERSION, PluginError

MAX_PACKAGE_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_FILES = 512
MAX_PACKAGE_ENTRIES = 1024
MAX_PACKAGE_DEPTH = 32
IGNORED_DIRECTORIES = frozenset((".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"))
IDENTIFIER = r"[A-Za-z_][A-Za-z_0-9]*"
ENTRY = re.compile(rf"{IDENTIFIER}(?:\.{IDENTIFIER})*:{IDENTIFIER}(?:\.{IDENTIFIER})*\Z")


class PackageReader:
    """Walk a bounded tree through open directory handles, retaining regular files only."""

    def __init__(self):
        self.files: list[tuple[str, bytes]] = []
        self.remaining = MAX_PACKAGE_BYTES
        self.entries = 0

    def read(self, root: Path) -> tuple[tuple[str, bytes], ...]:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.walk(descriptor)
        finally:
            os.close(descriptor)
        return tuple(self.files)

    def walk(self, directory: int, prefix: str = "", depth: int = 0) -> None:
        if depth > MAX_PACKAGE_DEPTH:
            raise PluginError(f"Plugin package exceeds {MAX_PACKAGE_DEPTH} directory levels")
        with os.scandir(directory) as entries:
            names = []
            for entry in entries:
                self.entries += 1
                if self.entries > MAX_PACKAGE_ENTRIES:
                    raise PluginError(f"Plugin package exceeds {MAX_PACKAGE_ENTRIES} directory entries")
                names.append(entry.name)
        for name in sorted(names):
            if name in IGNORED_DIRECTORIES or name.endswith((".pyc", ".pyo")):
                continue
            relative = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    self.walk(child, relative, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if len(self.files) >= MAX_PACKAGE_FILES:
                    raise PluginError(f"Plugin package exceeds {MAX_PACKAGE_FILES} files")
                data = read_regular(name, self.remaining, directory=directory)
                self.remaining -= len(data)
                self.files.append((relative, data))
            else:
                raise PluginError(f"Plugin package contains a symlink or non-regular file: {relative}")


@dataclass(frozen=True)
class PackageSnapshot:
    name: str
    entry: str
    manifest: str
    dependencies: tuple[str, ...]
    files: tuple[tuple[str, bytes], ...]
    digest: str
    description: str = ""

    @classmethod
    def read(cls, root: Path) -> PackageSnapshot:
        manifest = read_regular(root / "pyproject.toml", 256 * 1024).decode("utf-8")
        data = tomllib.loads(manifest)
        project = data.get("project", {})
        if not isinstance(project, dict) or not isinstance(project.get("name"), str):
            raise PluginError("pyproject.toml requires [project].name")
        name = re.sub(r"[-_.]+", "_", project["name"]).lower()
        if not re.fullmatch(IDENTIFIER, name):
            raise PluginError("Project name must normalize to an ASCII Python identifier")
        tool = data.get("tool", {})
        wizolt = tool.get("wizolt", {}) if isinstance(tool, dict) else {}
        plugin = wizolt.get("plugin", {}) if isinstance(wizolt, dict) else {}
        if not isinstance(plugin, dict) or plugin.get("sdk") != SDK_VERSION:
            raise PluginError(f"Declare [tool.wizolt.plugin] sdk = {SDK_VERSION}")
        if set(plugin) - {"sdk", "entry"}:
            raise PluginError("Unknown [tool.wizolt.plugin] keys; supported: sdk, entry")
        entry = plugin.get("entry", name + ":setup")
        if not isinstance(entry, str) or not ENTRY.fullmatch(entry):
            raise PluginError("Plugin entry must be a dotted module:callable reference")
        dependencies = project.get("dependencies", [])
        dynamic = project.get("dynamic", [])
        if not isinstance(dynamic, list) or any(not isinstance(item, str) for item in dynamic):
            raise PluginError("project.dynamic must be a list of field names")
        if "dependencies" in dynamic or not isinstance(dependencies, list) or any(not isinstance(item, str) or not item for item in dependencies):
            raise PluginError("Declare dependencies as a static [project].dependencies list")
        if required := project.get("requires-python"):
            import platform

            from packaging.specifiers import SpecifierSet

            if not isinstance(required, str) or not SpecifierSet(required).contains(platform.python_version(), prereleases=True):
                raise PluginError("Plugin requires a different Python version")
        module = entry.partition(":")[0].split(".")[0]
        source = root / "src" if (root / "src" / module).exists() or (root / "src" / (module + ".py")).exists() else root
        files = PackageReader().read(source)
        members = dict(files)
        if module + ".py" not in members and module + "/__init__.py" not in members:
            raise PluginError(f"Entry module {module!r} is missing; use a regular package or Python module")
        digest = hashlib.sha256(manifest.encode())
        for path, content in files:
            digest.update(path.encode() + b"\0" + len(content).to_bytes(8, "big") + content)
        description = project.get("description")
        if not isinstance(description, str) or not description.strip():
            description = cls.docstring(members.get(module + ".py") or members.get(module + "/__init__.py") or b"")
        return cls(name, entry, manifest, tuple(dependencies), files, digest.hexdigest()[:12], " ".join(description.split())[:300])

    @staticmethod
    def docstring(content: bytes) -> str:
        """The entry module's introduction, parsed, never executed; unreadable source has none."""
        try:
            return summary(ast.get_docstring(ast.parse(content.decode("utf-8"))))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            return ""

    def stage(self) -> tempfile.TemporaryDirectory:
        """Materialize exactly the approved revision, never re-read the user's source tree."""
        temporary = tempfile.TemporaryDirectory(prefix="wizolt-plugin-")
        try:
            for relative, data in self.files:
                path = Path(temporary.name) / relative
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_bytes(data)
            return temporary
        except BaseException:
            temporary.cleanup()
            raise


def summary(docstring: str | None) -> str:
    """A docstring's first paragraph as one line: the plugin's own introduction."""
    paragraph = (docstring or "").strip().split("\n\n", 1)[0]
    return " ".join(paragraph.split())[:300]
