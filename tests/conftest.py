import os
import shutil
import sys
from dataclasses import dataclass, field

import pytest
from network_guard import NetworkGuard
from rich.style import Style

_network = NetworkGuard()
sys.addaudithook(_network.audit)


@pytest.fixture(autouse=True)
def deny_external_network():
    """Caught transport failures must still fail the test that attempted real network IO."""
    _network.attempts.clear()
    _network.active = True
    yield
    _network.active = False
    assert not _network.attempts, "\n".join(_network.attempts)


@pytest.fixture
def offline_frontend(monkeypatch):
    """Frontend scenarios don't exercise remote maintenance; its own tests mock HTTP."""
    from wizolt.providers.sync import CatalogRuntime
    from wizolt.ui.cli.update import UpdateChecker

    monkeypatch.setattr(UpdateChecker, "load_cached", lambda _: False)
    monkeypatch.setattr(CatalogRuntime, "refresh_due", lambda _: False)

# Dedicated acceptance jobs must fail rather than silently skip if their multiplexer is missing.
if shutil.which("tmux") is None and os.environ.get("WIZOLT_REQUIRE_TMUX"):
    raise RuntimeError("this run requires tmux, but tmux is not installed")
if shutil.which("zellij") is None and os.environ.get("WIZOLT_REQUIRE_ZELLIJ"):
    raise RuntimeError("this run requires Zellij, but zellij is not installed")


@pytest.fixture(autouse=True)
def isolate_home(tmp_path_factory, monkeypatch):
    # `paths.data_dir` defaults to `~/.wizolt`, so any test that builds a config without setting
    # it and then saves a session writes into the developer's real home directory. Point HOME at a
    # per-test directory so `expanduser` resolves somewhere disposable.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # expanduser prefers this on Windows
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # An inherited COLUMNS=80 overrides the resized PTY in shutil.get_terminal_size.
    # Acceptance tests own their terminal geometry, not the shell that launched pytest.
    for name in ("COLUMNS", "LINES"):
        monkeypatch.delenv(name, raising=False)
    # A local proxy could otherwise turn an external request into an allowed loopback socket.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    # A wizolt agent's shell names its session's config and project; a suite started there must
    # not have `wizolt plugin` commands default to the developer's real ones.
    for name in ("WIZOLT_CONFIG", "WIZOLT_SESSION_CWD", "WIZOLT_PROJECT_DIR", "WIZOLT_EXECUTABLE"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture(autouse=True)
def reset_rich_style_cache():
    # Rich caches parsed/combined Style objects globally (Style.parse, Style._add, ... are
    # @lru_cache'd) and each Style memoizes its rendered SGR in `_ansi` keyed on the first
    # color_system it was ever rendered with. A test that renders a color through a standard-color
    # console poisons that shared `_ansi`, so a later test asserting truecolor bytes for the same
    # color sees the downgraded code. Drop the caches before each test so styling is deterministic
    # regardless of order.
    for name in ("normalize", "parse", "get_html_style", "clear_meta_and_links", "_add"):
        cached = getattr(Style, name, None)
        if cached is not None and hasattr(cached, "cache_clear"):
            cached.cache_clear()
    yield


@dataclass
class RecordingOutput:
    events: list[tuple[str, str]] = field(default_factory=list)

    def write_raw(self, text: str = "") -> None:
        self.events.append(("write", text))

    def erase_end_of_line(self) -> None:
        self.events.append(("erase", ""))

    def flush(self) -> None:
        self.events.append(("flush", ""))


@pytest.fixture
def recording_output():
    return RecordingOutput()


@pytest.fixture
def without_builtin_skills(tmp_path, monkeypatch):
    """Exercise empty-catalog behavior without relying on the packaged skill inventory."""
    from wizolt.skill.discovery import SkillDiscovery

    monkeypatch.setattr(SkillDiscovery, "BUILTIN_ROOT", str(tmp_path / "empty-builtins"))
