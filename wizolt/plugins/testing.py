"""Ephemeral plugin trials: exact-source loading, explicit stimuli and bounded feedback.

Trials never read or write the installation catalog. They still execute trusted Python with
the user's filesystem/network permissions; process isolation protects host liveness, not data.
The report contains semantic frames. UI exporters consume those frames in a higher layer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from wizolt.plugins.loading import PluginSource
from wizolt.plugins.process import PluginProcess, WorkerError
from wizolt.plugins.protocol import Capabilities, Snapshot
from wizolt.plugins.settings import PluginSettings
from wizolt.sdk import Context


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

    async def run(self, path: str, *, validate: bool = False, times: tuple[float, ...] = (0,), stimuli: tuple[Stimulus, ...] = ()) -> TrialReport:
        report = TrialReport()
        worker = None
        started = time.monotonic()
        try:
            source = PluginSource.read(path)
            report.name = source.name
            report.version = source.digest
            worker, report.capabilities = await PluginProcess.start(
                source, timeout=self.timeout, python=self.python, cwd=self.context.cwd, config=self.settings.read(source.name)
            )
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
                        result = await worker.request("event", timeout=self.timeout, name=stimulus.name, context=asdict(self.context))
                    elif stimulus.kind == "summarizer":
                        result = await worker.request("compact", timeout=self.timeout, text=stimulus.arguments["text"], context=asdict(self.context))
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
        return report
