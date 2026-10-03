"""Prepare dependency environments without changing the running interpreter.

The host's installed versions are constraints, not upgrade candidates. A new environment can
add packages while borrowing the current host installation through a .pth file. It is tied to
that installation; the user launches it explicitly after saving the current session.
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
from wizolt.sdk import PluginError
from wizolt.utils.process import ShellCommand


class DependencyEnvironment:
    """Build and validate a disposable candidate without changing the running host.

    In-process plugins share Python's import cache, so independent per-plugin dependency versions
    would promise isolation we cannot provide. Resolve the enabled set together instead; switching
    interpreters is an explicit restart. This environment borrows the host and is not portable.
    """

    def __init__(self, root: Path, sources: list[PluginSource], cwd: str):
        self.path = root / uuid.uuid4().hex
        self.sources = sources
        self.cwd = cwd

    async def command(self, arguments: list[str]) -> str:
        result = await ShellCommand(shlex.join(arguments), self.cwd, timeout=300).run()
        if result.exit_code:
            raise PluginError(f"Dependency preparation failed: {result.stderr or result.stdout}")
        return result.stdout.strip()

    async def prepare(self) -> dict[str, str]:
        """Return a launch command only after dependency and setup checks have succeeded.

        Neither success nor failure activates code in this process. On failure or cancellation,
        remove the candidate directory; installation records are the caller's responsibility and
        must not advertise a half-built environment.
        """
        uv = shutil.which("uv")
        if uv is None:
            raise PluginError("Dependency installation requires uv on PATH")
        requirements = sorted({item for source in self.sources for item in source.dependencies})
        if not requirements:
            raise PluginError("No DEPENDENCIES declared; use enable instead")
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
                script = (
                    "from wizolt.plugins.loading import LoadedPlugin, PluginSource; "
                    "import sys; p = LoadedPlugin.load(PluginSource.read(sys.argv[1])); p.close()"
                )
                await self.command([python, "-c", script, source.path])
            frozen = await self.command([uv, "pip", "freeze", "--python", python])
            (self.path / "requirements.lock").write_text(frozen + "\n")
            (self.path / "plugins.json").write_text(json.dumps({source.name: source.digest for source in self.sources}, indent=2))
            return {
                "status": "restart_required",
                "environment": str(self.path),
                "launch": shlex.join([python, "-m", "wizolt"]),
                "note": "Save/exit before launching this command with your usual --config and --resume arguments. The current process is unchanged.",
            }
        except BaseException:
            await run_blocking(partial(shutil.rmtree, self.path, ignore_errors=True))
            raise
