"""Code index tools: symbol lookup over the local code-symbol-index database."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, ClassVar

from wizolt.base import Json, ToolArgs, ToolError, run_blocking
from wizolt.session import Session
from wizolt.source import INSPECT, SourceBlock, SourceSpan, SourceViewDraft, ToolOutput
from wizolt.tools.base import Tool

_csi_module: Any | None = None


def _csi() -> Any:
    """The code-symbol-index integration, imported on first use.

    The package costs tens of milliseconds and is only needed when a code-symbol call actually
    runs or a status read asks about the index, so the tool registry and the startup path must
    not import it eagerly."""
    global _csi_module
    if _csi_module is None:
        import code_symbol_index

        _csi_module = code_symbol_index
    return _csi_module


class CodeIndex:
    """Keep the symbol index useful without ever making the user wait for it.

    An optional accelerator: absent, stale, or broken, symbol lookups fall back to ordinary search
    rather than failing, which is why every integration failure here becomes a status instead of an
    exception. That status is published for the status bar to display.

    Freshness is opportunistic. Checking the working tree hashes files and is slow on a large
    repository, so it runs on a background thread after a turn, never in the path of an answer, and a
    flag keeps scans from stacking up. Changed files are re-indexed a batch at a time, and a caller
    that keeps taking passes until nothing is pending converges on its own; a whole-tree rebuild
    stays explicit, because that is the user's time to spend.
    """

    AUTO_UPDATE_LIMIT: ClassVar[int] = 20
    # What a pass returns when it applied a batch and left more pending: the caller takes another.
    MORE_PENDING: ClassVar[str] = "more pending"
    # How long a caller waits between batches. Each pass walks and hashes the tree, so a tight loop
    # would spend the whole session's idle time on indexing.
    CONVERGE_PAUSE: ClassVar[float] = 1.0
    # fmt: off
    SYMBOLS: ClassVar[dict[str, str]] = {
        "ready": "✓", "synced": "✓", "stale": "*", "syncing": "~",
        "updating": "~", "missing": "?", "unavailable": "!", "error": "!",
    }
    # fmt: on

    def __init__(self, session: Session):
        self.session = session

    def available(self) -> bool:
        status, message = self.status()
        self.session.state.code_index_error = message if status == "error" else ""
        return status in {"ready", "stale"}

    def set_status(self, status: str, message: str = "") -> None:
        self.session.state.code_index_status = "synced" if status == "ready" else status
        self.session.state.code_index_error = message if status == "error" else ""

    @classmethod
    def label(cls, status: str) -> str:
        return cls.SYMBOLS.get(status, status)

    @classmethod
    def status_line(cls, status: str, message: str = "") -> str:
        status = "synced" if status == "ready" else status
        return f"index{cls.label(status)} {status}" + ((": " + message) if message else "")

    def notice(self, text: str = "", *, refreshing: bool = False) -> None:
        self.session.state.code_index_notice = text
        self.session.state.code_index_refreshing = refreshing
        if text:
            self.session.state.code_index_status = "syncing" if text in {"syncing", "updating"} else text

    def fail(self, error: object) -> str:
        self.session.state.code_index_error = str(error).strip()
        self.notice("error")
        return self.session.state.code_index_error

    def finish(self, status: str = "synced") -> None:
        self.notice("")
        self.session.state.code_index_error, self.session.state.code_index_status = "", status

    def status(self, *, check: bool = False, max_pending_files: int = 20) -> tuple[str, str]:
        """Read and publish index status on the calling thread."""

        return self._publish_status(*self._read_status(check=check, max_pending_files=max_pending_files))

    def _publish_status(self, status: str, message: str, pending: object) -> tuple[str, str]:
        """Apply one status read to Session on its owning thread."""

        preserves_stale = status == "ready" and pending == "unknown" and self.session.state.code_index_status == "stale"
        if not self.session.state.code_index_refreshing and not preserves_stale:
            self.set_status(status, message)
        return status, message

    def _read_status(self, *, check: bool = False, max_pending_files: int = 20) -> tuple[str, str, object]:
        """Blocking third-party status read. It returns data and never mutates Session."""

        try:
            data = _csi().status(self.session.cwd, check=check, max_pending_files=max_pending_files)
        except Exception as error:  # noqa: BLE001 - isolate failures from the optional code-index integration.
            return "error", str(error), None
        status = str(getattr(data, "status", "") or "error")
        message = str(getattr(data, "message", None) or getattr(data, "reason", None) or "")
        pending = getattr(data, "pending_changes", None)
        files = getattr(data, "pending_files", ()) or ()
        if pending and pending != "unknown":
            sample = ", ".join(str(path) for path in (files or [])[:3])
            message = (message + "; " if message else "") + "pending " + str(pending) + ((" (" + sample + ")") if sample else "")
        return status, message, pending

    async def sync(self, *, force: bool = False) -> str:
        """Index or rebuild the whole tree, keeping the prompt live while it runs.

        The third-party clean/index pair is a long synchronous walk of the workspace; it goes to a
        worker whole, and the status the reader sees is written back here on the loop."""

        if self.session.state.code_index_refreshing:
            return "code_index: syncing"
        self.notice("syncing", refreshing=True)
        try:
            await run_blocking(lambda: self._sync_worker(force))
        except asyncio.CancelledError:
            await self._settle_cancelled_operation()
            raise
        except Exception as error:  # noqa: BLE001 - isolate failures from the optional code-index integration.
            return "code_index: error\n" + self.fail(error)
        self.finish()
        status, message = self._publish_status(*await run_blocking(lambda: self._read_status(check=True)))
        index_path = os.path.join(self.session.cwd, ".code-symbol-index", "index.sqlite")
        lines = ["code_index: " + ("rebuilt" if force else "synced"), "status: " + status, "path: " + index_path]
        if message:
            lines.append("note: " + message)
        return "\n".join(lines)

    def _sync_worker(self, force: bool) -> None:
        """The blocking third-party half of `sync`. Runs on a worker; touches no session state."""
        if force:
            _csi().clean(self.session.cwd)
        _csi().index(self.session.cwd)

    async def update(self, paths: list[str]) -> str:
        """Update edited paths without blocking the loop or publishing state from a worker."""

        paths = self.update_paths(paths)
        if not paths or self.session.state.code_index_refreshing:
            return ""
        status, _ = self._publish_status(*await run_blocking(self._read_status))
        if status not in {"ready", "stale"}:
            return ""
        return await self._update(paths)

    async def update_pending(self) -> str:
        """Check the working tree for drift and apply one batch, off the answer path.

        The `check=True` scan walks and hashes the tree, so it and the update it may trigger go to
        a worker; the coalescing flag and every status field are set here, on the loop. A count
        above `AUTO_UPDATE_LIMIT` is served a batch at a time rather than refused: the pass says
        `MORE_PENDING` and the caller decides when to come back."""

        if self.session.state.code_index_checking or self.session.state.code_index_refreshing:
            return ""
        # The flag is the coalescing gate: several triggers can arrive per turn (the turn end,
        # /status, a queued command) and only the first of them should walk the tree.
        self.session.state.code_index_checking = True
        try:
            try:
                data = await run_blocking(lambda: _csi().status(self.session.cwd, check=True, max_pending_files=self.AUTO_UPDATE_LIMIT + 1))
            except Exception:  # noqa: BLE001 - background index freshness checks are best-effort.
                return ""
            self.set_status(str(getattr(data, "status", "") or "error"), str(getattr(data, "message", None) or getattr(data, "reason", None) or ""))
            if getattr(data, "status", "") != "stale":
                return ""
            pending = getattr(data, "pending_changes", None)
            files = [str(path) for path in getattr(data, "pending_files", ()) or () if path]
            if not files:
                return ""
            more = len(files) > self.AUTO_UPDATE_LIMIT or (isinstance(pending, int) and pending > self.AUTO_UPDATE_LIMIT)
            batch = self.update_paths([self.session.resolve_path(path) for path in files[: self.AUTO_UPDATE_LIMIT]])
            result = await self._update(batch)
            # A pass that applied nothing (a failure, or another refresh holding the flag) must not
            # ask for another: the next pass would meet the same tree and do the same thing.
            return self.MORE_PENDING if more and result.startswith("updated") else result
        finally:
            self.session.state.code_index_checking = False

    async def _update(self, paths: list[str]) -> str:
        """`update` for a caller on the loop: the third-party call on a worker, the status here."""
        if not paths or self.session.state.code_index_refreshing:
            return ""
        self.notice("updating", refreshing=True)
        try:
            await run_blocking(lambda: _csi().update(paths, root=self.session.cwd))
        except asyncio.CancelledError:
            await self._settle_cancelled_operation()
            raise
        except Exception as error:  # noqa: BLE001 - isolate failures from the optional code-index integration.
            return self.fail(error)
        self.finish()
        return "updated " + str(len(paths)) + " file(s)"

    async def _settle_cancelled_operation(self) -> None:
        """Publish the real index state after a cancelled await has quiesced its worker."""

        self.notice("")
        self._publish_status(*await run_blocking(lambda: self._read_status(check=True)))

    def update_paths(self, paths: list[str]) -> list[str]:
        paths = [self.session.resolve_path(path) for path in paths]
        return list(dict.fromkeys(path for path in paths if self.session.in_cwd(path) and os.path.isfile(path)))


class InspectCodeTool(Tool):
    _WHITESPACE_RE: ClassVar[re.Pattern] = re.compile(r"\s")
    NAME = "InspectCode"
    MAX_LIMIT: ClassVar[int] = 80
    MAX_OUTLINE_LIMIT: ClassVar[int] = 1000
    MAX_DEPTH: ClassVar[int] = 5
    MODES: ClassVar[tuple[str, ...]] = ("find", "inspect", "outline", "refs", "impls", "callers", "callees")
    SYMBOL_MODES: ClassVar[frozenset[str]] = frozenset({"find", "inspect", "refs", "impls", "callers", "callees"})
    RESOLVE_MODES: ClassVar[frozenset[str]] = frozenset({"inspect", "refs", "impls", "callers", "callees"})
    CHAIN_MODES: ClassVar[frozenset[str]] = frozenset({"callers", "callees"})
    OPTION_KEYS: ClassVar[tuple[str, ...]] = ("limit", "kind", "path", "symbol", "exact_only", "depth", "offset", "all_kinds", "ref_kind", "loose")
    DESCRIPTION = (
        "Query the code index: find symbols, inspect one symbol with current source, outline a file, classify refs, find impls, "
        "or walk callers/callees. Returned source blocks are editable; stale index locations never fabricate source."
    )
    EXAMPLE = (
        'Find symbols. Example: {"mode":"find","target":"Tool","kind":"class,function","limit":20}',
        'Inspect one symbol. Example: {"mode":"inspect","target":"Tool","path":"src/app.py"}',
    )

    @classmethod
    def params_schema(cls) -> Json:
        props = {
            "mode": {"type": "string", "enum": list(cls.MODES)},
            "target": {"type": "string", "description": "Symbol name (find/inspect/refs/impls/callers/callees) or file path (outline)"},
            "limit": {"type": "integer", "minimum": 1, "maximum": cls.MAX_OUTLINE_LIMIT, "description": "Max results"},
            "kind": {"type": "string", "description": "Restrict to a symbol kind, e.g. function, class, method"},
            "path": {"type": "string", "description": "Restrict the search to this file or directory"},
            "symbol": {"type": "string", "description": "Disambiguate target when multiple symbols share a name"},
            "exact_only": {"type": "boolean", "description": "Match the target name exactly instead of fuzzily"},
            "depth": {"type": "integer", "minimum": 1, "maximum": cls.MAX_DEPTH, "description": "Call-chain depth for callers/callees"},
            "offset": {"type": "integer", "minimum": 0, "description": "Pagination offset for refs/impls"},
            "all_kinds": {"type": "boolean", "description": "Include all reference kinds, not just behavioral ones (refs)"},
            "ref_kind": {"type": "string", "description": "Restrict refs to a specific reference kind"},
            "loose": {"type": "boolean", "description": "Loosen call-chain matching (callees)"},
        }
        return cls.object_schema(props, ["mode", "target"])

    @classmethod
    def payload_args(cls, payload: Json) -> ToolArgs:
        options = {key: payload[key] for key in cls.OPTION_KEYS if key in payload}
        return [str(payload.get("mode") or ""), str(payload.get("target") or ""), *([options] if options else [])]

    def call(self) -> ToolOutput:
        if len(self.args) not in (2, 3):
            raise ToolError("InspectCode requires mode, target[, options]")
        if not isinstance(self.args[0], str) or not isinstance(self.args[1], str):
            raise ToolError("InspectCode mode and target must be strings")
        mode, target = self.args[0].lower(), self.args[1].strip()
        if len(self.args) == 3 and not isinstance(self.args[2], dict):
            raise ToolError("InspectCode options must be an object")
        options = self.args[2] if len(self.args) == 3 else {}
        if unexpected := sorted(set(options) - set(self.OPTION_KEYS)):
            raise ToolError("InspectCode unexpected option: " + ", ".join(unexpected))
        if mode not in self.MODES:
            raise ToolError("InspectCode mode must be one of: " + ", ".join(self.MODES))
        if not target:
            raise ToolError("InspectCode target is required")
        if mode in self.SYMBOL_MODES and self._WHITESPACE_RE.search(target):
            # Models often repeat the kind inside the target, e.g. target "class Config" with
            # kind "class". When the first word duplicates a declared kind, drop it — that is the one
            # case we can strip deterministically (no guessing at per-language keywords).
            kinds = {token.strip().lower() for token in str(options.get("kind") or "").split(",") if token.strip()}
            first, _, rest = target.partition(" ")
            if kinds and first.lower() in kinds and rest.strip():
                target = rest.strip()
            if self._WHITESPACE_RE.search(target):
                raise ToolError("InspectCode symbol target must not contain whitespace")
        if mode in self.RESOLVE_MODES and (target.endswith(".py") or os.path.exists(self.session.resolve_path(target))):
            raise ToolError(f"InspectCode {mode} target must be a symbol, not a file")
        if mode == "outline" and not os.path.isfile(self.session.resolve_path(target)):
            raise ToolError("InspectCode outline target must be an existing file")
        limit = options.get("limit")
        max_limit = self.MAX_OUTLINE_LIMIT if mode == "outline" else self.MAX_LIMIT
        self._check_int_option(limit, 1, max_limit, f"InspectCode {mode} limit must be 1..{max_limit}")
        self._check_int_option(options.get("depth"), 1, self.MAX_DEPTH, f"InspectCode depth must be 1..{self.MAX_DEPTH}")
        self._check_int_option(options.get("offset"), 0, None, "InspectCode offset must be >= 0")
        ref_kind = options.get("ref_kind")
        if ref_kind is not None:
            if not isinstance(ref_kind, str):
                raise ToolError("InspectCode ref_kind must be a string")
            if options.get("all_kinds"):
                raise ToolError("InspectCode ref_kind and all_kinds are mutually exclusive")
            tokens = [token.strip() for token in ref_kind.split(",") if token.strip()]
            if unknown := sorted(set(tokens) - _csi().REFERENCE_KINDS):
                raise ToolError("InspectCode unknown ref_kind: " + ", ".join(unknown) + "; valid: " + ", ".join(sorted(_csi().REFERENCE_KINDS)))
        index = CodeIndex(self.session)
        if not index.available():
            raise ToolError("code index is not available; run /index")
        try:
            output = self.inspect_text(mode, target, options, limit)
        except _csi().CodeSymbolIndexError as error:
            text = self.process_result("InspectCodeToolResult", 1, "", str(error))
            return ToolOutput.of(text)
        hydrated, blocks = self.hydrate(output)
        return ToolOutput.rendered([hydrated, *blocks])

    def hydrate(self, text: str) -> tuple[str, list[SourceBlock]]:
        """Hydrate the indexed definition from the current file as an editable source block.

        The index may identify a stale path or range; it never mints evidence itself. The
        definition's current lines are emitted only when the file exists and the indexed
        signature still matches; otherwise the block is omitted with an explicit stale note.
        """
        match = re.search(r"^  file: (.+)$\n^  range: (\d+):(\d+)$\n^  signature: (.*)$", text, re.MULTILINE)
        if not match:
            return text, []
        path, start, end, signature = match.group(1), int(match.group(2)), int(match.group(3)), match.group(4)
        resolved = self.session.resolve_path(path)
        try:
            with open(resolved, encoding="utf-8") as file:
                lines = file.readlines()
        except OSError:
            note = f"\n\n<stale-index> definition file {path} no longer exists; Read it again for current source</stale-index>"
            return text + note, []
        if start < 1 or start > len(lines) or lines[start - 1].rstrip("\n") != signature:
            note = f"\n\n<stale-index> index is stale for {path}; Read it again for current source</stale-index>"
            return text + note, []
        spans = SourceSpan.build(lines, [(start, min(end, len(lines)))])
        if not spans:
            return text, []
        return text, [SourceBlock.plain(SourceViewDraft(resolved, self.session.relpath(resolved), len(lines), spans, INSPECT))]

    @staticmethod
    def _check_int_option(value: object, low: int, high: int | None, message: str) -> None:
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
            raise ToolError(message)

    def inspect_text(self, mode: str, target: str, options: Json, limit: int | None) -> str:
        common = {
            "root": self.session.cwd,
            "kind": options.get("kind") or None,
            "path": options.get("path") or None,
            "exact_only": bool(options.get("exact_only")),
            "format": "text",
        }
        if mode == "find":
            return _csi().search(target, limit=limit or _csi().DEFAULT_SEARCH_LIMIT, **common)
        if mode == "inspect":
            return _csi().inspect(target, limit=limit or _csi().DEFAULT_PAGE_LIMIT, **common)
        if mode == "refs":
            ref_kinds = options.get("ref_kind") or ("all" if options.get("all_kinds") else "behavioral")
            return _csi().refs(target, limit=limit or _csi().DEFAULT_MAX_REFERENCES, offset=int(options.get("offset") or 0), ref_kinds=ref_kinds, **common)
        if mode == "impls":
            return _csi().impls(target, limit=limit or _csi().DEFAULT_MAX_IMPLEMENTORS, offset=int(options.get("offset") or 0), **common)
        if mode in self.CHAIN_MODES:
            depth = int(options.get("depth") or 3)
            if mode == "callees":
                return _csi().callees(target, limit=limit or _csi().DEFAULT_MAX_CALLEES, depth=depth, loose=bool(options.get("loose")), **common)
            return _csi().callers(target, limit=limit or _csi().DEFAULT_MAX_CALLERS, depth=depth, **common)
        symbol = options.get("symbol") or None
        return _csi().outline(
            target, root=self.session.cwd, symbol=str(symbol) if symbol else None, max_symbols=limit or _csi().DEFAULT_MAX_OUTLINE_SYMBOLS, format="text"
        )
