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

    async def run(self, path: str, *, validate: bool = False, times: tuple[float, ...] = (0,), stimuli: tuple[Stimulus, ...] = ()) -> TrialReport:
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
