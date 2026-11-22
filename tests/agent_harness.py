"""Shared harness for the agent test modules: an isolated session, a tool-call factory, and
the user-input queue helpers."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from wizolt.agent.lifecycle import bootstrap_features
from wizolt.base import (
    ToolCall,
)
from wizolt.config import (
    Config,
    ProviderConfig,
)
from wizolt.session import Session


def session(tmp_path):
    # Isolate the data dir so tests never read the developer's real ~/.wizolt (sessions, skills).
    config = Config()
    config.data_dir = str(tmp_path / "data")
    session = Session(cwd=str(tmp_path), config=config)
    bootstrap_features(session)
    # The interactive runtime scans skills during the "starting" settle; a test plays the part of
    # that settle having finished, so the library it reads is indexed.
    if session.skills is not None:
        session.skills.reload()
    return session


def session_with_provider(tmp_path, **provider_kwargs):
    """A session whose default provider is complete, so the real request path (e.g. compaction's
    provider resolution) can serve instead of tripping missing_fields."""
    s = session(tmp_path)
    provider_kwargs.setdefault("model", "gpt-4")
    provider_kwargs.setdefault("url", "http://test")
    provider_kwargs.setdefault("key", "sk-test")
    s.config.providers = {"default": ProviderConfig(**provider_kwargs)}
    return s


def call(name, args):
    return ToolCall(name + "-id", name, args)


def queue(s, *texts):
    for text in texts:
        s.enqueue_user_input(text)


async def promote_bash_between_writes(session, monkeypatch):
    """Exercise real pipe handoff, advancing the Bash clock only after the first write is read."""
    from wizolt.tools import BashTool

    session.settings.bash_wait_timeout = 1
    session.settings.shell_timeout = 30
    release = Path(session.cwd) / "release-late"
    bash = BashTool(session, ["printf early; while [ ! -e release-late ]; do sleep 0.01; done; printf late"])
    stdout = []
    start = time.monotonic()

    def observe(stream, text):
        if stream == "stdout":
            stdout.append(text)

    bash.live_output = observe
    try:
        with monkeypatch.context() as patch:
            patch.setattr("wizolt.tools.shell.time", SimpleNamespace(monotonic=lambda: start + (1 if "".join(stdout) == "early" else 0)))
            promoted = await asyncio.wait_for(bash.call(), timeout=10)
    finally:
        release.touch()
    job = session.jobs["job.1"]
    assert job.stream_thread is not None
    await asyncio.to_thread(job.stream_thread.join, 10)
    assert not job.stream_thread.is_alive()
    return promoted
