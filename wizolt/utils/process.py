"""Short shell commands run to completion: bounded in time and output, never left behind.

For commands wizolt runs on the user's behalf rather than the model's -- a hook, a skill's
`!`command`` context -- where there is nothing to stream and nothing to promote to a background
job. The Bash tool owns that richer lifecycle; this is the plain one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass(frozen=True)
class ShellResult:
    exit_code: int  # -1 when the command was killed for running past its timeout
    stdout: str
    stderr: str

    @property
    def timed_out(self) -> bool:
        return self.exit_code == -1


@dataclass(frozen=True)
class ShellCommand:
    """One command for bash, with where and how long it may run and what it reads on stdin."""

    command: str
    cwd: str
    timeout: float
    stdin: str = ""
    env: Mapping[str, str] = field(default_factory=dict)  # added to the inherited environment

    MAX_OUTPUT_CHARS: ClassVar[int] = 32_000

    async def run(self) -> ShellResult:
        """Run in its own process group and wait for it.

        Past the timeout the whole group is killed and the result says so. Cancellation kills it
        too and waits for the exit before re-raising: a cancelled caller must not leave the
        command running on its own."""
        process = await asyncio.create_subprocess_exec(
            shutil.which("bash") or "bash",
            "-c",
            self.command,
            cwd=self.cwd,
            env={**os.environ, **self.env} if self.env else None,
            stdin=asyncio.subprocess.PIPE if self.stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own group, so the kill below reaches its children too
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(self.stdin.encode() if self.stdin else None), self.timeout)
        except TimeoutError:
            await self._kill(process)
            return ShellResult(-1, "", f"timed out after {self.timeout:g}s")
        except BaseException:
            await self._kill(process)
            raise
        return ShellResult(process.returncode if process.returncode is not None else -1, self._text(stdout), self._text(stderr))

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()

    @classmethod
    def _text(cls, data: bytes) -> str:
        text = data.decode(errors="replace")
        if len(text) <= cls.MAX_OUTPUT_CHARS:
            return text
        return text[: cls.MAX_OUTPUT_CHARS] + f"\n... ({len(text) - cls.MAX_OUTPUT_CHARS} more characters cut)"
