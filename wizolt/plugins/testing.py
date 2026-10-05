"""Ephemeral plugin trials: exact-source loading, explicit stimuli and bounded feedback.

Trials never read or write the installation catalog. They still execute trusted Python with
the user's filesystem/network permissions; process isolation protects host liveness, not data.
The report contains semantic frames. UI exporters consume those frames in a higher layer.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from wizolt.config import ConfigFile
from wizolt.plugins.loading import PluginSource
from wizolt.plugins.process import PluginProcess, WorkerError
from wizolt.plugins.protocol import Capabilities, Snapshot
from wizolt.plugins.settings import PluginSettings
from wizolt.sdk import Context
from wizolt.sdk.models import HostCall


@dataclass(frozen=True)
class Stimulus:
    """One explicit event or action, never implicitly execute every registered operation."""

    kind: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrialReport:
    name: str = ""
    status: str = "failed"
    stage: str = "load"
    version: str = ""
    capabilities: dict = field(default_factory=dict)
    frames: list[dict] = field(default_factory=list)
    results: list[str | None] = field(default_factory=list)
    settings_changes: list[dict] = field(default_factory=list)
    # One per fixture entry: effective input that reached the scripted core (or None), the
    # delivered result, and provenance (origin, shaped) as live receipts record them.
    operations: list[dict] = field(default_factory=list)
    error: str = ""
    traceback: str = ""
    log: str = ""
    seconds: float = 0


class PluginTrial:
    """Own exactly one candidate process, and retire it on success, failure or cancellation."""

    def __init__(self, context: Context, *, timeout: float = 5, python: str = "", settings: PluginSettings | None = None):
        self.context = context
        self.timeout = timeout
        self.python = python
        self.settings = settings or PluginSettings()
        self.validate: Callable[[Capabilities], None] | None = None
        self.host_service: HostCall | None = None

    async def run(
        self, path: str, *, validate: bool = False, times: tuple[float, ...] = (0,), stimuli: tuple[Stimulus, ...] = (), operations: tuple[dict, ...] = ()
    ) -> TrialReport:
        report = TrialReport()
        worker = None
        temporary = tempfile.TemporaryDirectory(prefix="wizolt-plugin-trial-")
        started = time.monotonic()
        try:
            source = PluginSource.read(path)
            original = self.settings.read(source.name)
            settings = PluginSettings(path=str(Path(temporary.name) / "config.toml"))
            ConfigFile.patch_table(settings.path, ("plugins", source.name), original, [], expected={})
            services = TrialServices(source.name, settings, report, self.host_service)
            report.name = source.name
            report.version = source.digest
            worker, report.capabilities = await PluginProcess.start(source, timeout=self.timeout, python=self.python, cwd=self.context.cwd, config=original)
            worker.host_calls.handler = services.call
            if self.validate is not None:
                # Setup succeeded; a failure from here is a contribution, such as a bad preset.
                report.stage = "validate"
                self.validate(Capabilities.decode(source.name, report.capabilities))
            if not validate:
                for stimulus in stimuli:
                    report.stage = f"{stimulus.kind}:{stimulus.name}"
                    if stimulus.kind == "event":
                        if stimulus.name not in report.capabilities["events"]:
                            raise ValueError(f"No observer registered for {stimulus.name}")
                        result = await worker.request(
                            "event",
                            timeout=self.timeout,
                            name=stimulus.name,
                            context=asdict(self.context),
                            tool=stimulus.arguments.get("tool"),
                            reason=stimulus.arguments.get("reason", ""),
                        )
                    else:
                        result = await worker.request(
                            "invoke",
                            timeout=self.timeout,
                            kind=stimulus.kind,
                            name=stimulus.name,
                            arguments=stimulus.arguments,
                            context=asdict(self.context),
                        )
                    report.results.append(result)
                if operations:
                    chain = OperationChain(self.context, source, original, self.python, worker, report.capabilities)
                    chain.timeout = self.timeout  # --timeout bounds every worker call, intercept handlers included.
                    for index, entry in enumerate(operations):
                        report.stage = f"operation {index + 1}"
                        outcome = await chain.run(entry)
                        report.operations.append(outcome)
                        if failure := outcome.get("error") or outcome.get("fixture_error"):
                            raise ValueError(failure)
                report.stage = "snapshot"
                for moment in times:
                    context = replace(self.context, now=moment)
                    snapshot = Snapshot.decode(await worker.request("snapshot", timeout=self.timeout, context=asdict(context)))
                    report.frames.append({"context": asdict(context), **asdict(snapshot)})
            report.status = "passed"
        except Exception as error:  # noqa: BLE001 - trials are a feedback boundary, including syntax and IO errors.
            report.error = f"{type(error).__name__}: {error}"
            if isinstance(error, WorkerError):
                report.log, report.traceback = error.log, error.traceback
        finally:
            if worker is not None:
                await worker.close()
                report.log, report.traceback = worker.stderr, worker.traceback
            report.seconds = round(time.monotonic() - started, 6)
            temporary.cleanup()
        return report


class OperationChain:
    """Run fixture operations through the live chain executor, answering ``next`` from the fixture.

    Each entry is ``{"operation", "input", "next"}``. ``input`` is the operation's value (its
    ``type`` may be omitted). ``next`` is what wizolt would return: a result value, or
    ``{"error": "message"}`` for a failure. Omit ``next`` when the plugin must answer without
    calling it. A ``next`` the chain never asks for, or a call to ``next`` the fixture does not
    answer, fails the trial: nothing ever reaches wizolt's real implementation.
    """

    def __init__(self, context: Context, source: PluginSource, settings: dict, python: str, worker: PluginProcess, description: dict):
        from wizolt.plugins.runtime import Entry, Generation, PluginRuntime, Revision

        self.runtime = PluginRuntime(lambda: context)
        generation = Generation(Revision(source, settings, python), Capabilities.decode(source.name, description), worker)
        self.runtime.entries[source.name] = Entry(generation)
        self.timeout: float | None = None  # The trial's per-call deadline, when it runs operations.

    async def run(self, entry: object) -> dict:
        from wizolt.sdk import operations
        from wizolt.sdk.operations import OPERATIONS

        if not isinstance(entry, dict) or entry.keys() - {"operation", "input", "next"} or "input" not in entry:
            return {"fixture_error": "Each operation entry has operation, input and optional next"}
        spec = OPERATIONS.get(str(entry.get("operation")))
        if spec is None:
            return {"fixture_error": f"Unknown operation {entry.get('operation')!r}; choose {', '.join(sorted(OPERATIONS))}"}
        outcome: dict = {"operation": spec.name, "effective": None}
        try:
            value = operations.decode({"type": spec.input.TYPE, **dict(entry["input"])}, (spec.input,))
        except (TypeError, ValueError) as error:
            return {**outcome, "fixture_error": f"Invalid {spec.name} input: {error}"}

        async def scripted(effective: operations.Value) -> operations.Value:
            if "next" not in entry:
                raise ScriptedCoreError(f"{spec.name} called next(), but the fixture gives no next result")
            outcome["effective"] = operations.encode(effective)
            answer = entry["next"]
            if isinstance(answer, dict) and set(answer) == {"error"}:
                raise ScriptedCoreError(str(answer["error"]))
            default = next(cls for cls in spec.results if cls is not operations.Refusal)
            return operations.decode({"type": default.TYPE, **dict(answer)} if isinstance(answer, dict) else answer, spec.results)

        from wizolt.plugins.rules import adapter_rules

        transition, result_check = adapter_rules(value, self.runtime.interception_order.ordered)
        trace: dict = {}
        try:
            result = await self.runtime.interception.run(
                spec.name, value, scripted, transition=transition, result_check=result_check, trace=trace, handler_seconds=self.timeout
            )
            outcome["result"] = operations.encode(result)
        except Exception as error:  # noqa: BLE001 - a trial reports every failure as feedback.
            outcome["error"] = f"{type(error).__name__}: {error}"
        outcome["origin"], outcome["shaped"] = trace.get("origin", "core"), trace.get("shaped", [])
        if "next" in entry and outcome["effective"] is None and "error" not in outcome:
            outcome["fixture_error"] = f"The plugin answered {spec.name} without calling next(); remove next from this entry"
        return outcome


class ScriptedCoreError(Exception):
    """A failure the fixture asked wizolt's side to return, or a call it did not script."""


class TrialServices:
    """A trial writes a disposable config, never the author's selected profile.

    Only explicit host capabilities are provided; model/network services are not emulated.
    This protects SDK writes, not arbitrary Python filesystem access by trusted plugins.
    """

    def __init__(self, name: str, settings: PluginSettings, report: TrialReport, handler: HostCall | None):
        self.name, self.settings, self.report, self.handler = name, settings, report, handler

    async def call(self, service: str, arguments: dict) -> dict:
        if service.startswith("settings."):
            result = await self.settings.call(self.name, service, arguments)
            if service == "settings.update":
                self.report.settings_changes.append({"values": arguments["values"], "reset": arguments["reset"]})
            return result
        if service.startswith("ui.") and self.handler is not None:
            return await self.handler(service, arguments)
        from wizolt.sdk import PluginError

        raise PluginError("Host services unavailable in offline trials")
