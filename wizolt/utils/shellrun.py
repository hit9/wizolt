"""Run one short shell command to completion: bounded in time and output, never left behind.

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
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 32_000


@dataclass(frozen=True)
class ShellResult:
    exit_code: int  # -1 when the command was killed for running past its timeout
    stdout: str
    stderr: str

    @property
    def timed_out(self) -> bool:
        return self.exit_code == -1


async def run_shell(command: str, *, cwd: str, timeout: float, stdin: str = "", env: dict[str, str] | None = None) -> ShellResult:
    """Run `command` under bash in its own process group and wait for it.

    Past `timeout` seconds the whole group is killed and the result says so. Cancellation kills it
    too and waits for the exit before re-raising: a cancelled caller must not leave the command
    running on its own."""
    process = await asyncio.create_subprocess_exec(
        shutil.which("bash") or "bash",
        "-c",
        command,
        cwd=cwd,
        env=None if env is None else {**os.environ, **env},
        stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # its own group, so the kill below reaches its children too
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin.encode() if stdin else None), timeout)
    except TimeoutError:
        await _kill(process)
        return ShellResult(-1, "", f"timed out after {timeout:g}s")
    except BaseException:
        await _kill(process)
        raise
    return ShellResult(process.returncode if process.returncode is not None else -1, _text(stdout), _text(stderr))


async def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(OSError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await process.wait()


def _text(data: bytes) -> str:
    text = data.decode(errors="replace")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... ({len(text) - MAX_OUTPUT_CHARS} more characters cut)"
