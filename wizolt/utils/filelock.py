"""Serialize same-process file transactions without retaining locks for every visited path."""

from __future__ import annotations

import os
import threading
from _thread import LockType
from weakref import WeakValueDictionary

_registry_lock = threading.Lock()
_locks: WeakValueDictionary[str, LockType] = WeakValueDictionary()


def path_lock(path: str) -> LockType:
    """A worker-thread lock shared by aliases of one file; hold it across validation and write.

    This coordinates wizolt's own tools. Shell commands and external processes still require
    separate file boundaries and changed-target checks. Waiting workers retain their locks;
    otherwise the weak registry lets idle locks disappear.
    """
    identity = os.path.normcase(os.path.realpath(path))
    with _registry_lock:
        lock = _locks.get(identity)
        if lock is None:
            lock = _locks[identity] = threading.Lock()
        return lock
