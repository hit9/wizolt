"""Host-owned invocation scope: which operation chain a piece of host work belongs to.

Workers never supply ancestry. The host maps each worker request to the scope it issued and
restores it around that request's host-service calls, so an auxiliary request made from inside
an interceptor can skip the registrations already active in its ancestry. Scopes are ephemeral
routing facts, never persisted or restored.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

MAX_DEPTH = 8  # Nested operations beyond this fail explicitly instead of queueing.


@dataclass(frozen=True)
class InvocationScope:
    root: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent: str = ""
    agent: str = ""
    active: frozenset[str] = frozenset()  # Registration IDs ("plugin/operation") in progress.
    depth: int = 0
    # Set on the scope of a plugin's own host-service requests (e.g. models.complete). Only those
    # skip registrations active in their ancestry; core descendants enter their full chains.
    auxiliary: bool = False

    def child(self, operation_id: str) -> InvocationScope:
        return replace(self, parent=operation_id, depth=self.depth + 1, auxiliary=False)

    def entering(self, registration: str, *, auxiliary: bool = False) -> InvocationScope:
        return replace(self, active=self.active | {registration}, auxiliary=auxiliary)


CURRENT: ContextVar[InvocationScope | None] = ContextVar("plugin_invocation_scope", default=None)
