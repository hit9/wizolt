"""Short shell commands run to completion: bounded in time and output, never left behind.

For commands wizolt runs on the user's behalf rather than the model's -- a hook, a skill's
`!`command`` context -- where there is nothing to stream and nothing to promote to a background
job. The Bash tool owns that richer lifecycle; this is the plain one.
"""

from __future__ import annotations

import asyncio
import codecs
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
    CANNOT_START: ClassVar[int] = 127  # what a shell reports for a command it could not run

    async def run(self, max_output: int | None = None) -> ShellResult:
        """Run in its own process group and wait for it; each stream keeps at most `max_output`
        characters (MAX_OUTPUT_CHARS by default).

        Past the timeout the whole group is killed and the result says so. Cancellation kills it
        too and waits for the exit before re-raising: a cancelled caller must not leave the
        command running on its own. A command that cannot start at all (its directory is gone,
        there is no bash) is a failed result too, with the shell's own exit code for that."""
        try:
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
        except OSError as error:
            return ShellResult(self.CANNOT_START, "", f"cannot start: {error}")
        limit = self.MAX_OUTPUT_CHARS if max_output is None else max(0, max_output)
        assert process.stdout is not None and process.stderr is not None
        tasks = [
            asyncio.create_task(self._read(process.stdout, limit)),
            asyncio.create_task(self._read(process.stderr, limit)),
            asyncio.create_task(self._write(process)),
            asyncio.create_task(process.wait()),
        ]
        communication = asyncio.gather(*tasks)
        try:
            # Keep draining while killing: wait() can otherwise hang on a full pipe after its
            # reader was cancelled. Neither timeout nor cancellation leaves a reader behind.
            stdout, stderr, _, _ = await asyncio.wait_for(asyncio.shield(communication), self.timeout)
        except TimeoutError:
            await self._kill(process)
            return ShellResult(-1, "", f"timed out after {self.timeout:g}s")
        except BaseException:
            await self._kill(process)
            raise
        finally:
            await asyncio.gather(communication, *tasks, return_exceptions=True)
        return ShellResult(process.returncode if process.returncode is not None else -1, str(stdout), str(stderr))

    async def _write(self, process: asyncio.subprocess.Process) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(self.stdin.encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # hooks need not consume stdin
        finally:
            process.stdin.close()

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()

    @staticmethod
    async def _read(stream: asyncio.StreamReader, limit: int) -> str:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parts: list[str] = []
        kept = total = 0
        while True:
            chunk = await stream.read(16_384)
            text = decoder.decode(chunk, final=not chunk)
            total += len(text)
            if kept < limit:
                part = text[: limit - kept]
                parts.append(part)
                kept += len(part)
            if not chunk:
                break
        return "".join(parts) + (f"\n... ({total - kept} more characters cut)" if total > kept else "")
