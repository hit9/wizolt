"""Lazy, generation-owned resources for connections and background services.

Use an async context manager to express acquisition and teardown together. The worker closes
resources in reverse acquisition order after cancelling callbacks. A failed acquisition is
retryable; a resource that was acquired successfully is reused until this generation retires.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, contextmanager
from contextvars import ContextVar
from typing import Generic, TypeVar

from wizolt.sdk import PluginError

T = TypeVar("T")


class Service(Generic[T]):
    """A lazy resource handle. Call ``await service.get()`` from an async action."""

    def __init__(self, owner: Services, factory: Callable[[], AbstractAsyncContextManager[T]]):
        self._owner = owner
        self._factory = factory
        self._lock = asyncio.Lock()
        self._ready = False
        self._value: T

    async def get(self) -> T:
        if not self._owner.invoking.get():
            raise PluginError("Services may only be acquired inside an explicit action")
        async with self._lock:
            if self._owner.closed:
                raise PluginError("Plugin services are closed")
            if not self._ready:
                self._value = await self._owner.stack.enter_async_context(self._factory())
                self._ready = True
            return self._value


class Services:
    """One worker's resource scope; never shared across agents or reload generations."""

    def __init__(self):
        self.stack = AsyncExitStack()
        self.closed = False
        self.invoking = ContextVar("plugin_invoking", default=False)

    @contextmanager
    def invocation(self):
        """Worker-owned effect boundary: admission and repeated samples cannot start IO."""
        token = self.invoking.set(True)
        try:
            yield
        finally:
            self.invoking.reset(token)

    def register(self, factory: Callable[[], AbstractAsyncContextManager[T]]) -> Service[T]:
        return Service(self, factory)

    async def close(self) -> None:
        # The caller must cancel and join users first, including acquisitions in progress.
        self.closed = True
        await self.stack.aclose()
