"""Prepare dependency environments without changing the running interpreter.

The host's installed versions are constraints, not upgrade candidates. A new worker environment
can add packages while borrowing the host installation through a .pth file. It remains tied to
that installation; only the plugin worker changes interpreter, never the running wizolt process.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shlex
import shutil
import sys
import uuid
from functools import partial
from pathlib import Path

from wizolt.base import run_blocking
from wizolt.plugins.loading import PluginSource
from wizolt.plugins.process import PluginProcess
from wizolt.plugins.settings import PluginSettings
from wizolt.sdk import PluginError
from wizolt.utils.process import ShellCommand


class DependencyEnvironment:
    """Build a worker interpreter without changing the host. The environment borrows the host
    SDK installation and is not portable; it becomes usable only after preparation succeeds.
    """

    def __init__(self, root: Path, sources: list[PluginSource], cwd: str, settings: PluginSettings | None = None):
        self.path = root / uuid.uuid4().hex
        self.sources = sources
        self.cwd = cwd
        self.settings = settings or PluginSettings()

    @staticmethod
    def requirements(sources: list[PluginSource]) -> list[str]:
        return sorted({item for source in sources for item in source.dependencies})

    @classmethod
    def built_for(cls, python: str, source: PluginSource) -> bool:
        """Whether a host-built environment was prepared for exactly these declarations.

        Declarations, not resolved versions: an unchanged list reuses the environment, a changed
        one prepares a fresh environment. Unreadable metadata means it must be prepared again.
        """
        try:
            metadata = json.loads((Path(python).parent.parent / "plugins.json").read_text())
        except (OSError, ValueError):
            return False
        return isinstance(metadata, dict) and metadata.get("requirements") == cls.requirements([source])

    async def command(self, arguments: list[str]) -> str:
        result = await ShellCommand(shlex.join(arguments), self.cwd, timeout=300).run()
        if result.exit_code:
            raise PluginError(f"Dependency preparation failed: {result.stderr or result.stdout}")
        return result.stdout.strip()

    async def prepare(self) -> dict[str, str]:
        """Return a worker interpreter only after dependency and setup checks have succeeded.

        Neither success nor failure activates code in this process. On failure or cancellation,
        remove the candidate directory; installation records are the caller's responsibility and
        must not advertise a half-built environment.
        """
        uv = shutil.which("uv")
        if uv is None:
            raise PluginError("Dependency installation requires uv on PATH")
        requirements = self.requirements(self.sources)
        if not requirements:
            raise PluginError("No DEPENDENCIES declared; nothing to prepare")
        from packaging.requirements import InvalidRequirement, Requirement

        # Both single-file and package metadata arrive here before pip. A list of argv
        # strings is shell-safe but would still let '--python/--target' become uv options.
        for requirement in requirements:
            try:
                Requirement(requirement)
            except InvalidRequirement as error:
                raise PluginError(f"Invalid dependency requirement: {requirement!r}") from error
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self.command([uv, "venv", "--python", sys.executable, str(self.path)])
            python = str(self.path / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            constraints = self.path / "host-constraints.txt"
            pins = sorted({f"{dist.metadata['Name']}=={dist.version}" for dist in importlib.metadata.distributions() if "Name" in dist.metadata})
            constraints.write_text("\n".join(pins) + "\n")
            await self.command([uv, "pip", "install", "--python", python, "--constraints", str(constraints), *requirements])
            site = Path(await self.command([python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]))
            # Plain path lines only. Do not run code in the .pth file or mutate sys.path in the
            # live host; the new interpreter establishes its import environment at startup.
            host_paths = [str(Path(path).resolve()) for path in sys.path if path and Path(path).is_dir() and "\n" not in path]
            # Editable installs may expose wizolt through a meta-path finder rather than a
            # sys.path entry. The candidate interpreter does not inherit that finder.
            host_paths.append(str(Path(__file__).resolve().parents[2]))
            (site / "wizolt-host.pth").write_text("\n".join(dict.fromkeys(host_paths)) + "\n")
            await self.command([uv, "pip", "check", "--python", python])
            for source in self.sources:
                worker, _ = await PluginProcess.start(source, python=python, cwd=self.cwd, config=self.settings.read(source.name))
                await worker.close()
            frozen = await self.command([uv, "pip", "freeze", "--python", python])
            (self.path / "requirements.lock").write_text(frozen + "\n")
            metadata = {"requirements": requirements, "plugins": {source.name: source.digest for source in self.sources}}
            (self.path / "plugins.json").write_text(json.dumps(metadata, indent=2))
            return {
                "status": "prepared",
                "environment": str(self.path),
                "python": python,
                "note": "Worker environment prepared. Reload the plugin to activate it in an existing agent.",
            }
        except BaseException:
            await run_blocking(partial(shutil.rmtree, self.path, ignore_errors=True))
            raise
