"""A bounded request channel to one plugin generation, with host-enforced cancellation.

Neither reading stdout nor waiting for plugin Python runs on the host's input thread. A failed
frame, EOF or deadline fails pending requests and makes the generation recoverable by reload.
Worker stderr is continuously drained into a bounded tail, preventing accidental print deadlocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable
from typing import Any

from wizolt.plugins.hostcalls import Continuations, HostCalls, RequestDeadline
from wizolt.plugins.loading import PluginSource
from wizolt.plugins.protocol import MAX_FRAME, MAX_REQUEST
from wizolt.plugins.scope import InvocationScope
from wizolt.sdk import PluginError


class WorkerError(PluginError):
    """A failed worker call with bounded diagnostics suitable for an authoring report."""

    def __init__(self, message: str, *, log: str = "", traceback: str = ""):
        super().__init__(message)
        self.log = log
        self.traceback = traceback

    @staticmethod
    def diagnostics(log: str, traceback: str) -> dict[str, str]:
        """Bounded tails for a model-facing result: enough to find the failing line, never whole logs."""
        return {key: value[-limit:] for key, value, limit in (("traceback", traceback, 4000), ("log", log, 2000)) if value.strip()}


class PluginProcess:
    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.pending: dict[int, asyncio.Future] = {}
        self.operations: dict[int, str] = {}
        self.host_calls = HostCalls(self._write, self.admits_host_call)
        self.deadlines: dict[int, RequestDeadline] = {}
        self.scopes: dict[int, InvocationScope] = {}
        self.host_calls.suspend = lambda identity: self.deadlines[identity].pause()
        self.host_calls.permits = self.permits
        self.host_calls.scope_of = self.scopes.get
        self.continuations = Continuations(self._write, lambda identity: self.operations.get(identity, "").startswith("intercept:"))
        self.continuations.suspend = lambda identity: self.deadlines[identity].pause()
        self.sequence = 0
        self._stderr = b""
        self.error = ""
        self.traceback = ""
        self.reader = asyncio.create_task(self._read())
        self.errors = asyncio.create_task(self._drain_errors())
        self._close_lock = asyncio.Lock()
        self.snapshot = None

    def admits_host_call(self, identity: int) -> bool:
        future = self.pending.get(identity)
        operation = self.operations.get(identity, "")
        return future is not None and not future.done() and (operation == "invoke" or operation.startswith("intercept:"))

    # Views and notices: explicit actions, and the two interceptors that act for the user.
    INTERACTIVE = frozenset({"invoke", "intercept:prompt.submit", "intercept:tool.call"})

    def permits(self, identity: int, service: str) -> bool:
        """Per-operation service permissions. Settings and layout writes stay explicit-action only."""
        operation = self.operations.get(identity, "")
        if service.startswith(("settings.", "ui.components.", "ui.shortcuts.")):
            return operation == "invoke"
        if service.startswith("ui."):
            return operation in self.INTERACTIVE
        return True

    @classmethod
    async def start(
        cls, source: PluginSource, *, timeout: float = 5, python: str = "", cwd: str | None = None, config: dict | None = None
    ) -> tuple[PluginProcess, dict]:
        snapshot = source.package.stage() if source.package else None
        try:
            process = await asyncio.create_subprocess_exec(
                python or sys.executable,
                "-P",  # Workspace files must not shadow worker/validator dependencies.
                "-m",
                "wizolt.plugins.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=MAX_FRAME,
                cwd=cwd,
            )
        except BaseException:
            if snapshot is not None:
                snapshot.cleanup()
            raise
        worker = cls(process)
        worker.snapshot = snapshot
        try:
            description = await worker.request(
                "load", timeout=timeout, revision=source.descriptor(), directory=snapshot.name if snapshot else "", config=config or {}
            )
            return worker, description
        except BaseException as error:
            await worker.close()
            if isinstance(error, Exception):
                raise WorkerError(str(error), log=worker.stderr, traceback=worker.traceback) from error
            raise

    def _write(self, value: dict) -> None:
        if self.error or self.process.returncode is not None or self.process.stdin is None:
            raise PluginError(self.error or "Plugin process is closed")
        frame = json.dumps(value, ensure_ascii=True, allow_nan=False).encode() + b"\n"
        if len(frame) > MAX_REQUEST:
            raise PluginError("Plugin request exceeds protocol frame limit")
        self.process.stdin.write(frame)

    async def request(
        self, operation: str, *, timeout: float = 2, scope: InvocationScope | None = None, on_start: Callable[[int], None] | None = None, **parameters: Any
    ) -> Any:
        """One worker call. ``scope`` is restored around its host-service calls; ``on_start``
        learns the request identity before the frame is sent (continuation tokens bind to it)."""
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        self.operations[identity] = f"intercept:{parameters.get('name')}" if operation == "intercept" else operation
        if scope is not None:
            self.scopes[identity] = scope
        deadline = self.deadlines[identity] = RequestDeadline(timeout)
        if on_start is not None:
            on_start(identity)
        try:
            self._write({"id": identity, "operation": operation, **parameters})
            # wait(), not shield(): the future must outlive a cancel to receive the worker's
            # answer, and a shield cancelled first logs that answer's error as unhandled.
            async with deadline:
                assert self.process.stdin is not None
                await self.process.stdin.drain()
                await asyncio.wait((future,))
            return future.result()
        except TimeoutError as error:
            self.error = f"Plugin {operation} timed out after {timeout:g}s"
            await self.close()
            raise PluginError(self.error) from error
        except asyncio.CancelledError:
            # Give cooperative callbacks a brief chance to unwind. If the loop is blocked,
            # terminate the generation; cancellation must never strand the host's turn. A
            # worker that answers, even with the callback's CancelledError, stays usable.
            with contextlib.suppress(PluginError, BrokenPipeError, ConnectionResetError):
                self._write({"operation": "cancel", "target": identity})
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(0.2):
                    await asyncio.wait((future,))
            if not future.done() or self.error:
                await self.close()
            raise
        finally:
            self.pending.pop(identity, None)
            self.operations.pop(identity, None)
            await self.host_calls.cancel(identity)
            await self.continuations.cancel(identity)
            self.deadlines.pop(identity, None)
            self.scopes.pop(identity, None)
            if future.done() and not future.cancelled():
                future.exception()
            elif not future.done():
                future.cancel()

    async def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "service_id" in message:
                    self.host_calls.dispatch(message)
                    continue
                if "continue" in message:
                    self.continuations.dispatch(message)
                    continue
                future = self.pending.get(message["id"])
                if future is None or future.done():
                    continue
                if error := message.get("error"):
                    self.traceback = str(message.get("traceback", ""))
                    future.set_exception(PluginError(error))
                else:
                    future.set_result(message.get("value"))
        except Exception as error:  # noqa: BLE001 - malformed plugin output must not crash the host.
            self.error = f"Plugin protocol failed: {error}"
        finally:
            self.error = self.error or "Plugin process exited"
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(PluginError(self.error))
            await self.host_calls.cancel()
            await self.continuations.cancel()

    @property
    def stderr(self) -> str:
        return self._stderr.decode(errors="replace")

    async def _drain_errors(self) -> None:
        """Keep a bounded tail, draining at a bounded rate.

        Draining prevents print deadlocks, but a plugin printing in a tight loop made the host
        spend a whole core copying its output. Pacing each read by its size caps the drain at
        STDERR_RATE: beyond it the pipe fills and the flood blocks only the plugin's own writes.
        A line of ordinary logging costs microseconds of pacing.
        """
        assert self.process.stderr is not None
        while chunk := await self.process.stderr.read(64 * 1024):
            self._stderr = (self._stderr + chunk)[-8192:]
            await asyncio.sleep(len(chunk) / self.STDERR_RATE)

    STDERR_RATE = 8 * 1024 * 1024  # Bytes per second.

    async def close(self) -> None:
        """Offer bounded resource teardown, then kill the group and drain pipe consumers."""
        async with self._close_lock:
            await self.host_calls.cancel()
            await self.continuations.cancel()
            if not self.error and self.process.returncode is None:
                # Do not use request(): its timeout calls close(), which would await this lock.
                self.sequence += 1
                identity = self.sequence
                future = asyncio.get_running_loop().create_future()
                self.pending[identity] = future
                try:
                    self._write({"id": identity, "operation": "shutdown"})
                    async with asyncio.timeout(1):
                        await future
                except (PluginError, TimeoutError, BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    self.pending.pop(identity, None)
            # The leader may have exited while its descendants still hold our pipe ends.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            if self.process.stdin is not None:
                self.process.stdin.close()
            consumers = asyncio.gather(self.reader, self.errors, return_exceptions=True)
            try:
                async with asyncio.timeout(1):
                    await asyncio.shield(consumers)
            except TimeoutError:
                self.reader.cancel()
                self.errors.cancel()
                await consumers
            # wait() can wait for inherited pipes, not only the leader. A plugin that creates
            # a detached child is unsupported; even then teardown must have a finite deadline.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(1):
                    await self.process.wait()
            if self.snapshot is not None:
                self.snapshot.cleanup()
                self.snapshot = None
