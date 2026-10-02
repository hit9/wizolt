"""Application assembly: create/resume sessions and attach their feature resources.

Persistence only restores semantic state. These entry points own feature discovery, writable
resume leases and rollback if assembly fails. Worker assembly explicitly borrows the parent's
MCP/skills/catalog instead of creating another owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from wizolt.base import WizoltError
from wizolt.config import Config, ConfigFile, RuntimeSettings
from wizolt.mentions import FilePick
from wizolt.providers.sync import CatalogRuntime
from wizolt.session import Session, SessionLease, SessionSnapshotStore
from wizolt.session.ownership import canonical_snapshot_path, ownership_identity, subagent_root_uid
from wizolt.utils.workspace import Workspace

if TYPE_CHECKING:
    from wizolt.agent.engine import Agent


def create_session(*, path: str | None = None, yolo: bool = False, theme: str = "") -> Session:
    data = ConfigFile.load(path)
    catalog = CatalogRuntime(Config.data_dir_from(data))
    session = Session(
        config=Config.from_dict(data, policy=catalog.policy, path=ConfigFile.resolve_path(path)),
        settings=RuntimeSettings.from_dict(data, yolo=yolo, theme=theme),
        catalog=catalog,
    )
    bootstrap_features(session)
    return session


def load_session(
    uid: str,
    config: Config | None = None,
    settings: RuntimeSettings | None = None,
    cwd: str = "",
    catalog: CatalogRuntime | None = None,
    lease: SessionLease | None = None,
) -> Session:
    """Open a stored session for writable use, under its exclusive ownership lease.

    Aliases resolve to a concrete file before the lease is taken, existence and identity are
    revalidated under it, and only then is the snapshot decoded -- so a contended open leaves
    the log untouched and never builds state from a baseline another owner may have moved. A
    reserved lease handed in by the interactive handoff is used as-is and closed on failure.
    """

    path = ""
    try:
        if config is None:
            data = ConfigFile.load()
            catalog = catalog or CatalogRuntime(Config.data_dir_from(data))
            config = Config.from_dict(data, policy=catalog.policy, path=ConfigFile.resolve_path(None))
            if settings is None:
                settings = RuntimeSettings.from_dict(data)
        else:
            catalog = catalog or CatalogRuntime(config.data_dir)
        if settings is None:
            settings = RuntimeSettings()
        cwd = cwd or os.getcwd()
        resolved = SessionSnapshotStore.resolve_uid(uid, config.data_dir, cwd)
        if subagent_root_uid(resolved) != resolved:
            raise WizoltError(f"cannot resume a subagent directly; resume its main session: {resolved}")
        path = SessionSnapshotStore.find_session_path(config.data_dir, resolved)
        if not path:
            raise WizoltError(
                f"Session snapshot not found: {resolved} under {SessionSnapshotStore.path_for(config.data_dir, SessionSnapshotStore.PROJECTS_DIR)}"
            )
        if lease is None:
            lease = SessionLease.acquire(config.data_dir, path)
        lease.assert_owned(path)
        if not os.path.isfile(path):
            # Discovery can race with cleanup; a file that vanished before acquisition is a
            # normal not-found, not a reason to fall back to a decoded baseline.
            raise WizoltError(
                f"Session snapshot not found: {resolved} under {SessionSnapshotStore.path_for(config.data_dir, SessionSnapshotStore.PROJECTS_DIR)}"
            )
        session = SessionSnapshotStore.load(resolved, config=config, settings=settings, cwd=cwd)
        session.catalog = catalog
        session.adopt_ownership(lease, path)
        bootstrap_features(session)
        # A resumed session is fully live on return: it can carry active skills whose guards must
        # hold on the first tool call, and the replay that follows reloads nothing. So the scan
        # happens here rather than in the runtime's "starting" settle, which the fresh-session
        # path (`create_session`) uses instead.
        if session.skills is not None:
            session.skills.reload()
        return session
    except BaseException:
        # A reservation is consumed even if discovery/bootstrap fails, but an explicitly
        # mismatched capability belongs to another session and must not unlock that owner.
        if lease is not None and (not path or lease.identity == canonical_snapshot_path(ownership_identity(path))):
            lease.close()
        raise


def bootstrap_features(session: Session) -> None:
    """Attach the session's feature objects (MCP, skills, file mentions) when not already injected.

    Session itself stays feature-free: the dataclass constructor never reaches upward. Callers that
    need the features -- the runtime entry points and agent creation -- opt in explicitly after
    construction, so the feature packages sit above session/ without a module-scope cycle.
    """
    if session.mcp is None:
        from wizolt.mcp import MCPManager  # local import: mcp is built on top of session

        session.mcp = MCPManager(session)
    if session.skills is None:
        from wizolt.skill.library import SkillLibrary  # local import: skill is built on top of session

        # Constructed, not scanned: like MCP -- whose manager is attached here and whose servers
        # connect in the background -- the library is attached with its discovery and trust ready
        # and its index still empty. The interactive runtime runs the first scan during the
        # "starting" settle; `SkillLibrary.load` covers callers that need skills immediately.
        session.skills = SkillLibrary.attach(session)
    if session.shell_hooks is None:
        from wizolt.shellhooks import HookCommand, ShellHooks  # local import: shellhooks is built on top of session

        # Parsed here, not lazily: a malformed guard must stop startup, not silently never run.
        session.shell_hooks = ShellHooks(HookCommand.parse_table(session.config.hooks), Workspace(session.cwd).root)
    if session.mentions is None:
        from wizolt.mentions import FileMentions  # local import: mentions is built on top of session

        session.mentions = FileMentions(session)
    if session.agents is None:
        from wizolt.agentsmd import AgentsMentions  # local import: agentsmd is built on top of session

        session.agents = AgentsMentions(session)
    if session.catalog is None:
        from wizolt.providers.sync import CatalogRuntime  # local import: keeps providers above session

        session.catalog = CatalogRuntime(session.config.data_dir)


async def close_agent_resources(agent: Agent, *, shared_mcp: bool = False, reason: str = "other") -> None:
    """Quiesce request clients, then the root's MCP manager, on their owning event loop.

    A subagent borrows the root's MCP capability; closing its model must not close that manager.
    Individual close failures must not prevent the remaining resources from being settled.
    """
    group = agent.session.subagents
    closing_error: Exception | None = None
    if group is not None and group.root is agent:
        try:
            await group.close()
        except Exception as error:  # noqa: BLE001 - a failed save must not leak request clients.
            closing_error = error
        for entry in tuple(group.entries.values()):
            if entry.agent is not agent:
                await close_agent_resources(entry.agent, shared_mcp=True, reason=reason)
                entry.agent.session.close()
    try:
        await agent.end_session(reason)
    except Exception as error:  # noqa: BLE001 - a shutdown hook must not prevent resource cleanup
        with contextlib.suppress(Exception):
            agent.output_fn(f"SessionEnd hook failed: {error}")
    with contextlib.suppress(Exception):
        await agent.model.close()
    if not shared_mcp and agent.session.mcp is not None:
        with contextlib.suppress(Exception):
            await agent.session.mcp.close()
    if closing_error is not None:
        raise closing_error


class BackgroundServices:
    """Own one frontend invocation's maintenance tasks and coalesced workspace scans.

    Session state outlives event loops. Tasks do not: open admits work on the current loop, close
    cancels and joins it all, and a later invocation opens a fresh scope. Presentation receives
    unexpected failures through a single reporting callback.
    """

    def __init__(self, session: Session, report_error: Callable[[str], None]):
        self.session = session
        self.report_error = report_error
        self._background: set[asyncio.Task] = set()
        self._background_open = False
        self._mention_refresh: asyncio.Task | None = None

    def open_background(self) -> None:
        """Start admitting session-scoped background work on the loop that is now running."""
        self._background = set()
        self._background_open = True
        self._mention_refresh = None
        if self.session.mentions is not None:
            self.session.mentions.refresh_owner = self.refresh_mentions

    def refresh_mentions(self) -> asyncio.Task | None:
        """One mention-candidate scan at a time, owned here.

        Every caller -- the startup warm-up, the picker on a cold cache, a completion behind a
        keystroke -- joins the scan already running instead of starting a competing one. None means
        admission is closed, and the caller falls back to whatever snapshot it already has."""

        mentions = self.session.mentions
        if mentions is None:
            return None
        task = self._mention_refresh
        if task is not None and not task.done():
            return task
        self._mention_refresh = task = self.spawn_background(mentions.refresh(), name="mention-candidates")
        return task

    async def pick_file(self, query: str, *, colors: str = "") -> FilePick:
        """Run the interactive picker as work owned and settled by this session."""

        mentions = self.session.mentions
        if mentions is None:
            return FilePick(unavailable=True)
        task = self.spawn_background(mentions.picker.pick(query, colors=colors), name="file-picker")
        if task is None:
            return FilePick(unavailable=True)
        result = await task
        assert isinstance(result, FilePick)
        return result

    def spawn_background(self, coroutine: Coroutine[Any, Any, object], *, name: str) -> asyncio.Task | None:
        """Admit one background coroutine, and keep it until its outcome has been observed.

        Refusing after close is what makes shutdown mean something: a maintenance sweep scheduled
        while the loop is unwinding would mutate the session after close returned. A refused
        coroutine is closed here rather than dropped, so it never surfaces as a never-awaited
        warning."""

        if not self._background_open:
            coroutine.close()
            return None
        task = asyncio.get_running_loop().create_task(coroutine, name=name)
        self._background.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        # Expected maintenance failures are contained by the feature coroutines themselves and
        # published as their own status. Anything reaching here is an invariant break: make it
        # visible rather than leaving "Task exception was never retrieved" to the loop's handler.
        self.report_error(f"background task {task.get_name()} failed: {error}")

    async def close_background(self) -> None:
        """Stop admitting background work, then cancel and quiesce what was already accepted."""

        self._background_open = False
        if self.session.mentions is not None:
            self.session.mentions.refresh_owner = None
        self._mention_refresh = None
        pending, self._background = self._background, set()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
